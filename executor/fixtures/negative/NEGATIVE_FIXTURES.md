# 2b negative-case coverage (PR-A)

Negative behavior is enforced two ways:

1. Static conformance fixtures in this directory (parse/validate stage, no git
   repository required). `test_fixtures_conformance.py` runs `parse_manifest` +
   `validate_schema` on each `manifest.yaml` and asserts the `reason_code` in the
   sibling `expect.json`.
2. Programmatic unit tests for git-stage cases that need a built repository
   (existence, blob hash, tree id, object format, tree/parent classification).
   These are intentionally NOT static fixtures.

| Case                           | Reason code       | Covered by                                                          |
| ------------------------------ | ----------------- | ------------------------------------------------------------------- |
| N01 add onto existing file     | E_ADD_EXISTS      | test_preconditions.py                                               |
| N02 replace missing            | E_REPLACE_MISSING | test_preconditions.py                                               |
| N03 delete missing             | E_DELETE_MISSING  | test_preconditions.py                                               |
| N04 blob hash mismatch         | E_BLOB_HASH       | test_blob_verify.py                                                 |
| N05 path traversal             | E_PATH            | fixture N05_path_traversal                                          |
| N06 .github/workflows target   | E_PATH_FORBIDDEN  | fixture N06_workflows_target                                        |
| N07 duplicate path             | E_DUP_PATH        | fixture N07_duplicate_path                                          |
| N08 bad mode (120000)          | E_MODE            | fixture N08_bad_mode                                                |
| N09 expected_tree_sha mismatch | E_TREE_MISMATCH   | test_tree_verify.py                                                 |
| N10 content_sha256 on delete   | E_FORBIDDEN_FIELD | fixture N10_content_on_delete                                       |
| N11 object format != sha1      | E_OBJFMT          | test_object_format.py                                               |
| N12 patch_ref moving/ambiguous | deferred          | PR-B; no patch_ref resolution in PR-A                               |
| N13 duplicate YAML key         | E_DUPKEY          | fixture N13_duplicate_yaml_key                                      |
| N14 disallowed YAML feature    | E_SCALAR          | fixture N14_yaml_feature                                            |
| N15 illegal path char          | E_SCALAR          | fixture N15_path_bad_char; E_PATH_CHAR direct in test_path_rules.py |
| N16 unquoted mode              | E_MODE_UNQUOTED   | fixture N16_mode_unquoted                                           |
| N17 multi-document stream      | E_SYNTAX          | fixture N17_multi_document                                          |
| N18 replace target is tree     | E_TARGET_IS_TREE  | test_preconditions.py                                               |
| N19 delete target is tree      | E_TARGET_IS_TREE  | test_preconditions.py                                               |
| N20 add onto tree              | E_TARGET_IS_TREE  | test_preconditions.py                                               |
| N21 add parent not tree        | E_PARENT_NOT_TREE | test_preconditions.py                                               |
| N22 nested path conflict       | E_PATH_NESTED     | fixture N22_path_nested                                             |

N12 is deferred to PR-B, where the orchestrator resolves `patch_ref` to an
immutable `patch_package_commit`; PR-A's filesystem-package core performs no
`patch_ref` resolution.
