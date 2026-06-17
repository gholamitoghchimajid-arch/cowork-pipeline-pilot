#!/usr/bin/env python3
"""
validate_2a.py — zero-write instruction + approval-marker validator (2a).

Implements CANONICAL_INSTRUCTION_HASH_SPEC.md v1.0 and INDEPENDENTLY re-verifies a
MAJID_RUN_APPROVAL marker (does NOT call the relay). READ-ONLY: GitHub GET only;
never writes. Output -> stdout (Actions log) + $GITHUB_STEP_SUMMARY.
Exit 0 if VALID, 1 if INVALID, 2 on config/IO error.

Marker field values MUST be raw, unbackticked tokens (no surrounding `backticks`),
matching the relay's strict verify_approval parser. (Strict by design: the executor
must be at least as strict as the relay.)
"""

import hashlib, json, os, re, sys, urllib.request, urllib.error
from datetime import datetime, timezone

API = "https://api.github.com"
APPROVER_LOGIN = "gholamitoghchimajid-arch"
APPROVAL_MARKER = "MAJID_RUN_APPROVAL"
SELFTEST_INPUT = "FINAL_COWORK_INSTRUCTION: approval-marker smoke test only; no repo changes; no executor run."
SELFTEST_EXPECTED = "73974af005130285fbf6e70f5d8b9fe66be9ff86c27aeb64b1c363c3fc15a7df"

def gh_get(path, token):
    req = urllib.request.Request(API + path, method="GET", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "validate-2a/2.0",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def gh_get_all(path, token):
    out, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        batch = gh_get(f"{path}{sep}per_page=100&page={page}", token)
        if not isinstance(batch, list) or not batch:
            break
        out += batch
        if len(batch) < 100:
            break
        page += 1
    return out

def prenormalize(body):
    if body.startswith("\ufeff"):
        body = body[1:]
    return body.replace("\r\n", "\n").replace("\r", "\n")

def extract_blocks(body):
    lines = prenormalize(body).split("\n")
    blocks, unterminated, i = [], False, 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("FINAL_COWORK_INSTRUCTION:"):
            blocks.append(line); i += 1; continue
        if line == "FINAL_COWORK_INSTRUCTION":
            content, j, term = [line], i + 1, False
            while j < len(lines):
                if lines[j] == "END_FINAL_COWORK_INSTRUCTION":
                    term = True; break
                content.append(lines[j]); j += 1
            if not term:
                unterminated = True; i = j; continue
            blocks.append("\n".join(content)); i = j + 1; continue
        i += 1
    return blocks, unterminated

def canonical_hash(block):
    return hashlib.sha256(block.rstrip("\n").encode("utf-8")).hexdigest()

def parse_field(body, names):
    for name in names:
        rx = re.sub(r"[_-]", "[_-]", name) + r"\s*[:=]\s*([^\s`]+)"
        m = re.search(rx, body, re.IGNORECASE)
        if m:
            return m.group(1)
    return None

def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def main():
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("GITHUB_REPOSITORY", "")
    num   = (os.environ.get("ISSUE_NUMBER", "") or "").strip()
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not (token and repo and num.isdigit()):
        print("ERROR: missing/invalid GITHUB_TOKEN / GITHUB_REPOSITORY / ISSUE_NUMBER"); sys.exit(2)

    checks = []
    add = lambda n, r, p, d: checks.append((n, r, p, d))

    st_blocks, _ = extract_blocks(SELFTEST_INPUT)
    st_hash = canonical_hash(st_blocks[0]) if len(st_blocks) == 1 else None
    add("hash_selftest", True, st_hash == SELFTEST_EXPECTED,
        f"reference vector -> {st_hash}" + ("" if st_hash == SELFTEST_EXPECTED else f" (expected {SELFTEST_EXPECTED})"))

    try:
        issue = gh_get(f"/repos/{repo}/issues/{num}", token)
        comments = gh_get_all(f"/repos/{repo}/issues/{num}/comments", token)
    except urllib.error.HTTPError as e:
        print(f"ERROR: GitHub read failed: {e.code} {e.reason}"); sys.exit(2)

    sources = [("issue-body", issue.get("body") or "")] + \
              [(f"comment-{c.get('id')}", c.get("body") or "") for c in comments]
    found, unterminated = [], False
    for src, text in sources:
        b, ut = extract_blocks(text)
        unterminated = unterminated or ut
        found += [(src, blk) for blk in b]
    computed = None
    if unterminated:
        add("instruction_block", True, False, "Opener without END_FINAL_COWORK_INSTRUCTION terminator.")
    elif len(found) != 1:
        add("instruction_block", True, False, f"Expected exactly one block; found {len(found)}.")
    else:
        computed = canonical_hash(found[0][1]); add("instruction_block", True, True, f"One block in {found[0][0]}.")

    add("approver_configured", True, bool(APPROVER_LOGIN), f"Configured approver: {APPROVER_LOGIN}")

    marker_cs = sorted(
        [c for c in comments
         if (c.get("user") or {}).get("login") == APPROVER_LOGIN and APPROVAL_MARKER in (c.get("body") or "")],
        key=lambda c: str(c.get("created_at")))
    ev = marker_cs[-1] if marker_cs else None
    add("approval_evidence_present", True, ev is not None,
        f"Marker in comment {ev['id']} by {APPROVER_LOGIN}." if ev else "No approver-authored marker.")

    ebody = (ev.get("body") if ev else "") or ""
    m_hash   = parse_field(ebody, ["instruction_hash", "instruction-hash"])
    m_base   = parse_field(ebody, ["base_commit", "base-commit"])
    m_issued = parse_field(ebody, ["issued_at", "issued-at"])
    m_ttl    = parse_field(ebody, ["ttl", "ttl_seconds"])
    m_repo   = parse_field(ebody, ["repo"])
    m_issue  = parse_field(ebody, ["issue"])

    add("instruction_hash_match", True,
        (computed is not None and m_hash is not None and m_hash == computed),
        ("matches" if (computed and m_hash == computed) else
         ("no instruction_hash in marker" if not m_hash else
          ("no instruction block to hash" if not computed else "does NOT match"))))

    reasons, ok = [], True
    if ev is None:
        ok = False
        reasons.append("no evidence")
    try:
        issued_ts = parse_iso(m_issued) if m_issued else None
    except Exception:
        issued_ts = None
    if not m_issued or issued_ts is None:
        ok = False
        reasons.append("missing/invalid issued_at")
    try:
        ttl = float(m_ttl) if m_ttl is not None else None
    except Exception:
        ttl = None
    if ttl is None or ttl <= 0:
        ok = False
        reasons.append("missing/invalid ttl (>0)")
    if issued_ts is not None and ttl is not None and ttl > 0:
        exp = issued_ts + ttl
        if datetime.now(timezone.utc).timestamp() > exp:
            ok = False
            reasons.append("stale/expired")
        else:
            reasons.append("fresh")
    add("not_stale", True, ok, "; ".join(reasons))

    if m_base is not None:
        try:
            db = gh_get(f"/repos/{repo}", token).get("default_branch", "main")
            base_sha = (gh_get(f"/repos/{repo}/git/ref/heads/{db}", token).get("object") or {}).get("sha")
        except Exception:
            base_sha = None
        add("base_commit_match", True, (base_sha is not None and m_base == base_sha),
            "matches default-branch HEAD" if m_base == base_sha else "does NOT match")
    if m_repo is not None:
        add("repo_binding", True, m_repo == repo, f"{m_repo} {'==' if m_repo==repo else '!='} {repo}")
    if m_issue is not None:
        add("issue_binding", True, str(m_issue) == num, f"{m_issue} {'==' if str(m_issue)==num else '!='} {num}")

    approved = all(p for (_, req, p, _) in checks if req)

    out = [f"# Executor 2a validation — {'VALID ✅' if approved else 'INVALID ❌'}", "",
           f"- repo: `{repo}`  issue: `#{num}`",
           f"- hash self-test: `{st_hash}` ({'ok' if st_hash==SELFTEST_EXPECTED else 'FAIL'})",
           f"- computed instruction_hash: `{computed or '(none)'}`",
           (f"- marker: {ev.get('html_url')}" if ev else "- marker: (none)"), "",
           "| check | required | passed | detail |", "|---|---|---|---|"]
    out += [f"| {n} | {r} | {'✅' if p else '❌'} | {d} |" for (n, r, p, d) in checks]
    report = "\n".join(out); print(report)
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as f: f.write(report + "\n")
        except Exception as e:
            print(f"(step summary write skipped: {e})")
    sys.exit(0 if approved else 1)

if __name__ == "__main__":
    main()
