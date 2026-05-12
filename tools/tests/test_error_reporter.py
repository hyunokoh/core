from __future__ import annotations

import importlib
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.abspath(os.path.join(HERE, ".."))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)


def _reload_error_reporter(monkeypatch, url: str):
    monkeypatch.setenv("ERROR_COLLECTOR_URL", url)
    sys.modules.pop("_error_reporter", None)
    return importlib.import_module("_error_reporter")


def test_error_reporter_accepts_http_collector_url(monkeypatch):
    reporter = _reload_error_reporter(
        monkeypatch,
        "http://127.0.0.1:5690/errors/internal/python-error",
    )

    assert reporter.COLLECTOR_URL.startswith("http://")


def test_error_reporter_rejects_non_http_collector_url(monkeypatch):
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        _reload_error_reporter(monkeypatch, "file:///tmp/not-a-collector")
