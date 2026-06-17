# executor/tests/test_path_rules.py
# PR-A path-validation tests for executor/patch_2b.py.
#
# Path rules are enforced across two stages:
#   * the strict scalar grammar in parse_manifest rejects illegal characters
#     (space, non-ASCII, backslash -> E_SCALAR; NUL/control -> E_CONTROL) before
#     a path ever reaches the schema, and
#   * validate_schema._validate_path rejects structurally-bad but
#     lexically-valid paths (dot segments, leading/trailing/double slash,
#     forbidden prefixes) and, directly, illegal segment chars (E_PATH_CHAR).
# Both stages are exercised here.
#
# pytest + stdlib only. No network, no fetch, no push, no PR, no GitHub API,
# no token. (`import patch_2b` resolves via the sys.path bootstrap in conftest.py.)
import pytest

import patch_2b
from patch_2b import ManifestError

BASE = "d3af6894c87481af55d8b554437636cb4460d3e0"
CSHA = "e22807ae090559c5bcce03c9af5162ced450edbeb0b01c550715cff319b7f432"
TREE = "937f6e7dc362120860efa63cfa9043becf4f4dda"


def _b(text: str) -> bytes:
    return text.encode("utf-8")


def _manifest_with_path(path: str) -> str:
    return "\n".join([
        "schema: 2b-patch/1.0",
        f"base_commit: {BASE}",
        "operations:",
        "  - op: add",
        f"    path: {path}",
        f"    content_sha256: {CSHA}",
        '    mode: "100644"',
        f"expected_tree_sha: {TREE}",
    ]) + "\n"


def _parse_and_validate(path: str) -> dict:
    """Full pipeline: parse then validate. Raises ManifestError from whichever
    stage rejects the path."""
    m = patch_2b.parse_manifest(_b(_manifest_with_path(path)))
    patch_2b.validate_schema(m)
    return m


# --------------------------------------------------------------------------- #
# Accepted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["pilot/noop-2b.txt", "a/b_c.d-e"])
def test_accepted_paths(path):
    m = _parse_and_validate(path)            # must not raise
    assert m["operations"][0]["path"] == path


# --------------------------------------------------------------------------- #
# Rejected (whichever stage catches it)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,code", [
    ("a b", "E_SCALAR"),                     # space -> rejected by scalar grammar
    ("caf\u00e9/x", "E_SCALAR"),             # non-ASCII segment
    ("a\\b", "E_SCALAR"),                    # backslash
    ("a\x00b", "E_CONTROL"),                 # NUL / control character
    ("../x", "E_PATH"),                      # parent (".." segment)
    ("./x", "E_PATH"),                       # "." segment
    ("/x", "E_PATH"),                        # leading slash
    ("x/", "E_PATH"),                        # trailing slash
    ("a//b", "E_PATH"),                      # empty segment
    ("patches/x", "E_PATH_FORBIDDEN"),       # reserved patch store
    (".git/x", "E_PATH_FORBIDDEN"),          # git internals
    (".github/workflows/ci.yml", "E_PATH_FORBIDDEN"),  # CI self-modification
])
def test_rejected_paths(path, code):
    with pytest.raises(ManifestError) as ei:
        _parse_and_validate(path)
    assert ei.value.code == code, f"path {path!r}: expected {code}, got {ei.value.code}"


# --------------------------------------------------------------------------- #
# Schema-level segment-char check (E_PATH_CHAR), exercised directly because the
# scalar grammar would otherwise mask it (it never lets such a char reach here).
# --------------------------------------------------------------------------- #
def test_validate_schema_rejects_illegal_segment_char_directly():
    m = {
        "schema": "2b-patch/1.0",
        "base_commit": BASE,
        "operations": [{"op": "add", "path": "a b", "content_sha256": CSHA, "mode": "100644"}],
        "expected_tree_sha": TREE,
    }
    with pytest.raises(ManifestError) as ei:
        patch_2b.validate_schema(m)
    assert ei.value.code == "E_PATH_CHAR"


# --------------------------------------------------------------------------- #
# Duplicate and nested-path conflicts
# --------------------------------------------------------------------------- #
def test_duplicate_path_rejected():
    raw = "\n".join([
        "schema: 2b-patch/1.0",
        f"base_commit: {BASE}",
        "operations:",
        "  - op: add",
        "    path: a.txt",
        f"    content_sha256: {CSHA}",
        '    mode: "100644"',
        "  - op: delete",
        "    path: a.txt",
        f"expected_tree_sha: {TREE}",
    ]) + "\n"
    m = patch_2b.parse_manifest(_b(raw))
    with pytest.raises(ManifestError) as ei:
        patch_2b.validate_schema(m)
    assert ei.value.code == "E_DUP_PATH"


def test_nested_path_conflict_rejected():
    raw = "\n".join([
        "schema: 2b-patch/1.0",
        f"base_commit: {BASE}",
        "operations:",
        "  - op: add",
        "    path: a",
        f"    content_sha256: {CSHA}",
        '    mode: "100644"',
        "  - op: add",
        "    path: a/b.txt",
        f"    content_sha256: {CSHA}",
        '    mode: "100644"',
        f"expected_tree_sha: {TREE}",
    ]) + "\n"
    m = patch_2b.parse_manifest(_b(raw))
    with pytest.raises(ManifestError) as ei:
        patch_2b.validate_schema(m)
    assert ei.value.code == "E_PATH_NESTED"
