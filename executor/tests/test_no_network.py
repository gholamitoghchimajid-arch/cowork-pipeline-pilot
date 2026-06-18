# executor/tests/test_no_network.py
# PR-A static self-audit: prove the 2b core (executor/patch_2b.py) has no
# network / remote-write / GitHub-API / token surface.
#
# Strategy: parse patch_2b.py with `ast` rather than scanning raw text. The AST
# contains NO comments, and we additionally exclude docstrings, so the module's
# own "NO push / no fetch / no token" prose cannot cause false positives. We then
# enforce, on the actual code:
#   * imports are stdlib-only (allowlist) and exclude PyYAML / network modules
#   * no string constant carries a remote/network/GitHub/token token
#   * no string constant is a network-capable git subcommand (fetch/push/...)
#   * no identifier references a forbidden network module
#
# pytest + stdlib only. No network, no fetch, no push, no PR, no GitHub API,
# no token.
import ast
from pathlib import Path

CORE_PATH = Path(__file__).resolve().parent.parent / "patch_2b.py"   # executor/patch_2b.py
SOURCE = CORE_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

# Exactly the imports patch_2b.py is permitted to use.
ALLOWED_IMPORTS = {"__future__", "hashlib", "re", "subprocess", "pathlib"}

# Modules that must never be imported or referenced by name.
FORBIDDEN_MODULE_NAMES = {
    "socket", "requests", "urllib", "http", "httpx", "ssl", "ftplib",
    "smtplib", "telnetlib", "asyncio", "pycurl", "yaml",
}

# Substrings that must not appear in any (non-docstring) code string constant.
FORBIDDEN_SUBSTRINGS = (
    "git fetch", "git push", "gh ", "github_token", "api.github.com",
    "github.com", "/pulls", "/rulesets", "/protection",
    "https://", "http://", "ssh://", "curl ", "wget ",
    "requests", "urllib", "httpx", "socket",
)

# Network-capable git subcommands that must not appear as a string argument.
FORBIDDEN_GIT_SUBCOMMANDS = {"fetch", "push", "pull", "clone", "remote", "ls-remote"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _docstring_constant_ids(tree):
    """Return id()s of Constant nodes that are module/class/function docstrings."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def _code_string_constants(tree):
    """All str Constant values that are NOT docstrings (includes f-string parts,
    call arguments, error messages, etc.)."""
    skip = _docstring_constant_ids(tree)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            out.append(node.value)
    return out


def _imported_modules(tree):
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods.add(node.module.split(".")[0])
    return mods


def _referenced_names(tree):
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_core_file_present():
    assert CORE_PATH.is_file(), f"core file missing: {CORE_PATH}"
    assert SOURCE.strip(), "core file is empty"


def test_imports_are_stdlib_allowlist_only():
    mods = _imported_modules(TREE)
    extra = mods - ALLOWED_IMPORTS
    assert not extra, f"patch_2b.py imports outside the stdlib allowlist: {sorted(extra)}"


def test_no_pyyaml_import():
    assert "yaml" not in _imported_modules(TREE), "patch_2b.py must not import PyYAML"


def test_no_forbidden_module_imports():
    mods = _imported_modules(TREE)
    bad = mods & FORBIDDEN_MODULE_NAMES
    assert not bad, f"patch_2b.py imports forbidden network modules: {sorted(bad)}"


def test_no_forbidden_module_identifiers():
    names = _referenced_names(TREE)
    bad = names & FORBIDDEN_MODULE_NAMES
    assert not bad, f"patch_2b.py references forbidden network modules: {sorted(bad)}"


def test_no_forbidden_substrings_in_code_strings():
    for s in _code_string_constants(TREE):
        low = s.lower()
        for tok in FORBIDDEN_SUBSTRINGS:
            assert tok not in low, f"forbidden token {tok!r} in code string: {s!r}"


def test_no_network_capable_git_subcommands():
    for s in _code_string_constants(TREE):
        assert s not in FORBIDDEN_GIT_SUBCOMMANDS, \
            f"patch_2b.py uses a network-capable git subcommand: {s!r}"
