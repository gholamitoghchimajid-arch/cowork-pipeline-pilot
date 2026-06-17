# executor/patch_2b.py
# Component 2b -- PURE LOCAL deterministic core. NO network, NO push, NO PR, NO token,
# NO GitHub API. Implements PATCH_MANIFEST_SCHEMA_SPEC.md v1.0:
# parse -> validate -> blob-verify -> deterministic apply -> tree verify.
# Tree ids come from local `git write-tree` (git is the authoritative SHA-1 oracle).
#
# PARSER NOTE (intentional): the manifest parser is a deliberately NARROWER,
# implementation-level strict subset of the (already strict) schema grammar:
# fixed indentation, exactly one document, no BOM, no blank lines, no trailing
# whitespace, bare tokens limited to [A-Za-z0-9._/-], `mode` double-quoted only.
# Rejection-by-construction: anything outside the grammar fails closed before any
# git/apply stage. It is a stricter-than-spec acceptor, never a looser one.
#
# DEPENDENCIES: Python stdlib only (no PyYAML) plus the local `git` binary used
# for local plumbing only (rev-parse, ls-tree, cat-file, hash-object,
# update-index, write-tree). There is no fetch/push/PR/token code path here.
from __future__ import annotations
import hashlib
import re
import subprocess
from pathlib import Path

SCHEMA_ID = "2b-patch/1.0"
ALLOWED_MODES = ("100644", "100755")
REGULAR_MODES = ("100644", "100755")
TOP_KEYS = ("schema", "base_commit", "operations", "expected_tree_sha")
OP_FIELD_KEYS = ("path", "content_sha256", "mode")
FORBIDDEN_PREFIXES = ("patches/", ".git/", ".github/workflows/")

_HEX40 = re.compile(r"\A[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_BARE  = re.compile(r"\A[A-Za-z0-9._/-]+\Z")
_DQ    = re.compile(r'\A"([\x21\x23-\x5b\x5d-\x7e]*)"\Z')   # printable ASCII, no space/" /\
_SEG   = re.compile(r"\A[A-Za-z0-9._-]+\Z")
_KEY   = r"([a-z0-9_]+)"                                    # keys may contain digits (content_sha256)


class ManifestError(Exception):
    def __init__(self, code: str, msg: str):
        self.code = code
        super().__init__(f"{code}: {msg}")


# --------------------------------------------------------------------------- #
# Parsing (strict YAML subset)
# --------------------------------------------------------------------------- #
def _scalar(text: str):
    """Return ('dq', inner) for a double-quoted string or ('bare', text) for a
    bareword. Anything else (anchors, aliases, tags, spaces, etc.) is rejected."""
    m = _DQ.match(text)
    if m:
        return ("dq", m.group(1))
    if _BARE.match(text):
        return ("bare", text)
    raise ManifestError("E_SCALAR", f"invalid scalar: {text!r}")


def parse_manifest(raw: bytes) -> dict:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError("E_UTF8", "manifest is not valid UTF-8")
    if text[:1] == "\ufeff":
        raise ManifestError("E_BOM", "manifest must not start with a BOM")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for ch in text:
        if ch != "\n" and (ord(ch) < 0x20 or ord(ch) == 0x7f):
            raise ManifestError("E_CONTROL", "control character in manifest")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()                       # allow exactly one trailing newline
    if any(ln == "" for ln in lines):
        raise ManifestError("E_BLANK", "blank lines are not allowed")
    if not lines:
        raise ManifestError("E_EMPTY", "empty manifest")

    top: dict = {}
    ops: list = []
    seen_top: set = set()
    in_ops = False
    cur: dict | None = None

    def finalize():
        nonlocal cur
        if cur is not None:
            cur.pop("_fields", None)
            ops.append(cur)
            cur = None

    for ln in lines:
        m_item  = re.match(r"\A  - op: (.+)\Z", ln)
        m_field = re.match(r"\A    " + _KEY + r": (.+)\Z", ln)
        is_ops  = ln == "operations:"
        m_top   = re.match(r"\A" + _KEY + r": (.+)\Z", ln)

        if is_ops:
            if "operations" in seen_top:
                raise ManifestError("E_DUPKEY", "duplicate key: operations")
            seen_top.add("operations")
            in_ops = True
            cur = None
            continue
        if m_item:
            if not in_ops:
                raise ManifestError("E_SYNTAX", "operation item outside operations")
            finalize()
            _, val = _scalar(m_item.group(1))
            cur = {"op": val, "_fields": {"op"}}
            continue
        if m_field and in_ops and cur is not None:
            key = m_field.group(1)
            if key not in OP_FIELD_KEYS:
                raise ManifestError("E_UNKNOWN_FIELD", f"unknown op field: {key}")
            if key in cur["_fields"]:
                raise ManifestError("E_DUPKEY", f"duplicate op field: {key}")
            kind, val = _scalar(m_field.group(2))
            if key == "mode" and kind != "dq":
                raise ManifestError("E_MODE_UNQUOTED", "mode must be a quoted string")
            cur[key] = val
            cur["_fields"].add(key)
            continue
        if m_top:
            key = m_top.group(1)
            if key == "operations":
                raise ManifestError("E_SYNTAX", "operations must not have an inline value")
            if key not in ("schema", "base_commit", "expected_tree_sha"):
                raise ManifestError("E_UNKNOWN_KEY", f"unknown top-level key: {key}")
            if in_ops:
                finalize()
                in_ops = False
            if key in seen_top:
                raise ManifestError("E_DUPKEY", f"duplicate key: {key}")
            seen_top.add(key)
            _, val = _scalar(m_top.group(2))
            top[key] = val
            continue
        raise ManifestError("E_SYNTAX", f"unparseable line: {ln!r}")

    if in_ops:
        finalize()
    for k in TOP_KEYS:
        if k not in seen_top:
            raise ManifestError("E_MISSING", f"missing top-level key: {k}")
    if not ops:
        raise ManifestError("E_EMPTY_OPS", "operations must be non-empty")
    return {"schema": top["schema"], "base_commit": top["base_commit"],
            "operations": ops, "expected_tree_sha": top["expected_tree_sha"]}


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #
def _validate_path(p: str) -> None:
    if p.startswith("/") or p.endswith("/"):
        raise ManifestError("E_PATH", "leading/trailing slash")
    for s in p.split("/"):
        if s == "":
            raise ManifestError("E_PATH", "empty segment")
        if s in (".", ".."):
            raise ManifestError("E_PATH", "dot segment")
        if not _SEG.match(s):
            raise ManifestError("E_PATH_CHAR", f"illegal char in segment: {s!r}")
    for pre in FORBIDDEN_PREFIXES:
        if (p + "/").startswith(pre):
            raise ManifestError("E_PATH_FORBIDDEN", f"forbidden prefix: {pre}")


def validate_schema(m: dict) -> None:
    if m["schema"] != SCHEMA_ID:
        raise ManifestError("E_SCHEMA", "unsupported schema id")
    if not _HEX40.match(m["base_commit"]):
        raise ManifestError("E_BASE_HEX", "base_commit not 40-hex lowercase")
    if not _HEX40.match(m["expected_tree_sha"]):
        raise ManifestError("E_TREE_HEX", "expected_tree_sha not 40-hex lowercase")
    seen: set = set()
    for op in m["operations"]:
        kind = op["op"]
        if kind not in ("add", "replace", "delete"):
            raise ManifestError("E_OP", f"unknown op: {kind}")
        if "path" not in op:
            raise ManifestError("E_MISSING_FIELD", "op missing path")
        _validate_path(op["path"])
        if op["path"] in seen:
            raise ManifestError("E_DUP_PATH", f"duplicate path: {op['path']}")
        seen.add(op["path"])
        if kind in ("add", "replace"):
            if "content_sha256" not in op or "mode" not in op:
                raise ManifestError("E_MISSING_FIELD", f"{kind} requires content_sha256 and mode")
            if not _HEX64.match(op["content_sha256"]):
                raise ManifestError("E_CONTENT_HEX", "content_sha256 not 64-hex lowercase")
            if op["mode"] not in ALLOWED_MODES:
                raise ManifestError("E_MODE", "mode not 100644/100755")
        else:  # delete
            if "content_sha256" in op or "mode" in op:
                raise ManifestError("E_FORBIDDEN_FIELD", "delete must not carry content_sha256/mode")
    allp = sorted(seen)
    for a in allp:
        for b in allp:
            if a != b and b.startswith(a + "/"):
                raise ManifestError("E_PATH_NESTED", f"path {a} is a parent of {b}")


# --------------------------------------------------------------------------- #
# Local git helpers (no network, no fetch/push/PR)
# --------------------------------------------------------------------------- #
def _git(repo_dir, *args, input_bytes=None) -> str:
    r = subprocess.run(("git", "-C", str(repo_dir)) + args,
                       input=input_bytes, capture_output=True)
    if r.returncode != 0:
        raise ManifestError("E_GIT", f"git {' '.join(args)}: {r.stderr.decode('utf-8','replace')}")
    return r.stdout.decode("utf-8")


def require_sha1(repo_dir) -> None:
    fmt = _git(repo_dir, "rev-parse", "--show-object-format").strip()
    if fmt != "sha1":
        raise ManifestError("E_OBJFMT", f"object format must be sha1, got {fmt}")


def verify_base(repo_dir, base_commit) -> None:
    # PR-A: base is the local detached HEAD the harness checked out.
    # (Fetching origin/main is PR-B; this only confirms HEAD == base_commit.)
    head = _git(repo_dir, "rev-parse", "HEAD").strip()
    if head != base_commit:
        raise ManifestError("E_BASE_DRIFT", f"HEAD {head} != base_commit {base_commit}")


def _tree_entry(repo_dir, base, path):
    """Return (mode, type) for base:path, or None if absent.
    Uses `git ls-tree` so symlinks (120000 blob) and submodules (160000 commit)
    are distinguishable from regular files (100644/100755 blob) and trees
    (040000 tree). `cat-file -t` alone cannot make this distinction."""
    r = subprocess.run(("git", "-C", str(repo_dir), "ls-tree", "--full-tree", base, "--", path),
                       capture_output=True)
    if r.returncode != 0:
        raise ManifestError("E_GIT", f"git ls-tree: {r.stderr.decode('utf-8','replace')}")
    out = r.stdout.decode("utf-8")
    if not out.strip():
        return None
    meta = out.splitlines()[0].split("\t", 1)[0]   # "<mode> <type> <oid>"
    mode, typ, _oid = meta.split()
    return (mode, typ)


def _classify(repo_dir, base, path):
    """None=absent, 'tree'=directory, 'regular'=100644/100755 blob,
    'badmode'=symlink/submodule/any other non-regular entry."""
    ent = _tree_entry(repo_dir, base, path)
    if ent is None:
        return None
    mode, typ = ent
    if typ == "tree":
        return "tree"
    if typ == "blob" and mode in REGULAR_MODES:
        return "regular"
    return "badmode"


def _assert_parents_are_trees(repo_dir, base, path) -> None:
    """Every existing parent prefix of an add target must be a directory.
    A parent that is a regular file, symlink, or submodule is rejected."""
    parts = path.split("/")
    for i in range(1, len(parts)):
        prefix = "/".join(parts[:i])
        cls = _classify(repo_dir, base, prefix)
        if cls is None or cls == "tree":
            continue                              # absent (will be created) or already a tree: OK
        raise ManifestError("E_PARENT_NOT_TREE", f"parent prefix is not a tree: {prefix}")


# --------------------------------------------------------------------------- #
# Blob verification + deterministic apply
# --------------------------------------------------------------------------- #
def _hash_blob(repo_dir, blobs_dir, op) -> str:
    bp = Path(blobs_dir) / op["content_sha256"]
    if not bp.is_file():
        raise ManifestError("E_BLOB_MISSING", f"blob not found: {op['content_sha256']}")
    data = bp.read_bytes()
    if hashlib.sha256(data).hexdigest() != op["content_sha256"]:
        raise ManifestError("E_BLOB_HASH", f"blob hash mismatch: {op['content_sha256']}")
    return _git(repo_dir, "hash-object", "-w", "--stdin", input_bytes=data).strip()


def apply_operations(repo_dir, manifest, package_dir) -> None:
    base = manifest["base_commit"]
    blobs = Path(package_dir) / "patches" / "blobs"
    for op in manifest["operations"]:
        kind, path = op["op"], op["path"]
        cls = _classify(repo_dir, base, path)
        if kind == "add":
            if cls == "tree":
                raise ManifestError("E_TARGET_IS_TREE", f"add target exists as directory: {path}")
            if cls == "regular":
                raise ManifestError("E_ADD_EXISTS", f"add target exists as file: {path}")
            if cls == "badmode":
                raise ManifestError("E_TARGET_BAD_MODE", f"add target is symlink/submodule/non-regular: {path}")
            _assert_parents_are_trees(repo_dir, base, path)     # cls is None (absent)
            oid = _hash_blob(repo_dir, blobs, op)
            _git(repo_dir, "update-index", "--add", "--cacheinfo", f"{op['mode']},{oid},{path}")
        elif kind == "replace":
            if cls is None:
                raise ManifestError("E_REPLACE_MISSING", f"replace target missing: {path}")
            if cls == "tree":
                raise ManifestError("E_TARGET_IS_TREE", f"replace target is a directory: {path}")
            if cls == "badmode":
                raise ManifestError("E_TARGET_BAD_MODE", f"replace target is symlink/submodule/non-regular: {path}")
            new_oid = _hash_blob(repo_dir, blobs, op)
            old_oid = _git(repo_dir, "rev-parse", f"{base}:{path}").strip()
            if new_oid == old_oid:
                raise ManifestError("E_NOOP_REPLACE", f"replace is a no-op: {path}")
            _git(repo_dir, "update-index", "--add", "--cacheinfo", f"{op['mode']},{new_oid},{path}")
        else:  # delete
            if cls is None:
                raise ManifestError("E_DELETE_MISSING", f"delete target missing: {path}")
            if cls == "tree":
                raise ManifestError("E_TARGET_IS_TREE", f"delete target is a directory: {path}")
            if cls == "badmode":
                raise ManifestError("E_TARGET_BAD_MODE", f"delete target is symlink/submodule/non-regular: {path}")
            _git(repo_dir, "update-index", "--force-remove", path)


def write_tree(repo_dir) -> str:
    return _git(repo_dir, "write-tree").strip()


# --------------------------------------------------------------------------- #
# Local-only entry points (no branch, no network, no push)
# --------------------------------------------------------------------------- #
def compute_tree_unverified(repo_dir, manifest_bytes, package_dir) -> str:
    """Parse -> validate -> base check -> apply -> write-tree. Returns the tree id
    without comparing to expected_tree_sha (used by the dual-oracle compute step)."""
    require_sha1(repo_dir)
    m = parse_manifest(manifest_bytes)
    validate_schema(m)
    verify_base(repo_dir, m["base_commit"])
    apply_operations(repo_dir, m, package_dir)
    return write_tree(repo_dir)


def compute_and_verify(repo_dir, manifest_bytes, package_dir) -> str:
    """Full local pipeline; asserts the resulting tree equals expected_tree_sha."""
    require_sha1(repo_dir)
    m = parse_manifest(manifest_bytes)
    validate_schema(m)
    verify_base(repo_dir, m["base_commit"])
    apply_operations(repo_dir, m, package_dir)
    tree = write_tree(repo_dir)
    if tree != m["expected_tree_sha"]:
        raise ManifestError("E_TREE_MISMATCH", f"tree {tree} != expected {m['expected_tree_sha']}")
    return tree
