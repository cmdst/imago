"""Process-wide runtime hooks shared between ``main.py`` and the adapters.

``NativeAdapter`` needs three things that only the plugin class can provide: the
AstrBot ``Context`` (to call a native provider), the plugin DEBUG logger
(redaction + bot-id prefix live in ``main.py``), and the shared provider-error
line. They are injected once during ``initialize()`` instead of being threaded
through the scheduler, because the scheduler constructs adapters with a bare
``ADAPTERS[api_type](config)`` call — a contract third parties and tests rely on
(``test/test_scheduler.py`` swaps in stubs with exactly that signature).

The container is deliberately mutable-by-function-only: tests and reloads call
:func:`configure`, production code calls :func:`reset` on unload.
"""

from __future__ import annotations

from typing import Any

_CONTEXT: Any | None = None
_DEBUG: Any | None = None
_ERROR_REPORTER: Any | None = None


def configure(
    *,
    context: Any = None,
    debug: Any = None,
    error_reporter: Any = None,
) -> None:
    """Install the runtime hooks.

    Args:
        context: AstrBot plugin context; ``None`` keeps the previous value.
        debug: ``callable(message, *args)`` writing to the plugin DEBUG log.
        error_reporter: ``callable(provider_config, exc)`` writing one provider
            failure line (the scheduler's ``logger`` callback).
    """
    global _CONTEXT, _DEBUG, _ERROR_REPORTER
    if context is not None:
        _CONTEXT = context
    if debug is not None:
        _DEBUG = debug
    if error_reporter is not None:
        _ERROR_REPORTER = error_reporter


def reset() -> None:
    """Drop every hook (plugin unload / test isolation)."""
    global _CONTEXT, _DEBUG, _ERROR_REPORTER
    _CONTEXT = None
    _DEBUG = None
    _ERROR_REPORTER = None


def context() -> Any:
    """Return the installed AstrBot context, or ``None``."""
    return _CONTEXT


def debug_log(message: str, *args: Any) -> None:
    """Write a DEBUG line through the installed plugin logger, if any.

    Args:
        message: Log message; when ``args`` are given it is a ``%``-style template.
        args: Template arguments.
    """
    if _DEBUG is not None:
        _DEBUG(message, *args)


def report_provider_error(provider: Any, exc: BaseException) -> None:
    """Report one provider failure through the installed reporter, if any.

    Args:
        provider: Node configuration the failure belongs to.
        exc: Raised exception.
    """
    if _ERROR_REPORTER is not None:
        _ERROR_REPORTER(provider, exc)
