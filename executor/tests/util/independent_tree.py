# executor/tests/util/independent_tree.py
# PR-A Oracle 2 -- INDEPENDENT tree re-derivation. Deliberately does NOT import
# patch_2b. It computes the intended final tree by materializing the end-state on
# the filesystem and running `git add -A` + `git write-tree` -- a different
# application path from the core's index/cacheinfo logic. Agreement between this
# oracle and patch_2b proves the core's apply produces the intended tree, so
# patch_2b is never the sole oracle for its own expected tree ids.
#
# No network, no fetch, no push, no PR, no GitHub API, no token. Stdlib only
# (os, subprocess, pathlib) plus the local `git` binary for local plumbing.
from __future__ import annotations
import os
import subprocess
from pathlib import Path

try:
    from util.repo_builder import FIXED_ENV   # when executor/tests is on sys.path
except ImportError:                            # when executor/tests/util is on sys.path
    from repo_builder import FIXED_ENV

_REGULAR_MODES = ("100644", "100755")


def _env() -> dict:
    return {**os.environ, **FIXED_ENV}


def _run(work: Path, *args) -> bytes:
    r = subprocess.run(("git", "-C", str(work)) + args, env=_env(), capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.decode('utf-8', 'replace')}")
    return r.stdout


def _norm(value) -> tuple[bytes, str]:
    """Accept bytes (-> mode 100644) or (bytes, mode) with mode 100644/100755."""
    if isinstance(value, tuple):
        data, mode = value
    else:
        data, mode = value, "100644"
    if mode not in _REGULAR_MODES:
        raise ValueError(f"unsupported file mode: {mode}")
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("file content must be bytes")
    return bytes(data), mode


def _materialize(work: Path, rel: str, data: bytes, mode: str) -> None:
    p = work / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.chmod(p, 0o755 if mode == "100755" else 0o644)


def independent_tree_id(base_files: dict, operations: list, blobs: dict, dest: Path) -> str:
    """Compute the post-apply git tree id INDEPENDENTLY of patch_2b.

    base_files : relpath -> bytes (mode 100644) or (bytes, mode) 100644/100755
    operations : list of {op, path, [content_sha256], [mode]} (schema-shaped)
    blobs      : content_sha256 -> raw bytes
    dest       : a fresh temp directory to work in

    Lays the base tree on disk, applies the operations by plain filesystem
    manipulation (write blob bytes for add/replace, unlink for delete), then
    stages everything with `git add -A` and returns `git write-tree`. Uses
    core.fileMode=true so a 100755 bit is recorded. This is a deliberately
    different application path from the core's index/cacheinfo apply."""
    work = dest / "work"
    work.mkdir(parents=True)
    subprocess.run(("git", "init", "-b", "main", str(work)),
                   env=_env(), capture_output=True, check=True)
    _run(work, "config", "core.fileMode", "true")

    for rel in sorted(base_files):
        data, mode = _norm(base_files[rel])
        _materialize(work, rel, data, mode)

    for op in operations:
        kind, path = op["op"], op["path"]
        if kind in ("add", "replace"):
            _materialize(work, path, blobs[op["content_sha256"]], op["mode"])
        elif kind == "delete":
            (work / path).unlink()
        else:
            raise ValueError(f"unsupported op for independent oracle: {kind}")

    _run(work, "add", "-A")
    return _run(work, "write-tree").decode("utf-8").strip()
