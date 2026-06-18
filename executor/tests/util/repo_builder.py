# executor/tests/util/repo_builder.py
# PR-A test helper -- deterministic temp git-repo builders. NOT part of the core.
#
# All entries are staged via git PLUMBING (hash-object + update-index --cacheinfo)
# so that file modes are EXACT and independent of OS/filesystem permission
# behavior: regular files (100644/100755), symlinks (120000), and submodule
# gitlinks (160000) are produced as true tree entries without ever touching the
# working tree, os.symlink, or chmod. Commits use a fixed identity/date so commit
# ids are reproducible.
#
# No network, no fetch, no push, no PR, no GitHub API, no token. Stdlib only
# (os, subprocess, pathlib) plus the local `git` binary for local plumbing.
from __future__ import annotations
import os
import subprocess
from pathlib import Path

FIXED_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00 +0000",
}

ALLOWED_BASE_MODES = ("100644", "100755")


def _env() -> dict:
    return {**os.environ, **FIXED_ENV}


def _run(dest: Path, *args, input_bytes: bytes | None = None) -> bytes:
    r = subprocess.run(("git", "-C", str(dest)) + args, env=_env(),
                       input=input_bytes, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.decode('utf-8', 'replace')}")
    return r.stdout


def _init(dest: Path, object_format: str = "sha1") -> None:
    dest.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(("git", "init", "-b", "main", "--object-format", object_format, str(dest)),
                       env=_env(), capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git init failed: {r.stderr.decode('utf-8', 'replace')}")
    _run(dest, "config", "commit.gpgsign", "false")


def _hash_object(dest: Path, data: bytes) -> str:
    return _run(dest, "hash-object", "-w", "--stdin", input_bytes=data).decode("utf-8").strip()


def _cacheinfo(dest: Path, mode: str, oid: str, path: str) -> None:
    _run(dest, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{path}")


def _commit_base(dest: Path) -> str:
    _run(dest, "commit", "-m", "base")
    return _run(dest, "rev-parse", "HEAD").decode("utf-8").strip()


def _norm(value) -> tuple[bytes, str]:
    """Accept bytes (-> mode 100644) or (bytes, mode) with mode 100644/100755."""
    if isinstance(value, tuple):
        data, mode = value
    else:
        data, mode = value, "100644"
    if mode not in ALLOWED_BASE_MODES:
        raise ValueError(f"unsupported base file mode: {mode}")
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("base file content must be bytes")
    return bytes(data), mode


def build_base_repo(dest: Path, base_files: dict, object_format: str = "sha1") -> str:
    """Deterministic base repo from a mapping of relpath -> bytes (mode 100644) or
    relpath -> (bytes, mode) where mode is '100644' or '100755'. Every entry is
    staged via plumbing so modes are exact and permission-independent; a single
    'base' commit is made with fixed identity/date. Returns the commit id.
    No working-tree checkout is performed (the 2b core reads the object db and
    index, never the working tree), so no file materialization is required."""
    _init(dest, object_format)
    for rel in sorted(base_files):
        data, mode = _norm(base_files[rel])
        oid = _hash_object(dest, data)
        _cacheinfo(dest, mode, oid, rel)
    return _commit_base(dest)


def build_base_with_symlink(dest: Path, link_path: str, target: str = "README.md") -> str:
    """Base repo containing a real symlink tree entry (mode 120000) at link_path,
    created purely via git plumbing -- NO os.symlink, so it is deterministic and
    independent of OS/filesystem symlink support. A git symlink blob's content is
    the link target path bytes (no trailing newline). Returns the commit id."""
    _init(dest)
    readme_oid = _hash_object(dest, b"base\n")
    _cacheinfo(dest, "100644", readme_oid, "README.md")
    link_oid = _hash_object(dest, target.encode("utf-8"))   # symlink blob = target bytes
    _cacheinfo(dest, "120000", link_oid, link_path)
    return _commit_base(dest)


def build_base_with_gitlink(dest: Path, sub_path: str) -> str:
    """Base repo containing a submodule-style gitlink entry (mode 160000) at
    sub_path, without a real submodule and without any network: a throwaway commit
    object (empty tree, fixed identity) is created locally and the gitlink entry
    points at it. Returns the commit id."""
    _init(dest)
    readme_oid = _hash_object(dest, b"base\n")
    _cacheinfo(dest, "100644", readme_oid, "README.md")
    empty_tree = _run(dest, "hash-object", "-t", "tree", "-w", "--stdin",
                      input_bytes=b"").decode("utf-8").strip()
    gitlink_commit = _run(dest, "commit-tree", empty_tree, "-m", "sub").decode("utf-8").strip()
    _cacheinfo(dest, "160000", gitlink_commit, sub_path)
    return _commit_base(dest)
