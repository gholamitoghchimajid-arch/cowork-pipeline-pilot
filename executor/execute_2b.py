#!/usr/bin/env python3
# executor/execute_2b.py
# Component 2b -- BUS-INPUT executor / orchestrator. Wraps the already-merged,
# pure-local deterministic core (executor/patch_2b.py) with the unsafe-by-default
# I/O surface the core deliberately does NOT contain: bus-item ingestion, identity
# (trusted relay author + allowed workflow actor) gating, base64 transport decode,
# end-to-end hash recomputation, isolated-worktree apply, and a tightly-fenced push
# / draft-PR step.
#
# Trust + safety contract (fail-closed at every step; zero remote writes on any
# rejection):
#   * read EXACTLY ONE valid, unconsumed `cowork-patch` bus item (0 or >1 -> fail);
#   * the bus item MUST be authored by the trusted relay (kraken-cowork-relay[bot]);
#   * the run MUST be driven by the allowed actor (gholamitoghchimajid-arch);
#   * manifest + every blob arrive base64-encoded and are decoded here;
#   * recompute and verify ALL hashes: patch_sha256 over the manifest bytes and
#     content_sha256 over each blob (the core re-verifies content + tree again);
#   * the manifest base_commit MUST equal the current origin/main HEAD (no drift);
#   * apply ONLY inside an isolated `git worktree` -- the caller's checkout, HEAD,
#     and index are never mutated -- then verify the resulting tree via the core;
#   * push ONLY `exec/2b/*` branches; NEVER main; NEVER a non-fast update; NEVER a
#     pre-existing remote branch (no-clobber);
#   * open DRAFT pull requests only, and only when later run by GitHub Actions
#     (never during local bootstrap);
#   * this module never merges, and never touches rulesets / settings / branch
#     rules / secrets / environments.
#
# DEPENDENCIES: Python stdlib only (hashlib, base64, json, os, re, subprocess, sys,
# shutil, tempfile, pathlib) plus the already-merged local `patch_2b` core and the
# local `git` binary. The optional draft-PR HTTP call lazily imports urllib and is
# reached only under GitHub Actions; the test suite exercises every code path
# offline.
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Make `import patch_2b` resolve whether this file is run as a script (from the
# repo root) or imported by the test suite. The tests' conftest also adds this dir.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import patch_2b
from patch_2b import ManifestError

# --------------------------------------------------------------------------- #
# Pinned identities and policy constants
# --------------------------------------------------------------------------- #
TRUSTED_RELAY_AUTHOR = "kraken-cowork-relay[bot]"   # the only author a bus item may carry
ALLOWED_ACTOR = "gholamitoghchimajid-arch"          # the only actor allowed to dispatch
BUS_KIND = "cowork-patch"                            # bus-item discriminator
EXEC_BRANCH_PREFIX = "exec/2b/"                      # the ONLY pushable branch namespace
PR_BASE_BRANCH = "main"                              # draft PRs target main (open != merge)
PROTECTED_REFS = ("main", "master", "HEAD")          # never a push target

_HEX40 = re.compile(r"\A[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
# A branch name is restricted to the same safe token set the core uses for paths,
# which structurally excludes refspec operators ('+', ':') and whitespace.
_BRANCH = re.compile(r"\A[A-Za-z0-9._/-]+\Z")


class ExecutorError(Exception):
    """Fail-closed orchestration error. `code` is a stable E_* token."""

    def __init__(self, code: str, msg: str):
        self.code = code
        super().__init__(f"{code}: {msg}")


# --------------------------------------------------------------------------- #
# Identity gates
# --------------------------------------------------------------------------- #
def verify_actor(actor: str) -> bool:
    """The workflow actor MUST be the single allowed operator."""
    if actor != ALLOWED_ACTOR:
        raise ExecutorError("E_ACTOR", f"actor {actor!r} is not the allowed actor")
    return True


def verify_author(item: dict) -> bool:
    """The selected bus item MUST be authored by the trusted relay identity."""
    if (item or {}).get("author") != TRUSTED_RELAY_AUTHOR:
        raise ExecutorError("E_AUTHOR", "bus item is not authored by the trusted relay")
    return True


# --------------------------------------------------------------------------- #
# Bus-item selection (exactly one unconsumed cowork-patch item)
# --------------------------------------------------------------------------- #
def _is_unconsumed_patch(item: dict) -> bool:
    env = (item or {}).get("envelope") or {}
    if env.get("kind") != BUS_KIND:
        return False
    return env.get("consumed") is not True


def select_unconsumed_item(items) -> dict:
    """Return the one unconsumed cowork-patch bus item, or fail closed.

    Candidacy is author-agnostic on purpose: a second unconsumed cowork-patch item
    -- even a spoofed one -- makes the bus ambiguous and MUST stop the run before
    any apply or write. Author trust is asserted separately on the survivor."""
    candidates = [it for it in (items or []) if _is_unconsumed_patch(it)]
    if not candidates:
        raise ExecutorError("E_NO_BUS_ITEM", "no unconsumed cowork-patch bus item")
    if len(candidates) > 1:
        raise ExecutorError(
            "E_AMBIGUOUS_BUS",
            f"{len(candidates)} unconsumed cowork-patch bus items; expected exactly one",
        )
    return candidates[0]


# --------------------------------------------------------------------------- #
# Base64 transport decode
# --------------------------------------------------------------------------- #
def _b64(value, what: str) -> bytes:
    if not isinstance(value, str):
        raise ExecutorError("E_B64", f"missing base64 payload for {what}")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception:
        raise ExecutorError("E_B64", f"invalid base64 payload for {what}")


def decode_manifest(envelope: dict) -> bytes:
    """Decode the manifest bytes carried base64-encoded in the bus item."""
    return _b64((envelope or {}).get("manifest_b64"), "manifest")


def decode_blobs(envelope: dict) -> dict:
    """Decode each base64 blob into {content_sha256: raw_bytes}."""
    out: dict = {}
    for entry in (envelope or {}).get("blobs", []) or []:
        csha = (entry or {}).get("content_sha256")
        if not (isinstance(csha, str) and _HEX64.match(csha)):
            raise ExecutorError("E_BLOB_REF", "blob entry missing valid content_sha256")
        if csha in out:
            raise ExecutorError("E_BLOB_REF", f"duplicate blob entry: {csha}")
        out[csha] = _b64((entry or {}).get("blob_b64"), f"blob {csha}")
    return out


# --------------------------------------------------------------------------- #
# Hash recomputation (defence in depth; the core verifies content + tree again)
# --------------------------------------------------------------------------- #
def verify_patch_sha256(manifest_bytes: bytes, envelope: dict) -> str:
    """Recompute patch_sha256 over the manifest bytes and bind it to the approved
    value carried by the bus item. Canonicalisation matches PATCH_MANIFEST_HASH_SPEC
    (UTF-8, strip one BOM, CRLF/CR -> LF, strip trailing newlines)."""
    declared = (envelope or {}).get("patch_sha256")
    if not (isinstance(declared, str) and _HEX64.match(declared)):
        raise ExecutorError("E_PATCH_SHA", "bus item missing valid patch_sha256")
    try:
        text = manifest_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ExecutorError("E_PATCH_SHA", "manifest is not valid UTF-8")
    if text.startswith(chr(0xFEFF)):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if actual != declared:
        raise ExecutorError("E_PATCH_SHA_MISMATCH",
                            "recomputed patch_sha256 does not match the approved value")
    return actual


def verify_blob_hashes(blobs: dict) -> None:
    """Every decoded blob's raw bytes MUST hash to its declared content_sha256."""
    for csha, data in blobs.items():
        if hashlib.sha256(data).hexdigest() != csha:
            raise ExecutorError("E_BLOB_HASH_MISMATCH", f"blob hash mismatch: {csha}")


# --------------------------------------------------------------------------- #
# Base-drift gate
# --------------------------------------------------------------------------- #
def verify_no_base_drift(manifest_base: str, current_base: str) -> None:
    """The manifest's base_commit MUST equal the current origin/main HEAD."""
    if not (isinstance(current_base, str) and _HEX40.match(current_base or "")):
        raise ExecutorError("E_BASE_UNKNOWN", "current origin/main HEAD is unknown/invalid")
    if manifest_base != current_base:
        raise ExecutorError("E_BASE_DRIFT",
                            "manifest base_commit does not match current origin/main HEAD")


# --------------------------------------------------------------------------- #
# Local git helpers (no fetch on the hot path; remote ops are local-friendly)
# --------------------------------------------------------------------------- #
def _git(repo, *args, input_bytes=None) -> str:
    r = subprocess.run(("git", "-C", str(repo)) + args,
                       input=input_bytes, capture_output=True)
    if r.returncode != 0:
        raise ExecutorError("E_GIT", f"git {' '.join(args)}: {r.stderr.decode('utf-8','replace')}")
    return r.stdout.decode("utf-8")


def _commit_tree(worktree, tree: str, parent: str, message: str) -> str:
    """commit-tree with a fixed bot identity so a missing user.* config never
    blocks the run. This builds an object only; it is not a checkout or a write."""
    args = ("git", "-C", str(worktree),
            "-c", "user.name=cowork-2b-executor",
            "-c", "user.email=executor@cowork.invalid",
            "commit-tree", tree, "-p", parent, "-m", message)
    r = subprocess.run(args, capture_output=True)
    if r.returncode != 0:
        raise ExecutorError("E_GIT", f"git commit-tree: {r.stderr.decode('utf-8','replace')}")
    return r.stdout.decode("utf-8").strip()


# --------------------------------------------------------------------------- #
# Package materialisation
# --------------------------------------------------------------------------- #
def materialize_package(blobs: dict, dest) -> Path:
    """Lay the decoded blobs out as patches/blobs/<content_sha256> so the core can
    load them exactly as it loads an on-disk package."""
    bd = Path(dest) / "patches" / "blobs"
    bd.mkdir(parents=True, exist_ok=True)
    for csha, data in blobs.items():
        (bd / csha).write_bytes(data)
    return Path(dest)


# --------------------------------------------------------------------------- #
# Isolated-worktree apply + tree verification (delegates to the core)
# --------------------------------------------------------------------------- #
def apply_in_isolated_worktree(repo, manifest_bytes: bytes, package_dir, branch: str,
                               *, commit_message: str = "cowork 2b executor apply"):
    """Apply the manifest in a throwaway `git worktree` detached at base_commit,
    verify the resulting tree against expected_tree_sha via the core, and record the
    result as a fresh local `exec/2b/*` branch -- WITHOUT touching the caller's
    HEAD, index, or working tree. Returns (tree_sha, commit_sha). Nothing is pushed.

    On any failure the worktree is torn down and no branch is created, so the run
    stays fail-closed with zero side effects on the caller's repo."""
    assert_safe_branch_target(branch)
    m = patch_2b.parse_manifest(manifest_bytes)
    patch_2b.validate_schema(m)
    base = m["base_commit"]

    head_before = _git(repo, "rev-parse", "HEAD").strip()
    worktree = tempfile.mkdtemp(prefix="exec2b_wt_")
    try:
        _git(repo, "worktree", "add", "--detach", worktree, base)
        # compute_and_verify performs: object-format guard, parse, validate,
        # base==HEAD check, apply, write-tree, and expected_tree_sha comparison.
        tree = patch_2b.compute_and_verify(worktree, manifest_bytes, package_dir)
        commit = _commit_tree(worktree, tree, base, commit_message)
        # Create the branch ref in the caller's repo without checking it out.
        _git(repo, "branch", branch, commit)
    finally:
        shutil.rmtree(worktree, ignore_errors=True)
        _git(repo, "worktree", "prune")

    head_after = _git(repo, "rev-parse", "HEAD").strip()
    if head_after != head_before:
        raise ExecutorError("E_ISOLATION", "caller HEAD moved during apply")
    return tree, commit


# --------------------------------------------------------------------------- #
# Branch-target safety + fenced push
# --------------------------------------------------------------------------- #
def assert_safe_branch_target(branch) -> None:
    """A push/PR target MUST be a well-formed `exec/2b/*` branch and never a
    protected ref. Refspec operators are excluded by the character allowlist."""
    if not isinstance(branch, str) or not branch:
        raise ExecutorError("E_BRANCH", "empty branch name")
    if branch in PROTECTED_REFS:
        raise ExecutorError("E_PROTECTED_BRANCH", f"refusing to target protected ref {branch!r}")
    if not _BRANCH.match(branch):
        raise ExecutorError("E_BRANCH", f"illegal branch name: {branch!r}")
    if branch.endswith("/") or ".." in branch:
        raise ExecutorError("E_BRANCH", f"illegal branch name: {branch!r}")
    if not branch.startswith(EXEC_BRANCH_PREFIX):
        raise ExecutorError("E_BRANCH_PREFIX", f"branch must start with {EXEC_BRANCH_PREFIX!r}")
    # Defence in depth: even with the required prefix, never a protected leaf.
    if branch.split("/")[-1] in PROTECTED_REFS:
        raise ExecutorError("E_PROTECTED_BRANCH", f"refusing protected leaf in {branch!r}")


def build_push_refspec(branch: str) -> str:
    """Exact dst refspec with NO leading '+': git then refuses any non-fast-forward
    update, so an in-place overwrite can never happen even before the no-clobber
    pre-check below."""
    assert_safe_branch_target(branch)
    return f"refs/heads/{branch}:refs/heads/{branch}"


def remote_has_branch(repo, remote: str, branch: str) -> bool:
    out = _git(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
    return bool(out.strip())


def push_exec_branch(repo, remote: str, branch: str) -> str:
    """Push a single `exec/2b/*` branch. Refuses protected refs, refuses to clobber
    an existing remote branch, and never performs a non-fast update. Returns the
    refspec actually pushed."""
    assert_safe_branch_target(branch)
    if remote_has_branch(repo, remote, branch):
        raise ExecutorError("E_BRANCH_EXISTS",
                            f"refusing to overwrite existing remote branch {branch!r}")
    refspec = build_push_refspec(branch)
    _git(repo, "push", remote, refspec)
    return refspec


# --------------------------------------------------------------------------- #
# Draft PR (draft-only; only under GitHub Actions; open != merge)
# --------------------------------------------------------------------------- #
def running_in_github_actions(env=None) -> bool:
    env = env if env is not None else os.environ
    return env.get("GITHUB_ACTIONS") == "true"


def build_pr_payload(repo_slug: str, branch: str, *, title: str = None,
                     body: str = None, base: str = PR_BASE_BRANCH) -> dict:
    """Construct the draft-PR request body. Always draft; head is an `exec/2b/*`
    branch; base is main. Opening a draft PR is not a merge."""
    assert_safe_branch_target(branch)
    return {
        "title": title or f"[cowork-2b] {branch}",
        "head": branch,
        "base": base,
        "draft": True,
        "body": body or "Automated draft PR opened by the cowork 2b executor.",
        "maintainer_can_modify": False,
    }


def open_draft_pr(opener, repo_slug: str, branch: str, **kw) -> dict:
    """Open a DRAFT PR via the injected `opener(repo_slug, payload)`. Refuses to
    proceed unless the payload is marked draft."""
    payload = build_pr_payload(repo_slug, branch, **kw)
    if payload.get("draft") is not True:
        raise ExecutorError("E_PR_NOT_DRAFT", "pull requests must be draft")
    return opener(repo_slug, payload)


def _http_pr_opener(repo_slug: str, payload: dict) -> dict:
    """Default opener: POST a draft PR to the repo's pulls collection. Lazily
    imports urllib so the module imports with zero network surface; reached only
    under GitHub Actions."""
    import urllib.request  # lazy, Actions-only
    token = os.environ.get("GITHUB_TOKEN", "")
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo_slug}/pulls",
        data=data, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "execute-2b/1.0",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def execute(*, bus_items, actor, repo, remote, current_base, repo_slug=None,
            env=None, pr_opener=None, do_push=True):
    """Full fail-closed pipeline. Returns a self-report dict on success; raises
    ExecutorError / ManifestError (zero remote writes) on any rejection.

    `do_push` lets the bootstrap exercise the whole local pipeline without a remote
    write. The draft PR is opened only under GitHub Actions."""
    env = env if env is not None else os.environ
    steps: list = []

    def step(msg):
        steps.append(msg)

    verify_actor(actor)
    step(f"actor OK: {actor}")

    item = select_unconsumed_item(bus_items)
    step("bus: exactly one unconsumed cowork-patch item")

    verify_author(item)
    step(f"author OK: {TRUSTED_RELAY_AUTHOR}")

    envelope = item["envelope"]
    manifest_bytes = decode_manifest(envelope)
    blobs = decode_blobs(envelope)
    step(f"decoded manifest ({len(manifest_bytes)} bytes) + {len(blobs)} blob(s)")

    verify_patch_sha256(manifest_bytes, envelope)
    verify_blob_hashes(blobs)
    step("hashes OK: patch_sha256 + content_sha256")

    m = patch_2b.parse_manifest(manifest_bytes)
    patch_2b.validate_schema(m)
    verify_no_base_drift(m["base_commit"], current_base)
    step(f"base OK (no drift): {m['base_commit']}")

    branch = envelope.get("branch")
    assert_safe_branch_target(branch)
    step(f"branch target OK: {branch}")

    pkg_root = tempfile.mkdtemp(prefix="exec2b_pkg_")
    try:
        materialize_package(blobs, pkg_root)
        tree, commit = apply_in_isolated_worktree(repo, manifest_bytes, pkg_root, branch)
    finally:
        shutil.rmtree(pkg_root, ignore_errors=True)
    step(f"isolated apply + tree verify OK: tree={tree} commit={commit}")

    pushed = False
    if do_push:
        push_exec_branch(repo, remote, branch)
        pushed = True
        step(f"pushed {branch} (fast-forward, no-clobber)")

    pr = None
    if running_in_github_actions(env):
        slug = repo_slug or env.get("GITHUB_REPOSITORY", "")
        opener = pr_opener if pr_opener is not None else _http_pr_opener
        pr = open_draft_pr(opener, slug, branch)
        step("opened draft PR")
    else:
        step("draft PR skipped (not running under GitHub Actions)")

    return {
        "ok": True,
        "actor": actor,
        "author": TRUSTED_RELAY_AUTHOR,
        "branch": branch,
        "base_commit": m["base_commit"],
        "expected_tree_sha": m["expected_tree_sha"],
        "tree": tree,
        "commit": commit,
        "pushed": pushed,
        "pr": pr,
        "report": steps,
    }


# --------------------------------------------------------------------------- #
# Bus discovery from GitHub issues/comments (READ-ONLY; needs only issues: read)
# --------------------------------------------------------------------------- #
# The relay posts each bus item as a fenced ```cowork-patch <json>``` block inside
# an issue body or comment. Discovery is GET-only: the executor READS the bus, it
# never writes to it (no issues: write). The operator just clicks "Run workflow" --
# no file path, no copied payload, no manual courier of any kind.
_BUS_BLOCK = re.compile(r"```cowork-patch[ \t]*\n(.*?)\n```", re.DOTALL)
_GH_API = "https://api.github.com"


def parse_bus_envelope(body):
    """Return the decoded envelope from the first fenced cowork-patch block in
    `body`, or None when the body carries no well-formed cowork-patch item.

    Unrelated issue chatter yielding None is intentional (not an error): only
    genuine cowork-patch items become bus candidates; trust + uniqueness are
    enforced afterwards by select_unconsumed_item / verify_author."""
    if not isinstance(body, str):
        return None
    m = _BUS_BLOCK.search(body)
    if not m:
        return None
    try:
        env = json.loads(m.group(1))
    except Exception:
        return None
    if not isinstance(env, dict) or env.get("kind") != BUS_KIND:
        return None
    return env


def collect_bus_items(sources):
    """Build [{'author', 'envelope'}] from an iterable of (author_login, body)."""
    items = []
    for author, body in sources:
        env = parse_bus_envelope(body)
        if env is not None:
            items.append({"author": author, "envelope": env})
    return items


def _http_get_json(url, token):
    """READ-ONLY GitHub GET. Lazily imports urllib so module import has zero
    network surface; reached only under GitHub Actions."""
    import urllib.request  # lazy, Actions-only
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "execute-2b/1.0",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def discover_bus_items(repo_slug, token, *, getter=None):
    """Discover cowork-patch bus items by READING issues and their comments (GET
    only -- issues: read). Returns [{'author','envelope'}] with author taken from
    the GitHub item's user.login (the trust anchor). Pull requests are skipped.
    The injectable `getter` keeps this fully offline-testable."""
    getter = getter if getter is not None else _http_get_json
    sources = []
    page = 1
    while True:
        issues = getter(f"{_GH_API}/repos/{repo_slug}/issues?state=open&per_page=100&page={page}", token)
        if not isinstance(issues, list) or not issues:
            break
        for issue in issues:
            if "pull_request" in issue:
                continue
            sources.append(((issue.get("user") or {}).get("login"), issue.get("body") or ""))
            num = issue.get("number")
            cpage = 1
            while True:
                comments = getter(
                    f"{_GH_API}/repos/{repo_slug}/issues/{num}/comments?per_page=100&page={cpage}", token)
                if not isinstance(comments, list) or not comments:
                    break
                for c in comments:
                    sources.append(((c.get("user") or {}).get("login"), c.get("body") or ""))
                if len(comments) < 100:
                    break
                cpage += 1
        if len(issues) < 100:
            break
        page += 1
    return collect_bus_items(sources)


def mark_consumed_by_remote(repo, remote, items):
    """Return items with envelope.consumed forced True for any item whose requested
    exec/2b/* branch already exists on the remote -- the real consumption signal,
    derived WITHOUT any issues: write. A re-run thus skips already-applied items,
    while two genuinely-unapplied items still trip E_AMBIGUOUS_BUS."""
    out = []
    for it in items:
        env = dict(it.get("envelope") or {})
        branch = env.get("branch")
        try:
            consumed = isinstance(branch, str) and bool(branch) and remote_has_branch(repo, remote, branch)
        except ExecutorError:
            consumed = False
        if consumed:
            env["consumed"] = True
        out.append({"author": it.get("author"), "envelope": env})
    return out


# --------------------------------------------------------------------------- #
# Actions entry point -- ONE CLICK. No file path, no manual input of any kind.
# (Not exercised during local bootstrap.)
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    env = os.environ
    actor = env.get("GITHUB_ACTOR", "")
    repo = env.get("GITHUB_WORKSPACE") or "."
    remote = env.get("COWORK_REMOTE", "origin")
    repo_slug = env.get("GITHUB_REPOSITORY", "")
    token = env.get("GITHUB_TOKEN", "")

    # Actor gate FIRST: a wrong actor fails closed before any read or apply.
    try:
        verify_actor(actor)
    except ExecutorError as e:
        print(f"FAIL-CLOSED {e}")
        return 1

    if not (repo_slug and token):
        print("E_CONFIG: GITHUB_REPOSITORY and GITHUB_TOKEN are required")
        return 2

    try:
        items = discover_bus_items(repo_slug, token)             # issues: read, GET-only
        items = mark_consumed_by_remote(repo, remote, items)     # consumed == exec/2b/* exists
        current_base = (env.get("COWORK_BASE", "").strip()
                        or _git(repo, "rev-parse", f"refs/remotes/{remote}/{PR_BASE_BRANCH}").strip())
        result = execute(bus_items=items, actor=actor, repo=repo, remote=remote,
                         current_base=current_base, repo_slug=repo_slug, env=env,
                         do_push=True)
    except (ExecutorError, ManifestError) as e:
        print(f"FAIL-CLOSED {e}")
        return 1

    print("cowork 2b executor: OK")
    for line in result["report"]:
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
