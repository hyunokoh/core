"""Smoke tests that verify every tools/*.py is at least importable.

The CI pipeline gates merges on this — if a module has a syntax error or its
top-level statements raise, the test fails. Module bodies must keep side
effects under ``if __name__ == "__main__":`` for this to succeed.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys

import pytest

TOOLS = pathlib.Path(__file__).parent.parent
# Make sure ``tools/`` is on sys.path so cross-module imports (e.g. one server
# importing ``auth_db``) resolve when the smoke test exec()s a module.
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

# A handful of modules need an env hint that points at an ephemeral sqlite DB
# rather than the developer's local one — done unconditionally because it's
# always safe in a CI runner.
os.environ.setdefault("AUTH_DB_BACKEND", "sqlite")
os.environ.setdefault("AUTH_DB_PATH", ":memory:")

SERVICE_FILES = sorted(p for p in TOOLS.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("path", SERVICE_FILES, ids=lambda p: p.name)
def test_module_imports(path: pathlib.Path) -> None:
    """Every tools/*.py loads cleanly (no syntax errors, no import-time crash)."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register the module so dataclass / typing introspection that looks up
    # ``sys.modules[cls.__module__]`` succeeds during exec_module.
    sys.modules[path.stem] = module
    try:
        spec.loader.exec_module(module)
    finally:
        # Don't leak modules across parametrized invocations.
        sys.modules.pop(path.stem, None)
    assert module is not None
