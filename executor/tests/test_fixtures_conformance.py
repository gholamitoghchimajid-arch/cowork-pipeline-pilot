# executor/tests/test_fixtures_conformance.py
# PR-A conformance driver.
#
# Positives:
# - rebuild base deterministically and verify pinned base_commit
# - verify pinned patch_sha256 over final manifest bytes
# - verify pinned content_sha256 list against package blobs
# - verify core tree and independent oracle both equal pinned expected_tree_sha
#
# Negatives:
# - honor expect.json stage
# - parse-stage cases must fail in parse_manifest
# - validate-stage cases must parse cleanly, then fail in validate_schema
#
# pytest + stdlib only. No network/fetch/push/PR/GitHub-API/token.

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import patch_2b
from patch_2b import ManifestError
from util.independent_tree import independent_tree_id
from util.repo_builder import build_base_repo


FIX = Path(__file__).resolve().parent.parent / "fixtures"
POSITIVE = sorted((FIX / "positive").iterdir()) if (FIX / "positive").exists() else []
NEGATIVE = sorted(p for p in (FIX / "negative").iterdir() if p.is_dir()) if (FIX / "negative").exists() else []


def _read_tree(root: Path) -> dict:
    files = {}
    if root.exists():
        for p in sorted(root.rglob("*")):
            if p.is_file():
                files[p.relative_to(root).as_posix()] = p.read_bytes()
    return files


def _read_blobs(pkg: Path) -> dict:
    blobs = {}
    bd = pkg / "patches" / "blobs"
    if bd.exists():
        for b in sorted(bd.iterdir()):
            if b.is_file():
                blobs[b.name] = b.read_bytes()
    return blobs


def _patch_sha256(raw: bytes) -> str:
    text = raw.decode("utf-8")
    if text[:1] == "\ufeff":
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("fx", POSITIVE, ids=lambda p: p.name)
def test_positive_fixture(fx, tmp_path):
    pinned = json.loads((fx / "pinned.json").read_text(encoding="utf-8"))
    base_files = _read_tree(fx / "base")

    repo = tmp_path / "repo"
    head = build_base_repo(repo, base_files)
    assert head == pinned["base_commit"], f"{fx.name}: base_commit {head} != {pinned['base_commit']}"

    pkg = fx / "package"
    manifest_bytes = (pkg / "manifest.yaml").read_bytes()

    assert _patch_sha256(manifest_bytes) == pinned["patch_sha256"], f"{fx.name}: patch_sha256 mismatch"

    blobs = _read_blobs(pkg)
    assert sorted(blobs.keys()) == sorted(pinned["content_sha256"]), (
        f"{fx.name}: content_sha256 list != package blob filenames"
    )

    for name, data in blobs.items():
        assert hashlib.sha256(data).hexdigest() == name, (
            f"{fx.name}: blob {name} content does not hash to its filename"
        )

    if not pinned["content_sha256"]:
        assert blobs == {}, f"{fx.name}: expected no blobs for empty content_sha256"

    tree = patch_2b.compute_and_verify(repo, manifest_bytes, pkg)
    assert tree == pinned["expected_tree_sha"], f"{fx.name}: core tree mismatch"

    m = patch_2b.parse_manifest(manifest_bytes)
    indep = independent_tree_id(base_files, m["operations"], blobs, tmp_path / "indep")
    assert indep == pinned["expected_tree_sha"], f"{fx.name}: independent oracle mismatch"


@pytest.mark.parametrize("fx", NEGATIVE, ids=lambda p: p.name)
def test_negative_fixture(fx):
    expect = json.loads((fx / "expect.json").read_text(encoding="utf-8"))
    stage = expect["stage"]
    reason = expect["reason_code"]
    manifest_bytes = (fx / "manifest.yaml").read_bytes()

    if stage == "parse":
        with pytest.raises(ManifestError) as ei:
            patch_2b.parse_manifest(manifest_bytes)
        assert ei.value.code == reason, f"{fx.name}: parse got {ei.value.code}, expected {reason}"

    elif stage == "validate":
        m = patch_2b.parse_manifest(manifest_bytes)
        with pytest.raises(ManifestError) as ei:
            patch_2b.validate_schema(m)
        assert ei.value.code == reason, f"{fx.name}: validate got {ei.value.code}, expected {reason}"

    else:
        pytest.fail(f"{fx.name}: unknown expect stage {stage!r}")
