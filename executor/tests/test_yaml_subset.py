# executor/tests/test_yaml_subset.py
# PR-A strict-parser tests for executor/patch_2b.py:parse_manifest.
# Verifies the deliberately narrow YAML-compatible subset accepts a valid
# manifest (including the digit-bearing `content_sha256` key) and fails closed
# on every out-of-subset construct with the correct reason code.
#
# pytest + stdlib only. No network, no fetch, no push, no PR, no GitHub API,
# no token. (`import patch_2b` resolves via the sys.path bootstrap in conftest.py.)
import pytest

import patch_2b
from patch_2b import ManifestError

BASE = "d3af6894c87481af55d8b554437636cb4460d3e0"
CSHA = "e22807ae090559c5bcce03c9af5162ced450edbeb0b01c550715cff319b7f432"
TREE = "937f6e7dc362120860efa63cfa9043becf4f4dda"

VALID = "\n".join([
    "schema: 2b-patch/1.0",
    f"base_commit: {BASE}",
    "operations:",
    "  - op: add",
    "    path: pilot/noop-2b.txt",
    f"    content_sha256: {CSHA}",
    '    mode: "100644"',
    f"expected_tree_sha: {TREE}",
]) + "\n"


def _b(text: str) -> bytes:
    return text.encode("utf-8")


def _expect(raw: bytes, code: str):
    with pytest.raises(ManifestError) as ei:
        patch_2b.parse_manifest(raw)
    assert ei.value.code == code, f"expected {code}, got {ei.value.code}"


# --------------------------------------------------------------------------- #
# Positive
# --------------------------------------------------------------------------- #
def test_valid_manifest_parses():
    m = patch_2b.parse_manifest(_b(VALID))
    assert m["schema"] == "2b-patch/1.0"
    assert m["base_commit"] == BASE
    assert m["expected_tree_sha"] == TREE
    assert len(m["operations"]) == 1
    assert m["operations"][0] == {
        "op": "add",
        "path": "pilot/noop-2b.txt",
        "content_sha256": CSHA,
        "mode": "100644",
    }


def test_content_sha256_field_parses_regression():
    # Regression for the key-regex fix [a-z_]+ -> [a-z0-9_]+ : a key containing
    # digits (content_sha256) must parse. Pre-fix this raised E_SYNTAX.
    m = patch_2b.parse_manifest(_b(VALID))
    assert m["operations"][0]["content_sha256"] == CSHA


def test_trailing_newline_is_allowed():
    # exactly one trailing newline is tolerated (VALID already ends with one)
    assert patch_2b.parse_manifest(_b(VALID))["schema"] == "2b-patch/1.0"


# --------------------------------------------------------------------------- #
# Byte-level rejections
# --------------------------------------------------------------------------- #
def test_bom_rejected():
    _expect(_b("\ufeff" + VALID), "E_BOM")


def test_blank_line_rejected():
    _expect(_b(VALID.replace("operations:\n", "operations:\n\n")), "E_BLANK")


def test_control_character_rejected():
    _expect(_b(VALID.replace("schema:", "sch\x00ema:")), "E_CONTROL")


def test_trailing_space_invalid_scalar_rejected():
    _expect(_b(VALID.replace("schema: 2b-patch/1.0", "schema: 2b-patch/1.0 ")), "E_SCALAR")


# --------------------------------------------------------------------------- #
# YAML-feature rejections (rejection-by-construction)
# --------------------------------------------------------------------------- #
def test_anchor_rejected():
    _expect(_b(VALID.replace(f"base_commit: {BASE}", f"base_commit: &anchor {BASE}")), "E_SCALAR")


def test_alias_rejected():
    _expect(_b(VALID.replace(f"base_commit: {BASE}", "base_commit: *alias")), "E_SCALAR")


def test_custom_tag_rejected():
    _expect(_b(VALID.replace(f"base_commit: {BASE}", f"base_commit: !!str {BASE}")), "E_SCALAR")


def test_merge_key_rejected():
    _expect(_b(VALID.replace("operations:", "<<: *defaults\noperations:")), "E_SYNTAX")


def test_multi_document_rejected():
    _expect(_b(VALID + "---\nschema: 2b-patch/1.0\n"), "E_SYNTAX")


# --------------------------------------------------------------------------- #
# Duplicate keys
# --------------------------------------------------------------------------- #
def test_duplicate_top_level_key_rejected():
    _expect(_b(VALID.replace("schema: 2b-patch/1.0\n",
                             "schema: 2b-patch/1.0\nschema: 2b-patch/1.0\n")), "E_DUPKEY")


def test_duplicate_op_field_rejected():
    _expect(_b(VALID.replace("    path: pilot/noop-2b.txt\n",
                             "    path: pilot/noop-2b.txt\n    path: other.txt\n")), "E_DUPKEY")


# --------------------------------------------------------------------------- #
# Structural rejections
# --------------------------------------------------------------------------- #
def test_top_level_sequence_rejected():
    _expect(_b("- a\n- b\n"), "E_SYNTAX")


def test_top_level_scalar_rejected():
    _expect(_b("2b-patch/1.0\n"), "E_SYNTAX")


def test_wrong_indentation_rejected():
    # op field indented 3 spaces instead of the required 4
    _expect(_b(VALID.replace("    path: pilot/noop-2b.txt", "   path: pilot/noop-2b.txt")), "E_SYNTAX")


def test_unquoted_mode_rejected():
    _expect(_b(VALID.replace('    mode: "100644"', "    mode: 100644")), "E_MODE_UNQUOTED")


def test_missing_required_top_level_key_rejected():
    _expect(_b(VALID.replace(f"\nexpected_tree_sha: {TREE}", "")), "E_MISSING")


def test_empty_operations_rejected():
    raw = "\n".join([
        "schema: 2b-patch/1.0",
        f"base_commit: {BASE}",
        "operations:",
        f"expected_tree_sha: {TREE}",
    ]) + "\n"
    _expect(_b(raw), "E_EMPTY_OPS")


def test_unknown_top_level_key_rejected():
    _expect(_b(VALID.replace("schema: 2b-patch/1.0\n",
                             "schema: 2b-patch/1.0\nfoo: bar\n")), "E_UNKNOWN_KEY")


def test_unknown_op_field_rejected():
    _expect(_b(VALID.replace("    path: pilot/noop-2b.txt\n",
                             "    path: pilot/noop-2b.txt\n    bogus: x\n")), "E_UNKNOWN_FIELD")
