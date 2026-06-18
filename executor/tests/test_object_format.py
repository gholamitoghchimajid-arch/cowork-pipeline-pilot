# executor/tests/test_object_format.py
# PR-A object-format guard tests for executor/patch_2b.py:require_sha1.
#
# require_sha1 must accept a sha1 repository and fail closed (E_OBJFMT) on a
# sha256 repository, because expected_tree_sha is defined as a 40-hex git SHA-1
# tree id in v1.0. The sha1-positive assertion always runs; the sha256-negative
# assertion skips gracefully if the local git lacks sha256 object-format support.
# Local temp repos only.
#
# pytest + stdlib only. No network, no fetch, no push, no PR, no GitHub API,
# no token. (`import patch_2b` / `from util...` resolve via conftest.py.)
import pytest

import patch_2b
from util.repo_builder import build_base_repo


def test_sha1_repo_passes_require_sha1(tmp_path):
    repo = tmp_path / "repo"
    build_base_repo(repo, {"README.md": b"base\n"})       # default object format: sha1
    assert patch_2b.require_sha1(repo) is None             # must not raise


def test_sha256_repo_fails_with_e_objfmt(tmp_path):
    repo = tmp_path / "repo"
    try:
        build_base_repo(repo, {"README.md": b"base\n"}, object_format="sha256")
    except Exception as exc:                               # noqa: BLE001 - graceful skip
        pytest.skip(f"local git does not support --object-format=sha256: {exc}")
    with pytest.raises(patch_2b.ManifestError) as ei:
        patch_2b.require_sha1(repo)
    assert ei.value.code == "E_OBJFMT"
