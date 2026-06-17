Status: Canonical v1.0
Scope: defines `patch_sha256` — the single hash that identifies the exact
patch a 2b executor is approved to apply. This is the integrity anchor that
binds operator approval to precise patch bytes.

This spec defines ONLY how to compute `patch_sha256` from a patch-manifest
file. The manifest *schema* (the meaning of its fields) and git-level
verification (`content_sha256`, `expected_tree_sha`) are specified elsewhere
and are out of scope here.

## 1. Purpose

`patch_sha256` is the lowercase-hex SHA-256 of the canonicalized bytes of a
patch-manifest file. The operator's approval marker carries this value. A 2b
executor MUST recompute it over the manifest it loads and refuse to act unless
it matches the approved value, byte for byte.

## 2. Hash input

The input is the **entire raw byte content of the manifest file** — not a
re-serialization, not a parsed/normalized object. What is reviewed and
approved is exactly what is hashed.

- No YAML/JSON re-encoding. The file's own bytes are authoritative.
- No field reordering, no whitespace reflow, no comment stripping.
- The manifest MUST be a single regular file (no includes, no references to
  other files for hashing purposes).

## 3. Canonicalization rules

Apply these steps, in this exact order, to the raw bytes:

1. **Decode** the bytes as UTF-8. Invalid UTF-8 → FAIL (no hash).
2. **Strip a single leading BOM** (U+FEFF) if present.
3. **Normalize newlines:** replace every `CRLF` (`\r\n`) with `LF` (`\n`),
   then replace every remaining lone `CR` (`\r`) with `LF` (`\n`).
4. **Strip all trailing newlines:** remove every trailing `\n` (equivalent to
   `rstrip("\n")` after step 3).
5. **Do not alter anything else:** interior whitespace, indentation, blank
   lines, key order, and quoting are preserved exactly.
6. **Re-encode** the result as UTF-8.

`patch_sha256` = `sha256(canonical_bytes)` rendered as **lowercase hex**.

## 4. Reference implementation (authoritative)

```python
import hashlib, sys

def patch_sha256(path: str) -> str:
    raw = open(path, "rb").read()
    text = raw.decode("utf-8")              # step 1; raises on invalid UTF-8
    if text and text[0] == "\ufeff":        # step 2
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # step 3
    text = text.rstrip("\n")                # step 4
    return hashlib.sha256(text.encode("utf-8")).hexdigest()  # steps 5-6

if __name__ == "__main__":
    print(patch_sha256(sys.argv[1]))
```

Any conforming implementation in any language MUST produce identical output
for identical input. The Python above is the reference oracle.

## 5. Canonical test vector

The following is the canonical manifest test vector. Its inner hex values
(`base_commit`, `content_sha256`, `expected_tree_sha`) are **fixed literals
chosen for this vector** and are NOT derived — this vector tests the
manifest-hashing pipeline only, not git semantics.

Canonical form (UTF-8, LF line endings, NO trailing newline; 290 bytes):

```
schema: 2b-patch/1.0
base_commit: 0000000000000000000000000000000000000000
operations:
  - op: add
    path: pilot/noop-2b.txt
    content_sha256: 1111111111111111111111111111111111111111111111111111111111111111
    mode: "100644"
expected_tree_sha: 2222222222222222222222222222222222222222
```

The last byte of the canonical input is the closing `2` of
`expected_tree_sha` — there is no trailing newline in the canonical form.
(An editor may save the file with a trailing newline; step 4 removes it, so
the digest is unaffected.)

Reproduce with the reference implementation:

```
python3 patch_manifest_hash.py path/to/test-vector.manifest.yaml
```

Expected `patch_sha256`:

```
6777e1844e051604db78c20ef4022f24a3693bf4a140c648401973ace3b0c9fb
```

This digest has been independently reproduced by two separate computations
over the canonical bytes above.

## 6. How a 2b executor uses this spec

1. Validate the instruction + approval marker via the 2a gate first.
2. Load the manifest referenced by the approved instruction (committed at the
   approved `base_commit`).
3. Compute `patch_sha256` over the loaded manifest using §3/§4.
4. Compare to the `patch_sha256` field in the operator approval marker.
   - Not equal → FAIL-CLOSED. No branch, no apply, no push, no PR.
   - Equal → proceed to local apply and tree verification (separate spec).

`patch_sha256` answers "is this the exact patch the operator approved?" It is
distinct from `content_sha256` / `expected_tree_sha`, which answer "did the
patch apply to the expected result?" All are required; this spec governs only
the first.

## 7. Fail-closed cases

A 2b executor MUST treat each of these as FAIL with zero writes:

- Manifest bytes are not valid UTF-8.
- Manifest file is missing, empty, or not a single regular file.
- Computed `patch_sha256` ≠ approved marker value.
- Approval marker has no `patch_sha256` field.
- Any error during canonicalization or hashing.
- Manifest loaded from any commit other than the approved `base_commit`.

In every case: no branch creation, no apply, no push, no PR.

## 8. Relationship to `CANONICAL_INSTRUCTION_HASH_SPEC.md`

Both specs share the **same canonicalization core** (UTF-8, BOM strip,
CRLF→LF then lone-CR→LF, strip all trailing newlines, lowercase-hex SHA-256).
They differ only in **input selection**:

- Instruction spec: extracts a delimited *block* from an issue/comment body
  (single-line `FINAL_COWORK_INSTRUCTION:` or multi-line opener …
  `END_FINAL_COWORK_INSTRUCTION`), then hashes that block.
- This spec: hashes the **whole manifest file**, no block extraction.

`instruction_hash` binds the human-readable intent; `patch_sha256` binds the
exact machine-applied change. The approval marker carries both, so operator
approval covers intent AND patch bytes together.

## 9. Versioning

This is v1.0. Any change to canonicalization, input selection, or the test
vector requires a new version (`v1.1`, `v2.0`) and a new published vector.
Executors pin the spec version they implement.

## 10. PASS / FAIL criteria for the spec PR

PASS (all required):
- Canonicalization rules (§3) are unambiguous and ordered; the reference
  implementation (§4) matches them exactly.
- The pinned digest (§5) reproduces from the published canonical bytes via the
  §4 reference command on at least two independent computations.
- The §5 canonical bytes are stated precisely enough that the
  no-trailing-newline / no-BOM / LF boundary is unmistakable (290 bytes).
- Fail-closed cases (§7) are complete and each implies zero writes.
- The relationship to the instruction spec (§8) is accurate — shared core,
  different input selection.
- Scope stays narrow: this spec governs only `patch_sha256`; it does not
  define the manifest schema or executor apply/merge behavior.

FAIL (any one):
- Pinned digest does not reproduce, or was authored rather than computed.
- Canonicalization is ambiguous or the reference code diverges from the prose.
- The test vector's trailing-newline / BOM behavior is underspecified.
- Any fail-closed case is missing or would permit a write.
- Scope creep into executor behavior, apply logic, or merge/bypass concerns.
