# executor/tests/test_blob_verify.py
# PR-A blob-verification tests for executor/patch_2b.py.
#
# The core loads each add/replace blob from
#   <package>/patches/blobs/<content_sha256>
# and verifies that sha256(raw_bytes) == content_sha256 before staging. These
# tests exercise that through compute_tree_unverified (the smallest local entry
# point that reaches the apply/blob-verify stage). Local temp repos only.
#
# pytest + stdlib only. No network, no fetch, no push, no PR, no GitHub API,
# no token. (`import patch_2b` / `from util...` resolve via conftest.py.)
import hashlib
from pathlib import Path

import pytest

import patch_2b
from patch_2b import ManifestError
from util.repo_builder import build_base_repo

BLOB = b"2b pilot no-op\n"
CSHA = hashlib.sha256(BLOB).hexdigest()          # content_sha256 = SHA-256 over raw bytes
ADD_PATH = "pilot/noop-2b.txt"


def _manifest(base_commit: str, path: str, content_sha256: str, mode: str = "100644") -> bytes:
    # expected_tree_sha is a format-valid placeholder; compute_tree_unverified
    # does not compare against it.
    return ("\n".join([
        "schema: 2b-patch/1.0",
        f"base_commit: {base_commit}",
        "operations:",
        "  - op: add",
        f"    path: {path}",
        f"    content_sha256: {content_sha256}",
        f'    mode: "{mode}"',
        f"expected_tree_sha: {'0' * 40}",
    ]) + "\n").encode("utf-8")


def _base(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    base_commit = build_base_repo(repo, {"README.md": b"base\n"})
    return repo, base_commit


def _package(tmp_path: Path) -> Path:
    pkg = tmp_path / "package"
    (pkg / "patches" / "blobs").mkdir(parents=True)
    return pkg


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_content_sha256_is_sha256_of_raw_bytes():
    assert CSHA == hashlib.sha256(BLOB).hexdigest()


def test_correct_blob_passes(tmp_path):
    repo, base = _base(tmp_path)
    pkg = _package(tmp_path)
    (pkg / "patches" / "blobs" / CSHA).write_bytes(BLOB)     # name == sha256(bytes)
    tree = patch_2b.compute_tree_unverified(repo, _manifest(base, ADD_PATH, CSHA), pkg)
    assert len(tree) == 40 and all(c in "0123456789abcdef" for c in tree)


def test_missing_blob_fails(tmp_path):
    repo, base = _base(tmp_path)
    pkg = _package(tmp_path)                                  # blobs/ dir exists but is empty
    with pytest.raises(ManifestError) as ei:
        patch_2b.compute_tree_unverified(repo, _manifest(base, ADD_PATH, CSHA), pkg)
    assert ei.value.code == "E_BLOB_MISSING"


def test_mutated_blob_fails(tmp_path):
    repo, base = _base(tmp_path)
    pkg = _package(tmp_path)
    # File is named with CSHA but holds different bytes -> sha256 mismatch.
    (pkg / "patches" / "blobs" / CSHA).write_bytes(b"tampered\n")
    with pytest.raises(ManifestError) as ei:
        patch_2b.compute_tree_unverified(repo, _manifest(base, ADD_PATH, CSHA), pkg)
    assert ei.value.code == "E_BLOB_HASH"


def test_blob_must_live_under_patches_blobs(tmp_path):
    repo, base = _base(tmp_path)
    pkg = _package(tmp_path)
    manifest = _manifest(base, ADD_PATH, CSHA)
    # Wrong location (package root) -> treated as missing.
    (pkg / CSHA).write_bytes(BLOB)
    with pytest.raises(ManifestError) as ei:
        patch_2b.compute_tree_unverified(repo, manifest, pkg)
    assert ei.value.code == "E_BLOB_MISSING"
    # Correct location patches/blobs/<content_sha256> -> succeeds.
    (pkg / "patches" / "blobs" / CSHA).write_bytes(BLOB)
    tree = patch_2b.compute_tree_unverified(repo, manifest, pkg)
    assert len(tree) == 40
