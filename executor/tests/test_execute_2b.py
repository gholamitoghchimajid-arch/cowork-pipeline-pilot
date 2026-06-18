# executor/tests/test_execute_2b.py
# PR-2b-bootstrap tests for the bus-input executor/orchestrator
# (executor/execute_2b.py). The 2b CORE (patch_2b.py) is verified elsewhere; here
# we prove the orchestration + safety envelope around it:
#
#   * isolated-worktree apply leaves the caller's HEAD/index/working tree untouched
#   * hash mismatch (patch_sha256 / content_sha256) fails closed
#   * base drift fails closed
#   * wrong actor / wrong author fail closed
#   * more than one unconsumed cowork-patch bus item fails closed (ambiguity)
#   * main()/discovery has no file-bus dependency; it is one-click (no required input)
#   * push refuses main, refuses to clobber, and never performs a non-fast update
#   * draft PRs only, and only under GitHub Actions
#   * static audit: no merge call; no ruleset/settings/branch-protection endpoints
#   * static audit: the 2b workflow has no `issues: write`, has `issues: read`,
#     least-privilege perms, workflow_dispatch, and requires NO manual input
#
# Real local git is used (deterministic temp repos, bare "remotes", worktrees);
# every assertion holds OFFLINE. The autouse no-network guard in conftest.py blocks
# Python sockets for the whole module, so any accidental real GitHub call would
# fail the test rather than reach the network. (`import execute_2b` / `patch_2b` /
# `from util...` resolve via conftest.py.)
from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

import execute_2b
from execute_2b import ExecutorError
import patch_2b
from patch_2b import ManifestError
from util.repo_builder import build_base_repo


EXEC_DIR = Path(execute_2b.__file__).resolve().parent           # executor/
SRC_PATH = EXEC_DIR / "execute_2b.py"
SRC = SRC_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SRC)
WORKFLOW_PATH = EXEC_DIR.parent / ".github" / "workflows" / "executor-2b-execute.yml"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _patch_sha256(manifest_bytes: bytes) -> str:
    """Canonical patch_sha256 per PATCH_MANIFEST_HASH_SPEC (UTF-8, strip BOM,
    CRLF/CR -> LF, strip trailing newlines) -- matches verify_patch_sha256."""
    text = manifest_bytes.decode("utf-8")
    if text.startswith(chr(0xFEFF)):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _git(repo, *args) -> str:
    r = subprocess.run(("git", "-C", str(repo)) + args, capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return r.stdout.decode("utf-8")


# --------------------------------------------------------------------------- #
# Fixtures: a deterministic base repo, package, manifest, bus envelope, remote.
# --------------------------------------------------------------------------- #
def _manifest_bytes(base: str, ops: list, expected: str) -> bytes:
    lines = ["schema: 2b-patch/1.0", f"base_commit: {base}", "operations:"]
    for op in ops:
        lines.append(f"  - op: {op['op']}")
        lines.append(f"    path: {op['path']}")
        if op["op"] in ("add", "replace"):
            lines.append(f"    content_sha256: {op['content_sha256']}")
            lines.append(f'    mode: "{op["mode"]}"')
    lines.append(f"expected_tree_sha: {expected}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _package(tmp_path: Path, blobs: dict) -> Path:
    pkg = Path(tempfile.mkdtemp(dir=str(tmp_path), prefix="pkg_"))
    bd = pkg / "patches" / "blobs"
    bd.mkdir(parents=True)
    for csha, data in blobs.items():
        (bd / csha).write_bytes(data)
    return pkg


def _expected_tree(repo: Path, base: str, manifest_bytes: bytes, pkg: Path) -> str:
    """Compute the real post-apply tree id in a throwaway worktree (so the caller
    repo's index is never disturbed), mirroring how fixtures pin expected_tree_sha."""
    wt = tempfile.mkdtemp(prefix="exp_wt_")
    try:
        _git(repo, "worktree", "add", "--detach", wt, base)
        return patch_2b.compute_tree_unverified(wt, manifest_bytes, pkg)
    finally:
        import shutil
        shutil.rmtree(wt, ignore_errors=True)
        _git(repo, "worktree", "prune")


def _add_scenario(tmp_path: Path):
    """A valid single-add scenario. Returns (repo, base, manifest_bytes, pkg, blob_map)."""
    repo = tmp_path / "repo"
    base = build_base_repo(repo, {"README.md": b"base\n"})
    blob = b"hello cowork 2b\n"
    csha = _sha(blob)
    ops = [{"op": "add", "path": "pilot/noop-2b.txt", "content_sha256": csha, "mode": "100644"}]
    blobs = {csha: blob}
    pkg = _package(tmp_path, blobs)
    expected = _expected_tree(repo, base, _manifest_bytes(base, ops, "0" * 40), pkg)
    manifest_bytes = _manifest_bytes(base, ops, expected)
    return repo, base, manifest_bytes, pkg, blobs


def _envelope(base: str, branch: str, manifest_bytes: bytes, blobs: dict, *,
              consumed: bool = False, patch_sha: str = None) -> dict:
    return {
        "kind": "cowork-patch",
        "consumed": consumed,
        "branch": branch,
        "base_commit": base,
        "patch_sha256": patch_sha if patch_sha is not None else _patch_sha256(manifest_bytes),
        "manifest_b64": _b64(manifest_bytes),
        "blobs": [{"content_sha256": c, "blob_b64": _b64(d)} for c, d in blobs.items()],
    }


def _bus_item(base, branch, manifest_bytes, blobs, *, author=execute_2b.TRUSTED_RELAY_AUTHOR,
              consumed=False, patch_sha=None) -> dict:
    return {"author": author,
            "envelope": _envelope(base, branch, manifest_bytes, blobs,
                                   consumed=consumed, patch_sha=patch_sha)}


def _bare_remote(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    r = subprocess.run(("git", "init", "--bare", "-b", "main", str(origin)), capture_output=True)
    assert r.returncode == 0, r.stderr
    return origin


def _attach_origin(repo: Path, origin: Path) -> None:
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "main")     # seed origin/main (local path; no network)


# --------------------------------------------------------------------------- #
# (1) Isolated-worktree behavior
# --------------------------------------------------------------------------- #
def test_isolated_worktree_leaves_caller_untouched(tmp_path):
    repo, base, manifest_bytes, pkg, _ = _add_scenario(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD").strip()

    tree, commit = execute_2b.apply_in_isolated_worktree(
        repo, manifest_bytes, pkg, "exec/2b/iso")

    # The applied result is a real branch whose tree is the verified tree.
    assert _git(repo, "rev-parse", "exec/2b/iso").strip() == commit
    assert _git(repo, "rev-parse", "exec/2b/iso^{tree}").strip() == tree
    # The caller's HEAD never moved...
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before
    # ...the applied file never materialized in the caller's working tree...
    assert not (repo / "pilot" / "noop-2b.txt").exists()
    # ...and no worktree leaked.
    assert "exec2b_wt_" not in _git(repo, "worktree", "list")


def test_isolated_worktree_failure_creates_no_branch(tmp_path):
    # Wrong expected_tree_sha -> core fails closed -> no branch, caller untouched.
    repo, base, _, pkg, blobs = _add_scenario(tmp_path)
    csha = next(iter(blobs))
    ops = [{"op": "add", "path": "pilot/noop-2b.txt", "content_sha256": csha, "mode": "100644"}]
    bad = _manifest_bytes(base, ops, "f" * 40)
    with pytest.raises(ManifestError) as ei:
        execute_2b.apply_in_isolated_worktree(repo, bad, pkg, "exec/2b/willfail")
    assert ei.value.code == "E_TREE_MISMATCH"
    rc = subprocess.run(("git", "-C", str(repo), "rev-parse", "--verify", "exec/2b/willfail"),
                        capture_output=True)
    assert rc.returncode != 0, "no branch must exist after a failed apply"


# --------------------------------------------------------------------------- #
# (2) Hash mismatch reject
# --------------------------------------------------------------------------- #
def test_patch_sha256_mismatch_rejected():
    env = {"patch_sha256": "a" * 64}
    with pytest.raises(ExecutorError) as ei:
        execute_2b.verify_patch_sha256(b"schema: 2b-patch/1.0\n", env)
    assert ei.value.code == "E_PATCH_SHA_MISMATCH"


def test_patch_sha256_match_ok():
    mb = b"schema: 2b-patch/1.0\n"
    env = {"patch_sha256": _sha(b"schema: 2b-patch/1.0")}   # trailing-newline stripped per spec
    assert execute_2b.verify_patch_sha256(mb, env) == env["patch_sha256"]


def test_content_sha256_mismatch_rejected():
    with pytest.raises(ExecutorError) as ei:
        execute_2b.verify_blob_hashes({"0" * 64: b"not the right bytes"})
    assert ei.value.code == "E_BLOB_HASH_MISMATCH"


def test_content_sha256_match_ok():
    data = b"abc\n"
    execute_2b.verify_blob_hashes({_sha(data): data})   # no raise


# --------------------------------------------------------------------------- #
# (3) Base drift reject
# --------------------------------------------------------------------------- #
def test_base_drift_rejected():
    with pytest.raises(ExecutorError) as ei:
        execute_2b.verify_no_base_drift("a" * 40, "b" * 40)
    assert ei.value.code == "E_BASE_DRIFT"


def test_base_no_drift_ok():
    execute_2b.verify_no_base_drift("a" * 40, "a" * 40)   # no raise


def test_base_drift_via_execute(tmp_path):
    repo, base, manifest_bytes, pkg, blobs = _add_scenario(tmp_path)
    item = _bus_item(base, "exec/2b/x", manifest_bytes, blobs)
    with pytest.raises(ExecutorError) as ei:
        execute_2b.execute(bus_items=[item], actor=execute_2b.ALLOWED_ACTOR, repo=repo,
                           remote="origin", current_base="c" * 40, do_push=False)
    assert ei.value.code == "E_BASE_DRIFT"


# --------------------------------------------------------------------------- #
# (4) Wrong actor reject  /  (5) Wrong author reject
# --------------------------------------------------------------------------- #
def test_wrong_actor_rejected():
    with pytest.raises(ExecutorError) as ei:
        execute_2b.verify_actor("somebody-else")
    assert ei.value.code == "E_ACTOR"


def test_right_actor_ok():
    assert execute_2b.verify_actor("gholamitoghchimajid-arch") is True


def test_wrong_author_rejected():
    item = {"author": "attacker", "envelope": {"kind": "cowork-patch", "consumed": False}}
    with pytest.raises(ExecutorError) as ei:
        execute_2b.verify_author(item)
    assert ei.value.code == "E_AUTHOR"


def test_right_author_ok():
    item = {"author": execute_2b.TRUSTED_RELAY_AUTHOR, "envelope": {}}
    assert execute_2b.verify_author(item) is True


def test_wrong_actor_fails_before_unsafe_work(tmp_path, monkeypatch):
    # execute() must verify the actor BEFORE touching the bus / applying / pushing.
    repo, base, manifest_bytes, pkg, blobs = _add_scenario(tmp_path)
    item = _bus_item(base, "exec/2b/x", manifest_bytes, blobs)

    def _boom(*a, **k):
        raise AssertionError("unsafe work ran despite wrong actor")

    monkeypatch.setattr(execute_2b, "select_unconsumed_item", _boom)
    monkeypatch.setattr(execute_2b, "apply_in_isolated_worktree", _boom)
    monkeypatch.setattr(execute_2b, "push_exec_branch", _boom)
    with pytest.raises(ExecutorError) as ei:
        execute_2b.execute(bus_items=[item], actor="intruder", repo=repo,
                           remote="origin", current_base=base, do_push=True)
    assert ei.value.code == "E_ACTOR"


def test_main_wrong_actor_returns_nonzero_without_discovery(monkeypatch, capsys):
    # main() gates the actor first: a wrong actor must never trigger GitHub discovery.
    def _boom(*a, **k):
        raise AssertionError("discovery ran despite wrong actor")

    monkeypatch.setattr(execute_2b, "discover_bus_items", _boom)
    monkeypatch.setenv("GITHUB_ACTOR", "intruder")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    rc = execute_2b.main()
    assert rc == 1
    assert "E_ACTOR" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# (6) Multiple unconsumed bus items reject
# --------------------------------------------------------------------------- #
def test_multiple_unconsumed_items_rejected():
    e = {"kind": "cowork-patch", "consumed": False}
    items = [{"author": execute_2b.TRUSTED_RELAY_AUTHOR, "envelope": dict(e)},
             {"author": execute_2b.TRUSTED_RELAY_AUTHOR, "envelope": dict(e)}]
    with pytest.raises(ExecutorError) as ei:
        execute_2b.select_unconsumed_item(items)
    assert ei.value.code == "E_AMBIGUOUS_BUS"


def test_zero_unconsumed_items_rejected():
    items = [{"author": execute_2b.TRUSTED_RELAY_AUTHOR,
              "envelope": {"kind": "cowork-patch", "consumed": True}}]
    with pytest.raises(ExecutorError) as ei:
        execute_2b.select_unconsumed_item(items)
    assert ei.value.code == "E_NO_BUS_ITEM"


def test_exactly_one_unconsumed_selected_among_consumed():
    chosen = {"author": execute_2b.TRUSTED_RELAY_AUTHOR,
              "envelope": {"kind": "cowork-patch", "consumed": False, "branch": "exec/2b/keep"}}
    items = [
        {"author": execute_2b.TRUSTED_RELAY_AUTHOR, "envelope": {"kind": "cowork-patch", "consumed": True}},
        chosen,
        {"author": "someone", "envelope": {"kind": "note", "consumed": False}},   # not a patch item
    ]
    assert execute_2b.select_unconsumed_item(items) is chosen


# --------------------------------------------------------------------------- #
# (7) No main push  /  (8) No force push  /  (9) No clobber
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["main", "master", "HEAD"])
def test_branch_target_refuses_protected(bad):
    with pytest.raises(ExecutorError) as ei:
        execute_2b.assert_safe_branch_target(bad)
    assert ei.value.code == "E_PROTECTED_BRANCH"


def test_branch_target_refuses_non_exec_prefix():
    with pytest.raises(ExecutorError) as ei:
        execute_2b.assert_safe_branch_target("feature/x")
    assert ei.value.code == "E_BRANCH_PREFIX"


def test_push_refuses_main(tmp_path):
    repo, base, manifest_bytes, pkg, blobs = _add_scenario(tmp_path)
    origin = _bare_remote(tmp_path)
    _attach_origin(repo, origin)
    with pytest.raises(ExecutorError) as ei:
        execute_2b.push_exec_branch(repo, "origin", "main")
    assert ei.value.code == "E_PROTECTED_BRANCH"
    # main on origin still points at the seeded base (unchanged).
    assert _git(origin, "rev-parse", "main").strip() == base


def test_push_refspec_is_never_a_force_update():
    rs = execute_2b.build_push_refspec("exec/2b/x")
    assert rs == "refs/heads/exec/2b/x:refs/heads/exec/2b/x"
    assert not rs.startswith("+"), "a leading '+' would make this a non-fast (force) update"


def test_push_succeeds_then_refuses_clobber(tmp_path):
    repo, base, manifest_bytes, pkg, blobs = _add_scenario(tmp_path)
    origin = _bare_remote(tmp_path)
    _attach_origin(repo, origin)

    execute_2b.apply_in_isolated_worktree(repo, manifest_bytes, pkg, "exec/2b/run1")
    execute_2b.push_exec_branch(repo, "origin", "exec/2b/run1")
    assert _git(origin, "rev-parse", "--verify", "exec/2b/run1").strip()

    # A second push to the same existing remote branch must be refused (no clobber).
    with pytest.raises(ExecutorError) as ei:
        execute_2b.push_exec_branch(repo, "origin", "exec/2b/run1")
    assert ei.value.code == "E_BRANCH_EXISTS"


def test_mark_consumed_by_remote_flags_existing_branch(tmp_path):
    repo, base, manifest_bytes, pkg, blobs = _add_scenario(tmp_path)
    origin = _bare_remote(tmp_path)
    _attach_origin(repo, origin)
    execute_2b.apply_in_isolated_worktree(repo, manifest_bytes, pkg, "exec/2b/done")
    execute_2b.push_exec_branch(repo, "origin", "exec/2b/done")

    items = [
        {"author": execute_2b.TRUSTED_RELAY_AUTHOR,
         "envelope": {"kind": "cowork-patch", "consumed": False, "branch": "exec/2b/done"}},
        {"author": execute_2b.TRUSTED_RELAY_AUTHOR,
         "envelope": {"kind": "cowork-patch", "consumed": False, "branch": "exec/2b/fresh"}},
    ]
    marked = execute_2b.mark_consumed_by_remote(repo, "origin", items)
    # The already-pushed branch is now consumed; only the fresh one survives selection.
    survivor = execute_2b.select_unconsumed_item(marked)
    assert survivor["envelope"]["branch"] == "exec/2b/fresh"


# --------------------------------------------------------------------------- #
# (10) Draft PR only (and only under GitHub Actions)
# --------------------------------------------------------------------------- #
def test_pr_payload_is_draft():
    p = execute_2b.build_pr_payload("owner/repo", "exec/2b/x")
    assert p["draft"] is True
    assert p["head"] == "exec/2b/x"
    assert p["base"] == "main"


def test_open_draft_pr_passes_draft_payload():
    captured = {}

    def opener(repo_slug, payload):
        captured["slug"] = repo_slug
        captured["payload"] = payload
        return {"number": 7, "draft": True}

    res = execute_2b.open_draft_pr(opener, "owner/repo", "exec/2b/x")
    assert res["number"] == 7
    assert captured["payload"]["draft"] is True


def test_pr_not_opened_outside_github_actions():
    assert execute_2b.running_in_github_actions({}) is False
    assert execute_2b.running_in_github_actions({"GITHUB_ACTIONS": "true"}) is True


def test_execute_skips_pr_when_not_in_actions(tmp_path):
    # Full local pipeline (no remote write, not under Actions): applies + verifies,
    # opens NO PR. Proves draft PRs are deferred to the GitHub Actions run.
    repo, base, manifest_bytes, pkg, blobs = _add_scenario(tmp_path)
    item = _bus_item(base, "exec/2b/full", manifest_bytes, blobs)

    def opener(*a, **k):
        raise AssertionError("a PR was opened outside GitHub Actions")

    res = execute_2b.execute(bus_items=[item], actor=execute_2b.ALLOWED_ACTOR, repo=repo,
                             remote="origin", current_base=base, env={}, pr_opener=opener,
                             do_push=False)
    assert res["ok"] is True and res["pr"] is None and res["pushed"] is False
    assert res["branch"] == "exec/2b/full"


def test_execute_opens_draft_pr_under_actions(tmp_path):
    repo, base, manifest_bytes, pkg, blobs = _add_scenario(tmp_path)
    origin = _bare_remote(tmp_path)
    _attach_origin(repo, origin)
    item = _bus_item(base, "exec/2b/withpr", manifest_bytes, blobs)
    seen = {}

    def opener(repo_slug, payload):
        seen.update(payload)
        return {"number": 1, "draft": True}

    res = execute_2b.execute(bus_items=[item], actor=execute_2b.ALLOWED_ACTOR, repo=repo,
                             remote="origin", current_base=base, repo_slug="owner/repo",
                             env={"GITHUB_ACTIONS": "true"}, pr_opener=opener, do_push=True)
    assert res["pushed"] is True
    assert seen["draft"] is True and seen["head"] == "exec/2b/withpr"
    assert _git(origin, "rev-parse", "--verify", "exec/2b/withpr").strip()


# --------------------------------------------------------------------------- #
# One-click discovery: GitHub issues/comments, GET-only, no manual courier
# --------------------------------------------------------------------------- #
def test_parse_bus_envelope_extracts_fenced_block():
    env = {"kind": "cowork-patch", "branch": "exec/2b/x"}
    body = "intro text\n```cowork-patch\n" + json.dumps(env) + "\n```\ntrailing"
    assert execute_2b.parse_bus_envelope(body) == env


def test_parse_bus_envelope_ignores_unrelated_bodies():
    assert execute_2b.parse_bus_envelope("just a normal comment") is None
    assert execute_2b.parse_bus_envelope("```json\n{\"kind\":\"other\"}\n```") is None


def test_discover_bus_items_reads_issues_and_comments_via_token():
    # Inject a fake GET layer; assert discovery reads issues + comments and that the
    # token threads through every call. No real network (conftest blocks sockets).
    env = {"kind": "cowork-patch", "consumed": False, "branch": "exec/2b/from-comment"}
    calls = []

    def fake_get(url, token):
        calls.append((url, token))
        if "/issues?" in url and "page=1" in url:
            return [{"number": 5, "user": {"login": "human"}, "body": "please apply"}]
        if "/issues?" in url:
            return []
        if "/issues/5/comments" in url and "page=1" in url:
            return [{"user": {"login": execute_2b.TRUSTED_RELAY_AUTHOR},
                     "body": "```cowork-patch\n" + json.dumps(env) + "\n```"}]
        return []

    items = execute_2b.discover_bus_items("owner/repo", "secret-token", getter=fake_get)
    assert len(items) == 1
    assert items[0]["author"] == execute_2b.TRUSTED_RELAY_AUTHOR
    assert items[0]["envelope"]["branch"] == "exec/2b/from-comment"
    assert calls, "discovery must issue GitHub GET calls"
    assert all(tok == "secret-token" for _, tok in calls)


def test_discover_bus_items_skips_pull_requests():
    def fake_get(url, token):
        if "/issues?" in url and "page=1" in url:
            return [{"number": 9, "user": {"login": "x"}, "body": "x",
                     "pull_request": {"url": "..."}}]
        return []

    assert execute_2b.discover_bus_items("o/r", "t", getter=fake_get) == []


# --------------------------------------------------------------------------- #
# End-to-end happy path (local remote; offline)
# --------------------------------------------------------------------------- #
def test_execute_end_to_end_pushes_exec_branch_only(tmp_path):
    repo, base, manifest_bytes, pkg, blobs = _add_scenario(tmp_path)
    origin = _bare_remote(tmp_path)
    _attach_origin(repo, origin)
    main_before = _git(origin, "rev-parse", "main").strip()

    item = _bus_item(base, "exec/2b/e2e", manifest_bytes, blobs)
    res = execute_2b.execute(bus_items=[item], actor=execute_2b.ALLOWED_ACTOR, repo=repo,
                             remote="origin", current_base=base, env={}, do_push=True)

    assert res["ok"] is True and res["pushed"] is True
    assert _git(origin, "rev-parse", "--verify", "exec/2b/e2e").strip()
    # main on the remote was never touched.
    assert _git(origin, "rev-parse", "main").strip() == main_before


# --------------------------------------------------------------------------- #
# Static audits over execute_2b.py
# --------------------------------------------------------------------------- #
def _docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def _code_strings(tree):
    skip = _docstring_ids(tree)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip]


def _attr_names(tree):
    return {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}


def _ident_names(tree):
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def test_static_no_merge_call():
    # No PR/branch merge anywhere: not as an endpoint string, not as a call/attr.
    for s in _code_strings(TREE):
        assert "merge" not in s.lower(), f"merge token in code string: {s!r}"
    for name in _attr_names(TREE) | _ident_names(TREE):
        assert "merge" not in name.lower(), f"merge in identifier: {name!r}"


def test_static_no_ruleset_settings_or_protection_endpoints():
    forbidden = ("ruleset", "protection", "/branches", "/settings",
                 "branch_protection", "/admin", "/secrets", "/environments")
    for s in _code_strings(TREE):
        low = s.lower()
        for tok in forbidden:
            assert tok not in low, f"forbidden endpoint token {tok!r} in code string: {s!r}"


def test_static_no_force_push():
    forbidden = ("--force", "force-with-lease", "force_with_lease", " -f ", "+refs/")
    for s in _code_strings(TREE):
        low = s.lower()
        for tok in forbidden:
            assert tok not in low, f"force-push token {tok!r} in code string: {s!r}"
    for name in _attr_names(TREE) | _ident_names(TREE):
        assert "force" not in name.lower(), f"force in identifier: {name!r}"


def test_static_no_file_bus_dependency():
    # The one-click design must not reintroduce a file/courier bus input.
    for tok in ("COWORK_BUS_FILE", "_load_bus_items", "bus_file"):
        assert tok not in SRC, f"file-bus dependency reintroduced: {tok!r}"


def test_pinned_identity_strings_are_embedded():
    assert "kraken-cowork-relay[bot]" in SRC
    assert "gholamitoghchimajid-arch" in SRC


# --------------------------------------------------------------------------- #
# Static audits over the 2b workflow
#
# Audits inspect the ACTUAL YAML config, not the documentation: full-line `#`
# comments are stripped first, so a comment that merely names a forbidden scope to
# say it is NOT granted cannot trip -- only a real grant can.
# --------------------------------------------------------------------------- #
def _workflow_text():
    assert WORKFLOW_PATH.is_file(), f"workflow missing: {WORKFLOW_PATH}"
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _workflow_config():
    """Workflow YAML with full-line comments removed."""
    lines = [ln for ln in _workflow_text().splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines)


def test_workflow_present_and_dispatch_only():
    cfg = _workflow_config()
    assert "workflow_dispatch" in cfg
    assert not re.search(r"^on:\s*\n\s*push:", cfg, re.MULTILINE), "must be manual-only"
    assert "schedule:" not in cfg


def test_workflow_requires_no_manual_input():
    cfg = _workflow_config()
    # One-click: no inputs block at all -> no `required:` anywhere.
    assert "inputs:" not in cfg, "workflow must require no manual input (one-click)"
    assert "required:" not in cfg


def test_workflow_has_issues_read_and_no_issues_write():
    cfg = _workflow_config()
    assert re.search(r"^\s*issues:\s*read\s*$", cfg, re.MULTILINE), "needs issues: read for discovery"
    assert not re.search(r"issues:\s*write", cfg), "must NOT grant issues: write"


def test_workflow_least_privilege_permissions():
    cfg = _workflow_config()
    assert re.search(r"^\s*contents:\s*write\s*$", cfg, re.MULTILINE)
    assert re.search(r"^\s*pull-requests:\s*write\s*$", cfg, re.MULTILINE)
    # No admin/settings/ruleset/branch-protection style permissions in the config.
    for forbidden in ("administration:", "ruleset", "protection", "secrets:", "environments:"):
        assert forbidden not in cfg, f"workflow grants forbidden scope: {forbidden!r}"


def test_workflow_pins_allowed_actor():
    # The actor pin is a real `if:` guard, so it survives comment stripping.
    assert "gholamitoghchimajid-arch" in _workflow_config(), "workflow must hard-gate the allowed actor"
