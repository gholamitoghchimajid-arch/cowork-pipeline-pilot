# Patch-Manifest Schema Specification — v1.0

Status: Canonical v1.0
Scope: defines the schema, git-level semantics, and deterministic apply rules
for a 2b patch manifest. This spec governs what a manifest MEANS and how its
referenced blobs deterministically become a git tree applied onto a target
commit.

This spec does NOT define how `patch_sha256` is computed — that is
`PATCH_MANIFEST_HASH_SPEC.md` (Canonical v1.0). This spec also does not define
executor process, workflow, permissions, PR, or merge behavior.

## 1. Purpose and model

A patch manifest describes a deterministic set of file operations and the
exact git tree they must produce. Two independent approval anchors apply:

* The **patch package** (manifest + blobs) is approved by `patch_sha256`
  (manifest bytes) and per-blob `content_sha256` (raw blob bytes).
* The **target** tree is approved by `base_commit` (the `origin/main` HEAD the
  patch applies onto).

The package source and the target base are related but distinct, and are
verified separately.

## 2. Patch package resolution: `patch_ref` and `patch_package_commit`

* `patch_ref` MAY be the input/reference used to locate the patch package
  (e.g. a branch, tag, or ref name supplied with the approved instruction).
* The executor MUST immediately resolve `patch_ref` to exactly one full
  40-hex commit id, called `patch_package_commit`.
* After resolution, the executor MUST load the manifest and all blobs ONLY
  from `patch_package_commit` — never from a moving branch or ref.
* If `patch_ref` cannot be resolved to exactly one commit (unresolvable,
  ambiguous, or a moving target that changes mid-run), the executor MUST
  FAIL-CLOSED before any remote write.
* Evidence/reporting MUST record both `patch_ref` (as supplied) and the
  resolved `patch_package_commit`.

`patch_sha256` still approves the manifest bytes. `content_sha256` still
approves each blob's raw bytes. `base_commit` remains the target `origin/main`
tree to apply onto. `expected_tree_sha` remains the post-apply git SHA-1 tree
id.

## 3. Git object-format guard (v1.0)

Before applying, the executor MUST run `git rev-parse --show-object-format` on
the target repository and FAIL-CLOSED unless it returns `sha1`. Rationale:
`expected_tree_sha` is defined in v1.0 as a 40-hex git SHA-1 tree id. A
`sha256` object-format repository produces 64-hex tree ids and breaks this
contract. Support for `sha256` object format is deferred to a future,
separately-gated schema version.

## 4. Manifest serialization format (strict YAML subset)

The manifest is parsed as YAML, but v1.0 accepts only a deliberately narrow,
safe subset so that parsed meaning is deterministic and free of surprising
YAML behavior. The hash spec hashes manifest bytes; this section governs how
those bytes are interpreted.

The executor MUST enforce ALL of the following, and FAIL-CLOSED before any
remote write on any violation:

* The parser MUST be a safe loader (no arbitrary object construction;
  equivalent to YAML "safe load").
* The stream MUST contain exactly ONE YAML document. Zero documents, multiple
  documents, or a trailing document separator introducing a second document
  are FAIL-CLOSED.
* The top-level node MUST be a mapping. Top-level sequences, scalars, or null
  are FAIL-CLOSED.
* No YAML anchors (`&name`).
* No YAML aliases (`*name`).
* No custom/explicit tags (e.g. `!!python/...`, `!Custom`, any `!`/`!!` tag).
* No YAML merge keys (`<<`).
* Duplicate mapping keys anywhere in the document are FAIL-CLOSED.
* All `schema`, `op`, hash, commit, `mode`, and `path` values MUST be present
  as YAML strings and MUST be validated against their exact required formats
  (§5–§9). A value supplied as a non-string YAML type (number, bool, null,
  mapping, sequence) where a string is required is FAIL-CLOSED. In particular
  `mode` MUST be a quoted string (e.g. `"100644"`), never a YAML integer.
* Any YAML feature not explicitly permitted by this subset is FAIL-CLOSED.

## 5. Top-level manifest fields

A manifest is a single YAML mapping (per §4) with exactly these fields, all
required:

* `schema` — MUST equal `2b-patch/1.0`.
* `base_commit` — 40-char lowercase hex; the target commit to apply onto.
* `operations` — an ordered, non-empty list of operations (§6).
* `expected_tree_sha` — 40-char lowercase hex; the git SHA-1 tree id of the
  resulting tree (§10).

Unknown top-level fields, a missing field, or a wrong `schema` value are
FAIL-CLOSED.

## 6. Operations

Allowed `op` values: `add`, `replace`, `delete`.

| op        | meaning                          | required fields                        | forbidden fields         |
| --------- | -------------------------------- | -------------------------------------- | ------------------------ |
| `add`     | create a file absent at base     | `op`, `path`, `content_sha256`, `mode` | —                        |
| `replace` | overwrite a file present at base | `op`, `path`, `content_sha256`, `mode` | —                        |
| `delete`  | remove a file present at base    | `op`, `path`                           | `content_sha256`, `mode` |

A missing required field, a present forbidden field, or an unknown `op` is
FAIL-CLOSED. Unknown per-operation fields are FAIL-CLOSED.

## 7. Path validation rules

Each `path` is repo-relative and uses POSIX `/` separators. v1.0 restricts
path segment characters to an ASCII-safe set to avoid Unicode normalization
ambiguity, spaces, and shell metacharacters.

Each segment (the text between `/` separators) MUST be non-empty and MUST
consist ONLY of the characters:

```
A-Z  a-z  0-9  .  _  -
```

A path is REJECTED (FAIL-CLOSED) if it:

* is empty, begins with `/`, or ends with `/`;
* has any empty segment (e.g. a `//` sequence);
* has a `.` or `..` segment;
* contains a character outside the allowed segment set above (this excludes
  spaces, backslashes, control characters, NUL, and all non-ASCII / Unicode
  characters);
* is absolute or otherwise escapes the repository root;
* targets the reserved `patches/` prefix (the patch-package blob/instruction
  store);
* targets `.git/`;
* targets `.github/workflows/` (v1.0 forbids 2b from modifying CI; a
  deliberate self-modification guard, relaxable only in a later versioned and
  separately-gated change).

Duplicate target paths anywhere in `operations` are FAIL-CLOSED. Because no two
operations may touch the same path, the result is order-independent. (The
allowed-character set still permits a leading-dot segment such as `.github`,
so the `.git/` and `.github/workflows/` prefix bans above remain necessary.)

This ASCII-only restriction is intentional for the first pilot version and may
be relaxed only in a later, separately-gated schema version.

## 8. File mode rules

`mode` MUST be a quoted string (§4) equal to one of:

* `"100644"` — normal (non-executable) file;
* `"100755"` — executable file.

Symlinks (`120000`), submodules/gitlinks (`160000`), and directory modes are
FAIL-CLOSED. `mode` is forbidden on `delete`.

## 9. `content_sha256` semantics

`content_sha256` is the lowercase-hex SHA-256 over the RAW bytes of the blob
loaded from the patch package — with NO canonicalization (exact bytes,
including end-of-line characters), unlike the manifest text.

* For `add` and `replace`, the blob MUST exist in the patch package at
  `patches/blobs/<content_sha256>`, resolved via `patch_package_commit` (NOT
  from `base_commit`).
* The executor MUST verify `sha256(blob) == content_sha256` before writing.
* For `replace`, this is the NEW content's hash, and it MUST differ from the
  existing target file's content (a no-op replace is FAIL-CLOSED).

`content_sha256` (SHA-256) is distinct from `expected_tree_sha` (git SHA-1).

## 10. `base_commit` binding and `expected_tree_sha`

* `base_commit` MUST equal the freshly-fetched `origin/main` HEAD at apply
  time; drift is FAIL-CLOSED. No blob-existence requirement is placed on
  `base_commit` — blobs come from the patch package.
* Per-operation preconditions against the tree at `base_commit`:

  * `add` → the path MUST NOT exist;
  * `replace` → the path MUST exist;
  * `delete` → the path MUST exist.
* `expected_tree_sha` is the git SHA-1 tree id (40-hex) of the full resulting
  tree after applying all operations — using patch-package blob bytes — to the
  tree of `base_commit`. The executor builds the result locally, computes the
  tree id, and compares; a mismatch is FAIL-CLOSED and the branch is NOT
  pushed.

## 11. Deterministic apply order

The executor MUST follow this order. All steps before the push are local; the
target `main` is never modified.

1. Resolve `patch_ref` → `patch_package_commit` (exactly one commit, §2).
2. Load the manifest and all blobs from `patch_package_commit`.
3. Compute `patch_sha256` over the manifest and match it to the approved
   marker value (per `PATCH_MANIFEST_HASH_SPEC.md`).
4. Object-format guard: require `sha1` (§3).
5. Parse the manifest with a safe loader and enforce the strict YAML subset
   (§4), then validate the schema: fields (§5), operations (§6), paths (§7),
   modes (§8), duplicate-path check (§7).
6. Fetch `origin/main`; assert HEAD == `base_commit`; checkout that base
   detached onto a fresh `exec/2b/*` branch.
7. Apply operations top-to-bottom: verify each precondition (§10); for
   `add`/`replace`, read the package blob, verify `content_sha256` (§9), and
   write the bytes EXACTLY with the given `mode`; for `delete`, remove the
   file; stage each change.
8. Build the tree, compute the git SHA-1 tree id, and compare to
   `expected_tree_sha` (§10).
9. Only if every preceding step passes: push the `exec/2b/*` branch (the first
   remote write), then open the draft PR.

Identical inputs (same `patch_package_commit`, same `base_commit`) always
produce the identical resulting tree id.

## 12. Fail-closed cases

Each of the following MUST result in FAIL with zero remote writes (no branch
push, no PR). A branch may be built locally but is never pushed on failure.

* `patch_ref` unresolvable, ambiguous, or resolving to more than one / a
  moving commit.
* `patch_sha256` mismatch.
* Object format ≠ `sha1`.
* Any strict-YAML-subset violation (§4): unsafe loader feature, not exactly one
  document, non-mapping top level, anchor, alias, custom tag, merge key,
  duplicate key, or a required value of the wrong YAML type.
* Wrong or missing `schema`; unknown top-level field.
* `operations` empty or not a list.
* Unknown `op`; missing required field; present forbidden field; unknown
  per-op field.
* Any path-rule violation (disallowed character, empty/`.`/`..` segment,
  leading/trailing `/`, traversal, reserved `patches/`, `.git/`,
  `.github/workflows/`).
* Duplicate target path.
* Invalid `mode`.
* `base_commit` ≠ `origin/main` HEAD.
* `add` target exists; `replace`/`delete` target missing.
* Blob missing from the patch package or blob-hash mismatch.
* No-op `replace` (new content identical to existing).
* `expected_tree_sha` mismatch after apply.
* Malformed hex (wrong length or non-lowercase) in any hash/commit field.
* Any parse, I/O, or hashing error.

## 13. Future conformance fixture requirements

The following fixture set is required for future executor/schema conformance
tests. Positive fixture manifests and their `expected_tree_sha` values MUST be
computed and independently reproduced before being pinned in a later
fixture/spec PR; they are not authored or pinned in this document.

Positive fixtures:

* F1 — single `add` of `pilot/noop-2b.txt` from one package blob.
* F2 — `replace` an existing file's content.
* F3 — `delete` an existing file.
* F4 — multi-op `add` + `delete` on distinct paths.

Negative fixtures (each MUST FAIL with zero remote writes):

* N1 — `add` where the path already exists.
* N2 — `replace` where the path is missing.
* N3 — `delete` where the path is missing.
* N4 — blob-hash mismatch.
* N5 — `../` path traversal.
* N6 — target under `.github/workflows/`.
* N7 — duplicate operation path.
* N8 — invalid mode (`120000`).
* N9 — `expected_tree_sha` mismatch.
* N10 — `content_sha256` present on a `delete`.
* N11 — repository object format ≠ `sha1`.
* N12 — `patch_ref` resolves to a moving/ambiguous ref (no single
  `patch_package_commit`).
* N13 — duplicate YAML mapping key.
* N14 — disallowed YAML feature (anchor, alias, custom tag, or merge key).
* N15 — path segment with a disallowed character (space, non-ASCII, or shell
  metacharacter).
* N16 — `mode` supplied as a YAML integer instead of a quoted string.
* N17 — manifest stream containing more than one YAML document.

## 14. Relationship to `PATCH_MANIFEST_HASH_SPEC.md`

`PATCH_MANIFEST_HASH_SPEC.md` defines IDENTITY: `patch_sha256` over the
manifest's canonical file bytes (loaded from `patch_package_commit`) — the
value the operator approves. This spec defines MEANING and INTEGRITY: what the
manifest says, and how package blobs deterministically become a git tree on
top of `base_commit`.

Both operate on the same manifest file. The hash spec is intentionally
schema-agnostic — it hashes bytes even if the schema is invalid. The executor
order is therefore: resolve package → match `patch_sha256` → object-format
guard → safe-parse + strict-YAML-subset + schema-validate → fetch and bind
`base_commit` → apply → verify `expected_tree_sha`.

Two distinct hash functions are in play: `patch_sha256` and `content_sha256`
are SHA-256; `expected_tree_sha` is a git SHA-1 tree id.

## 15. Versioning

This is v1.0. Any change to the schema, operation set, YAML subset, path/mode
rules, object-format assumption, or apply semantics requires a new version
(`v1.1`, `v2.0`) with its own fixtures. Executors pin the schema version they
implement and FAIL-CLOSED on a `schema` value they do not implement.
