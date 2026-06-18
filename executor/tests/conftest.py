# executor/tests/conftest.py
# PR-A test configuration.
#
# (1) Runtime no-network guard (the locked requirement): an autouse fixture that
#     blocks Python-level network access for every test, so the local-only 2b
#     core can never reach out. Raises a clear error on any attempt.
# (2) Import bootstrap: put the executor package root and the tests dir on
#     sys.path so `import patch_2b` and `from util... import ...` resolve
#     regardless of the directory pytest is invoked from.
#
# Stdlib + pytest only. No GitHub API, no token, no fetch, no push, no PR,
# no workflow behavior.
import socket
import sys
from pathlib import Path

import pytest

# --- (2) import bootstrap ---------------------------------------------------- #
_TESTS_DIR = Path(__file__).resolve().parent          # executor/tests
_EXECUTOR_DIR = _TESTS_DIR.parent                      # executor
for _p in (str(_EXECUTOR_DIR), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# --- (1) runtime no-network guard -------------------------------------------- #
class NetworkBlocked(RuntimeError):
    """Raised if any test attempts Python-level network access."""


def _blocked(*args, **kwargs):
    raise NetworkBlocked(
        "network access is forbidden in PR-A tests: the 2b local core must run "
        "offline (no fetch, no push, no PR, no GitHub API, no token)."
    )


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Block Python-level networking for the duration of every test.

    Note: this guards Python's socket layer. The 2b core performs no Python
    networking at all, and the git plumbing it shells out to is strictly local
    (no fetch/push); the static self-audit in test_no_network.py separately
    asserts the core contains no fetch/push/PR/token surface."""
    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield
