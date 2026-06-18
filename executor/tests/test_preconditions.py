# executor/tests/test_preconditions.py
# PR-A apply/precondition tests for executor/patch_2b.py.
#
# Exercises the git tree-entry classification (_classify via `git ls-tree`) and
# the per-op preconditions through compute_tree_unverified on local temp repos:
# regular-file / tree / symlink(120000) / gitlink(160000) targets and parent
# prefixes are handled with explicit reason codes; happy paths produce a tree id.
#
# pytest + stdlib only. No network, no fetch, no push, no PR, no GitHub API,
# no token. (`import patch_2b` / `from util...` resolve via conftest.py.)
from __future__ import annotations
import hashlib
import tempfile
from pathlib import Path

import pytest

import patch_2b
from patch_2b import ManifestError
from util.repo_builder import (
    build_base_repo,
    build_base_with_symlink,
    build_base_with_gitlink,
)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


ADD_BLOB = b"hello\n"
ADD_CSHA = _sha(ADD_BLOB)
NEW_BLOB = b"new\n"
NEW_CSHA = _sha(NEW_BLOB)


def _is_hex40(s: str) -> bool:
    return len(s) == 40 and all(c in "0123456789abcdef" for c in s)


def _manifest(base_commit: str, ops: list) -> bytes:
    lines = ["schema: 2b-patch/1.0", f"base_commit: {base_commit}", "operations:"]
    for op in ops:
        lines.append(f"  - op: {op['op']}")
        lines.append(f"    path: {op['path']}")
        if op["op"] in ("add", "replace"):
            lines.append(f"    content_sha256: {op['content_sha256']}")
            lines.append(f'    mode: "{op["mode"]}"')
    lines.append(f"expected_tree_sha: {'0' * 40}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _package(tmp_path: Path, blobs: dict) -> Path:
    pkg = Path(tempfile.mkdtemp(dir=str(tmp_path), prefix="pkg_"))
    bd = pkg / "patches" / "blobs"
    bd.mkdir(parents=True)
    for csha, data in blobs.items():
        (bd / csha).write_bytes(data)
    return pkg


def _compute(tmp_path: Path, repo: Path, base: str, ops: list, blobs: dict | None = None) -> str:
    pkg = _package(tmp_path, blobs or {})
    return patch_2b.compute_tree_unverified(repo, _manifest(base, ops), pkg)


# --------------------------------------------------------------------------- #
# Existence preconditions
# --------------------------------------------------------------------------- #
def test_add_onto_existing_regular_file(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_repo(repo, {"README.md": b"base\n", "exists.txt": b"x\n"})
    with pytest.raises(ManifestError) as ei:
        _compute(tmp_path, repo, base,
                 [{"op": "add", "path": "exists.txt", "content_sha256": ADD_CSHA, "mode": "100644"}])
    assert ei.value.code == "E_ADD_EXISTS"


def test_replace_missing_path(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_repo(repo, {"README.md": b"base\n"})
    with pytest.raises(ManifestError) as ei:
        _compute(tmp_path, repo, base,
                 [{"op": "replace", "path": "nope.txt", "content_sha256": NEW_CSHA, "mode": "100644"}])
    assert ei.value.code == "E_REPLACE_MISSING"


def test_delete_missing_path(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_repo(repo, {"README.md": b"base\n"})
    with pytest.raises(ManifestError) as ei:
        _compute(tmp_path, repo, base, [{"op": "delete", "path": "nope.txt"}])
    assert ei.value.code == "E_DELETE_MISSING"


def test_replace_identical_content_is_noop(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_repo(repo, {"README.md": b"base\n", "data.txt": b"same\n"})
    same = b"same\n"
    csha = _sha(same)
    with pytest.raises(ManifestError) as ei:
        _compute(tmp_path, repo, base,
                 [{"op": "replace", "path": "data.txt", "content_sha256": csha, "mode": "100644"}],
                 {csha: same})
    assert ei.value.code == "E_NOOP_REPLACE"


# --------------------------------------------------------------------------- #
# Tree / directory targets
# --------------------------------------------------------------------------- #
def test_replace_target_is_tree(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_repo(repo, {"README.md": b"base\n", "dir/x.txt": b"x\n"})
    with pytest.raises(ManifestError) as ei:
        _compute(tmp_path, repo, base,
                 [{"op": "replace", "path": "dir", "content_sha256": NEW_CSHA, "mode": "100644"}])
    assert ei.value.code == "E_TARGET_IS_TREE"


def test_delete_target_is_tree(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_repo(repo, {"README.md": b"base\n", "dir/x.txt": b"x\n"})
    with pytest.raises(ManifestError) as ei:
        _compute(tmp_path, repo, base, [{"op": "delete", "path": "dir"}])
    assert ei.value.code == "E_TARGET_IS_TREE"


def test_add_onto_tree(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_repo(repo, {"README.md": b"base\n", "dir/x.txt": b"x\n"})
    with pytest.raises(ManifestError) as ei:
        _compute(tmp_path, repo, base,
                 [{"op": "add", "path": "dir", "content_sha256": ADD_CSHA, "mode": "100644"}])
    assert ei.value.code == "E_TARGET_IS_TREE"


def test_add_parent_is_regular_file(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_repo(repo, {"README.md": b"base\n", "a": b"i am a file\n"})
    with pytest.raises(ManifestError) as ei:
        _compute(tmp_path, repo, base,
                 [{"op": "add", "path": "a/b.txt", "content_sha256": ADD_CSHA, "mode": "100644"}])
    assert ei.value.code == "E_PARENT_NOT_TREE"


# --------------------------------------------------------------------------- #
# Symlink (120000) targets
# --------------------------------------------------------------------------- #
def test_replace_symlink_target(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_with_symlink(repo, "link")
    with pytest.raises(ManifestError) as ei:
        _compute(tmp_path, repo, base,
                 [{"op": "replace", "path": "link", "content_sha256": NEW_CSHA, "mode": "100644"}])
    assert ei.value.code == "E_TARGET_BAD_MODE"


def test_delete_symlink_target(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_with_symlink(repo, "link")
    with pytest.raises(ManifestError) as ei:
        _compute(tmp_path, repo, base, [{"op": "delete", "path": "link"}])
    assert ei.value.code == "E_TARGET_BAD_MODE"


def test_add_onto_symlink_target(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_with_symlink(repo, "link")
    with pytest.raises(ManifestError) as ei:
        _compute(tmp_path, repo, base,
                 [{"op": "add", "path": "link", "content_sha256": ADD_CSHA, "mode": "100644"}])
    assert ei.value.code == "E_TARGET_BAD_MODE"


def test_add_parent_is_symlink(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_with_symlink(repo, "lnk")
    with pytest.raises(ManifestError) as ei:
        _compute(tmp_path, repo, base,
                 [{"op": "add", "path": "lnk/child.txt", "content_sha256": ADD_CSHA, "mode": "100644"}])
    assert ei.value.code == "E_PARENT_NOT_TREE"


# --------------------------------------------------------------------------- #
# Submodule / gitlink (160000) targets
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("op", [
    {"op": "replace", "path": "sub", "content_sha256": ADD_CSHA, "mode": "100644"},
    {"op": "delete", "path": "sub"},
    {"op": "add", "path": "sub", "content_sha256": ADD_CSHA, "mode": "100644"},
])
def test_gitlink_target_bad_mode(tmp_path, op):
    repo = tmp_path / "repo"
    base = build_base_with_gitlink(repo, "sub")
    with pytest.raises(ManifestError) as ei:
        _compute(tmp_path, repo, base, [op])
    assert ei.value.code == "E_TARGET_BAD_MODE"


# --------------------------------------------------------------------------- #
# _classify
# --------------------------------------------------------------------------- #
def test_classify_returns_expected_kinds(tmp_path):
    r_reg = tmp_path / "reg"
    base_reg = build_base_repo(r_reg, {"README.md": b"base\n",
                                       "run.sh": (b"#!/bin/sh\n", "100755")})
    assert patch_2b._classify(r_reg, base_reg, "README.md") == "regular"   # 100644
    assert patch_2b._classify(r_reg, base_reg, "run.sh") == "regular"      # 100755
    assert patch_2b._classify(r_reg, base_reg, "absent.txt") is None

    r_tree = tmp_path / "tree"
    base_tree = build_base_repo(r_tree, {"README.md": b"base\n", "dir/x.txt": b"x\n"})
    assert patch_2b._classify(r_tree, base_tree, "dir") == "tree"

    r_sym = tmp_path / "sym"
    base_sym = build_base_with_symlink(r_sym, "link")
    assert patch_2b._classify(r_sym, base_sym, "link") == "badmode"        # 120000

    r_sub = tmp_path / "sub"
    base_sub = build_base_with_gitlink(r_sub, "sub")
    assert patch_2b._classify(r_sub, base_sub, "sub") == "badmode"         # 160000


# --------------------------------------------------------------------------- #
# Happy preconditions -> a 40-hex tree id
# --------------------------------------------------------------------------- #
def test_happy_add_produces_tree(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_repo(repo, {"README.md": b"base\n"})
    tree = _compute(tmp_path, repo, base,
                    [{"op": "add", "path": "pilot/noop-2b.txt", "content_sha256": ADD_CSHA, "mode": "100644"}],
                    {ADD_CSHA: ADD_BLOB})
    assert _is_hex40(tree)


def test_happy_replace_produces_tree(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_repo(repo, {"README.md": b"base\n", "data.txt": b"old\n"})
    tree = _compute(tmp_path, repo, base,
                    [{"op": "replace", "path": "data.txt", "content_sha256": NEW_CSHA, "mode": "100644"}],
                    {NEW_CSHA: NEW_BLOB})
    assert _is_hex40(tree)


def test_happy_delete_produces_tree(tmp_path):
    repo = tmp_path / "repo"
    base = build_base_repo(repo, {"README.md": b"base\n", "drop.txt": b"bye\n"})
    tree = _compute(tmp_path, repo, base, [{"op": "delete", "path": "drop.txt"}])
    assert _is_hex40(tree)
