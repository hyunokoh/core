"""Hook into Python services to report uncaught exceptions to the
error_collector service (port 5690 by default).

Usage
-----
At the top of a service entry-point::

    from tools._error_reporter import install_global_handler

    install_global_handler()

The handler is fire-and-forget over a daemon thread: a failed report is
swallowed so the collector being down can never destabilise a calling
service. Reports are sent to the loopback-only internal endpoint
``/errors/internal/python-error``, so this module is safe to wire into
production code paths without exposing anything externally.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request


def _validated_collector_url(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("ERROR_COLLECTOR_URL must be an absolute http(s) URL")
    return raw_url


COLLECTOR_URL = _validated_collector_url(
    os.environ.get(
        "ERROR_COLLECTOR_URL",
        "http://127.0.0.1:5690/errors/internal/python-error",
    )
)
SERVICE_NAME = os.environ.get(
    "SERVICE_NAME",
    os.path.basename(sys.argv[0]).replace(".py", "") or "python-service",
)
ENVIRONMENT = os.environ.get("ZKCEX_ENV", "dev")
RELEASE_VERSION = os.environ.get("ZKCEX_RELEASE", "dev")
REPORT_TIMEOUT_S = float(os.environ.get("ERROR_REPORT_TIMEOUT_S", "2.0"))


def report(
    exc_type=None,
    exc_value=None,
    exc_tb=None,
    *,
    extra: dict | None = None,
    level: str = "error",
    message: str | None = None,
) -> None:
    """Manually report an exception or arbitrary error.

    When called inside an ``except:`` block with no args, picks up the
    active exception via :func:`sys.exc_info`. Always fire-and-forget.
    """
    if exc_type is None:
        exc_type, exc_value, exc_tb = sys.exc_info()

    if exc_type is not None:
        msg = message or f"{exc_type.__name__}: {exc_value}"
        ex_name = exc_type.__name__
        stack = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    else:
        msg = message or "manual report"
        ex_name = None
        stack = None

    body = {
        "source": f"python-service:{SERVICE_NAME}",
        "level": level,
        "message": msg,
        "exception_type": ex_name,
        "stack_trace": stack,
        "metadata": extra or {},
        "release_version": RELEASE_VERSION,
        "environment": ENVIRONMENT,
    }

    def _send():
        try:
            req = urllib.request.Request(  # noqa: S310 - COLLECTOR_URL is http(s)-validated.
                COLLECTOR_URL,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=REPORT_TIMEOUT_S).read()  # noqa: S310
        except Exception:
            # Never raise from the reporter — the collector is best-effort.
            pass

    threading.Thread(target=_send, daemon=True).start()


def install_global_handler() -> None:
    """Hook ``sys.excepthook`` and ``threading.excepthook`` so any uncaught
    exception (main thread or worker) is reported before the original
    handler runs."""
    prev_sys_hook = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        try:
            report(exc_type, exc_value, exc_tb)
        finally:
            try:
                prev_sys_hook(exc_type, exc_value, exc_tb)
            except Exception:
                sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = hook

    prev_thread_hook = threading.excepthook

    def thread_hook(args):
        try:
            report(
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
                extra={"thread": args.thread.name if args.thread else "unknown"},
            )
        finally:
            try:
                prev_thread_hook(args)
            except Exception:
                # Last resort fall-through
                threading.__excepthook__(args)

    threading.excepthook = thread_hook


__all__ = [
    "report",
    "install_global_handler",
    "COLLECTOR_URL",
    "SERVICE_NAME",
    "ENVIRONMENT",
    "RELEASE_VERSION",
]
