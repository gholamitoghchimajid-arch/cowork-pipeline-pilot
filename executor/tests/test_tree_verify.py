# executor/tests/test_tree_verify.py
# PR-A tree-verification tests for executor/patch_2b.py.
#
# compute_and_verify must reproduce the post-apply tree and compare it to the
# manifest's expected_tree_sha: a correct value passes (returns the tree id), a
# wrong value fails closed with E_TREE_MISMATCH. The correct tree is first
# computed with compute_tree_unverified. Because apply mutates a repo's index,
# each compute uses its own fresh (deterministic) temp repo.
#
# pytest + stdlib only. No network, no fetch, no push, no PR, no GitHub API,
# no token. (`import patch_2b` / `from util...` resolve via conftest.py.)
import hashlib
import tempfile
from pathlib import Path

import pytest

import patch_2b
from patch_2b import ManifestError
from util.repo_builder import build_base_repo

BLOB = b"2b pilot no-op\n"
CSHA = hashlib.sha256(BLOB).hexdigest()
ADD_PATH = "pilot/noop-2b.txt"


def _manifest(base_commit: str, expected_tree_sha: str) -> bytes:
    return ("\n".join([
        "schema: 2b-patch/1.0",
        f"base_commit: {base_commit}",
        "operations:",
        "  - op: add",
        f"    path: {ADD_PATH}",
        f"    content_sha256: {CSHA}",
        '    mode: "100644"',
        f"expected_tree_sha: {expected_tree_sha}",
    ]) + "\n").encode("utf-8")


def _fresh(tmp_path: Path):
    """A fresh, deterministic base repo + package (with the add blob present)."""
    workdir = Path(tempfile.mkdtemp(dir=str(tmp_path)))
    repo = workdir / "repo"
    base = build_base_repo(repo, {"README.md": b"base\n"})
    blobs = workdir / "package" / "patches" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / CSHA).write_bytes(BLOB)
    return repo, blobs.parent.parent, base   # repo, package_dir, base_commit


def test_correct_expected_tree_passes(tmp_path):
    # Compute the correct tree on one fresh repo.
    repo, pkg, base = _fresh(tmp_path)
    correct = patch_2b.compute_tree_unverified(repo, _manifest(base, "0" * 40), pkg)
    assert len(correct) == 40

    # Verify on a separate fresh repo (apply above mutated the first index).
    repo2, pkg2, base2 = _fresh(tmp_path)
    result = patch_2b.compute_and_verify(repo2, _manifest(base2, correct), pkg2)
    assert result == correct


def test_wrong_expected_tree_fails(tmp_path):
    repo, pkg, base = _fresh(tmp_path)
    wrong = "f" * 40                                   # format-valid 40-hex, not the real tree
    with pytest.raises(ManifestError) as ei:
        patch_2b.compute_and_verify(repo, _manifest(base, wrong), pkg)
    assert ei.value.code == "E_TREE_MISMATCH"
