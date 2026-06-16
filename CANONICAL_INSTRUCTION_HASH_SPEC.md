# Canonical Instruction Hash Spec — v1.0

**Status:** Locked v1.0 (Majid, 2026-06-16).

## Purpose
Define the exact bytes hashed to produce `instruction_hash`, so Cowork, Majid (in the
`MAJID_RUN_APPROVAL` marker), and the executor all derive the identical value. Any
divergence fails closed (the approval will not validate).

## 1. Source
- Hash is computed over the **raw comment/issue `body`** as returned by the GitHub REST
  API (markdown source) — never rendered HTML or any UI representation.
- Comment metadata (author, timestamps, ids, URLs) is excluded.

## 2. Pre-normalization (in order)
1. Strip a leading UTF-8 byte-order mark (BOM) if present.
2. Normalize line endings: replace every `\r\n` and every lone `\r` with `\n`.

## 3. Block extraction (exactly one block required)
- **Single-line form:** a line, anchored at **column 0**, beginning with
  `FINAL_COWORK_INSTRUCTION:` followed by the instruction content on the same line.
  The canonical block is **that entire line**.
- **Multi-line form:** a line at column 0 that is exactly `FINAL_COWORK_INSTRUCTION`
  (the marker token alone, no colon) opens the block; a later line at column 0 that is
  exactly `END_FINAL_COWORK_INSTRUCTION` closes it. The canonical block is the **opening
  line plus every line up to but excluding** the `END_FINAL_COWORK_INSTRUCTION` line.
- If zero opening markers, more than one opening marker, or a multi-line opener with no
  terminator is found, the instruction is **invalid** — no hash is produced (fail-closed).

## 4. Canonical content assembly
1. Take the extracted block lines **verbatim** — no per-line trimming of leading,
   internal, or trailing whitespace; no field reordering.
2. Join the lines with `\n`.
3. Strip **all** trailing `\n` from the assembled content. Do not add a trailing newline.

## 5. Hash
    instruction_hash = lowercase_hex( SHA-256( UTF-8 bytes of canonical content ) )

## 6. Reference test vector
Canonical content (single-line form, exactly):

    FINAL_COWORK_INSTRUCTION: approval-marker smoke test only; no repo changes; no executor run.

Expected:

    instruction_hash = 73974af005130285fbf6e70f5d8b9fe66be9ff86c27aeb64b1c363c3fc15a7df

Independent check:

    printf '%s' "FINAL_COWORK_INSTRUCTION: approval-marker smoke test only; no repo changes; no executor run." | sha256sum

## 7. Implementers (must match byte-for-byte)
- **Cowork** — computes the hash at draft time; shows the exact canonical block
  (copy-paste blob) + the hex in every `FINAL_COWORK_INSTRUCTION` preview.
- **Executor** (`executor/validate_2a.py` and later stages) — recomputes from the
  GitHub API `body`.
- **Relay** — does not hash; its `verify_approval` string-compares the marker's
  `instruction_hash` to the expected hash supplied by Cowork.
- **Majid** — the `MAJID_RUN_APPROVAL` marker carries the hash from Cowork's preview.

## 8. Non-goals
- Does not cover marker freshness / author / base-commit checks (those belong to the
  approval-marker rules).
- Does not define instruction semantics — only the bytes hashed.
