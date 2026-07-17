"""Browser engine: CloakBrowser-based replacement for camofox daemon API.

Provides the same public API as the old camofox.py (is_available, solve_url,
get_cookies, get_html, import_cookies, evaluate_js, create_tab, close_tab,
navigate_tab, get_snapshot, download_pdf_via_browser, fetch_url,
get_captured_responses, close_all_tabs) but uses CloakBrowser's direct
Playwright API instead of HTTP calls to an external daemon.

Uses a single shared browser instance with multiple tabs (pages). Cookies,
login sessions, and Cloudflare bypass state are shared across all tabs.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CloakBrowser availability check
# ---------------------------------------------------------------------------

_HAS_CLOAKBROWSER: bool | None = None


def _prepare_cloakbrowser_runtime() -> bool:
    """Prepare CloakBrowser before importing any of its launch APIs."""
    try:
        from .cloakbrowser_compat import prepare_cloakbrowser_runtime

        prepare_cloakbrowser_runtime()
    except Exception as exc:
        logger.warning("browser_engine: CloakBrowser runtime preparation failed: %s", exc)
        return False
    return True


def _check_cloakbrowser() -> bool:
    global _HAS_CLOAKBROWSER
    if not _prepare_cloakbrowser_runtime():
        return False
    if _HAS_CLOAKBROWSER is None:
        try:
            from cloakbrowser import launch  # noqa: F401
            _HAS_CLOAKBROWSER = True
        except ImportError:
            _HAS_CLOAKBROWSER = False
    return _HAS_CLOAKBROWSER


# ---------------------------------------------------------------------------
# Per-thread browser (one browser per thread, multiple tabs per browser)
# Playwright sync API uses greenlets bound to threads — cross-thread
# usage crashes. Each thread gets its own browser; within a thread,
# tabs share cookies/session/Cloudflare state.
# ---------------------------------------------------------------------------

import threading as _threading

_tls = _threading.local()
_browser_semaphore: _threading.Semaphore | None = None
_browser_semaphore_lock = _threading.Lock()
_browser_semaphore_capacity = 0
_browser_active = 0
_browser_waiters = 0
_browser_shutdown_generation = 0
_browser_reclaimed_generation = 0
_browser_memory_reclaim_lock = _threading.Lock()
_retained_browser_resources: dict[_threading.Thread, list[Any]] = {}
_retained_browser_resources_lock = _threading.RLock()
MAX_BROWSER_WORKERS = 4


class BrowserOperationCancelled(RuntimeError):
    """Raised when queued browser work is cancelled before launch."""


def _register_retained_browser_resource(resource: Any) -> None:
    """Keep an unclosed Playwright handle reachable for its owner-thread retry."""
    owner = _threading.current_thread()
    with _retained_browser_resources_lock:
        resources = _retained_browser_resources.setdefault(owner, [])
        if not any(candidate is resource for candidate in resources):
            resources.append(resource)


def _forget_retained_browser_resource(resource: Any) -> None:
    """Forget a retained handle only after its close has been confirmed."""
    owner = getattr(resource, "_owner_thread", _threading.current_thread())
    with _retained_browser_resources_lock:
        resources = _retained_browser_resources.get(owner)
        if not resources:
            return
        resources[:] = [candidate for candidate in resources if candidate is not resource]
        if not resources:
            _retained_browser_resources.pop(owner, None)


def _owner_retained_browser_resources() -> tuple[Any, ...]:
    # Key by the Thread object, not its reusable numeric ident. If an owner
    # exits while a driver remains live, a later thread must not touch that
    # Playwright handle merely because the OS recycled the same identifier.
    owner = _threading.current_thread()
    with _retained_browser_resources_lock:
        return tuple(_retained_browser_resources.get(owner, ()))


def _retry_retained_browser_resources() -> tuple[bool, list[Exception]]:
    """Retry failed closes without ever crossing Playwright thread ownership."""
    errors: list[Exception] = []
    for resource in _owner_retained_browser_resources():
        try:
            resource.close()
        except Exception as exc:
            # A driver may raise after it has nevertheless confirmed closure.
            # In that case the proxy has already removed itself from the registry.
            if any(
                candidate is resource
                for candidate in _owner_retained_browser_resources()
            ):
                errors.append(exc)
    return not _owner_retained_browser_resources(), errors


class _BrowserSlotToken:
    """One process-wide browser permit shared by nested work on its owner thread."""

    def __init__(self, semaphore: _threading.Semaphore):
        self.semaphore = semaphore
        self.owner_thread = _threading.current_thread()
        self.owner_thread_id = _threading.get_ident()
        self.references = 1
        self.released = False


class BrowserSlotLease:
    """Owner-thread lease for one slot in the process-wide browser limiter.

    Scoped leases are re-entrant on one thread: a source worker may hold the
    outer lease while ``_get_shared_browser()`` or ``get_persistent_context()``
    retain the same permit for the actual Playwright resource.  Resource leases
    are not left on the re-entrancy stack after their creator returns, so a
    leaked/failed-to-close browser cannot let later work bypass the limiter.
    """

    def __init__(
        self,
        token: _BrowserSlotToken,
        *,
        scoped: bool,
    ) -> None:
        self._token = token
        self._owner_thread = token.owner_thread
        self._owner_thread_id = token.owner_thread_id
        self._scoped = scoped
        self._closed = False

    @property
    def semaphore(self) -> _threading.Semaphore:
        return self._token.semaphore

    def _assert_owner(self) -> None:
        current_thread = _threading.current_thread()
        current = _threading.get_ident()
        if current_thread is not self._owner_thread:
            raise RuntimeError(
                "browser slot lease belongs to another thread; "
                f"owner={self._owner_thread_id}, current={current}"
            )

    def close(self) -> None:
        """Release this reference, and the permit only after the last reference."""
        self._assert_owner()
        if self._closed:
            return

        if self._scoped:
            stack = getattr(_tls, "browser_slot_stack", None)
            if not stack or stack[-1] is not self:
                raise RuntimeError("browser slot scopes must close in owner-thread LIFO order")
            stack.pop()

        release_semaphore = None
        with _browser_semaphore_lock:
            if self._token.released or self._token.references <= 0:
                raise RuntimeError("browser slot token was already released")
            self._token.references -= 1
            if self._token.references == 0:
                self._token.released = True
                release_semaphore = self._token.semaphore
        self._closed = True
        if release_semaphore is not None:
            _release_browser_slot(release_semaphore)

    def __enter__(self):
        self._assert_owner()
        if self._closed:
            raise RuntimeError("browser slot lease is already closed")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _new_browser_slot_lease(
    config: dict[str, Any] | None,
    cancel_event: _threading.Event | None,
    *,
    scoped: bool,
) -> BrowserSlotLease:
    """Acquire or re-enter the global limiter on the current owner thread."""
    cancel_event = _effective_cancel_event(cancel_event)
    if _cancelled(cancel_event):
        raise BrowserOperationCancelled("browser operation cancelled before launch")

    # A previous direct/persistent resource can have failed to close after its
    # caller dropped the local variable. Retry that exact owner-thread handle
    # before taking/re-entering a permit; otherwise max=1 would deadlock and a
    # re-entrant outer source lease could launch a replacement beside it.
    if _owner_retained_browser_resources():
        retained_closed, retained_errors = _retry_retained_browser_resources()
        if not retained_closed:
            detail = "; ".join(str(error) for error in retained_errors)
            raise RuntimeError(
                "previous owner-thread browser resource could not be closed; "
                "replacement launch refused"
                + (f": {detail}" if detail else "")
            )

    stack = getattr(_tls, "browser_slot_stack", None)
    if stack is None:
        stack = []
        _tls.browser_slot_stack = stack

    if stack:
        token = stack[-1]._token
        if token.owner_thread is not _threading.current_thread():
            raise RuntimeError("browser slot scope crossed thread ownership")
        with _browser_semaphore_lock:
            if token.released:
                raise RuntimeError("cannot re-enter a released browser slot")
            token.references += 1
    else:
        semaphore = _prepare_browser_slot(config)
        _acquire_browser_slot(
            semaphore,
            waiter_registered=True,
            cancel_event=cancel_event,
        )
        token = _BrowserSlotToken(semaphore)

    lease = BrowserSlotLease(token, scoped=scoped)
    if scoped:
        stack.append(lease)
    return lease


def browser_slot(
    config: dict[str, Any] | None = None,
    cancel_event: _threading.Event | None = None,
) -> BrowserSlotLease:
    """Return a re-entrant scoped lease for a browser-backed operation."""
    return _new_browser_slot_lease(config, cancel_event, scoped=True)


def _retain_browser_slot(
    config: dict[str, Any] | None = None,
    cancel_event: _threading.Event | None = None,
) -> BrowserSlotLease:
    """Retain a permit until an owning browser/context is confirmed closed."""
    return _new_browser_slot_lease(config, cancel_event, scoped=False)


class _LeasedPersistentContext:
    """Proxy that releases a browser slot when its context is closed."""

    def __init__(
        self,
        context: Any,
        lease: BrowserSlotLease | _threading.Semaphore,
    ):
        self._context = context
        # Accept a raw semaphore for compatibility with low-level lifecycle
        # tests; production paths always pass an owner-thread lease.
        self._lease = lease if isinstance(lease, BrowserSlotLease) else None
        self._semaphore = lease.semaphore if self._lease is not None else lease
        self._owner_thread = _threading.current_thread()
        self._owner_thread_id = _threading.get_ident()
        self._closed = False
        self._close_lock = _threading.Lock()

    def __getattr__(self, name: str) -> Any:
        if self._lease is not None:
            self._lease._assert_owner()
        return getattr(self._context, name)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            if self._lease is not None:
                self._lease._assert_owner()
            try:
                self._context.close()
            except Exception:
                if not _context_is_confirmed_closed(self._context):
                    _register_retained_browser_resource(self)
                    raise
                self._closed = True
                self._release_lease()
                _forget_retained_browser_resource(self)
                raise
            self._closed = True
            self._release_lease()
            _forget_retained_browser_resource(self)

    def _release_lease(self) -> None:
        if self._lease is not None:
            self._lease.close()
        else:
            _release_browser_slot(self._semaphore)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class _LeasedBrowser:
    """Browser proxy that owns a slot until the process is confirmed closed."""

    def __init__(self, browser: Any, lease: BrowserSlotLease):
        self._browser = browser
        self._lease = lease
        self._owner_thread = _threading.current_thread()
        self._owner_thread_id = _threading.get_ident()
        self._closed = False
        self._close_lock = _threading.Lock()

    def __getattr__(self, name: str) -> Any:
        self._lease._assert_owner()
        return getattr(self._browser, name)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, _LeasedBrowser):
            other = other._browser
        return self._browser == other

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._lease._assert_owner()
            closed, errors = _close_resource_with_confirmation(
                self._browser,
                is_browser=True,
            )
            if not closed:
                _register_retained_browser_resource(self)
                if errors:
                    raise errors[-1]
                raise RuntimeError("browser close could not be confirmed")
            self._closed = True
            self._lease.close()
            _forget_retained_browser_resource(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _browser_worker_limit(config: dict[str, Any] | None) -> int:
    try:
        configured = int((config or {}).get("max_browser_workers", 1))
    except (TypeError, ValueError):
        return 1
    return min(MAX_BROWSER_WORKERS, max(1, configured))


def _get_browser_semaphore(config: dict[str, Any] | None) -> _threading.Semaphore:
    desired = _browser_worker_limit(config)
    with _browser_semaphore_lock:
        return _select_browser_semaphore_locked(desired)


def _select_browser_semaphore_locked(desired: int) -> _threading.Semaphore:
    """Select the limiter while ``_browser_semaphore_lock`` is held."""
    global _browser_semaphore, _browser_semaphore_capacity
    if _browser_semaphore is None or (
        _browser_active == 0
        and _browser_waiters == 0
        and _browser_semaphore_capacity != desired
    ):
        _browser_semaphore = _threading.Semaphore(desired)
        _browser_semaphore_capacity = desired
    return _browser_semaphore


def _prepare_browser_slot(config: dict[str, Any] | None) -> _threading.Semaphore:
    """Select a limiter and register its waiter as one atomic operation."""
    global _browser_waiters
    desired = _browser_worker_limit(config)
    with _browser_semaphore_lock:
        semaphore = _select_browser_semaphore_locked(desired)
        _browser_waiters += 1
        return semaphore


def _set_thread_cancel_event(cancel_event: _threading.Event | None):
    """Set cooperative cancellation for browser work on the current thread."""
    previous = getattr(_tls, "cancel_event", None)
    _tls.cancel_event = cancel_event
    return previous


def _effective_cancel_event(
    cancel_event: _threading.Event | None = None,
) -> _threading.Event | None:
    return cancel_event if cancel_event is not None else getattr(_tls, "cancel_event", None)


def _cancelled(cancel_event: _threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _wait_or_cancel(
    cancel_event: _threading.Event | None,
    timeout: float,
) -> bool:
    if cancel_event is None:
        time.sleep(max(0.0, timeout))
        return False
    return cancel_event.wait(max(0.0, timeout))


def _write_pdf_bytes_atomic(
    output_path: Path,
    pdf_bytes: bytes,
    cancel_event: _threading.Event | None = None,
) -> bool:
    """Replace a PDF only after a complete, non-cancelled temporary write."""
    from .pdf_utils import write_pdf_bytes_atomic

    return write_pdf_bytes_atomic(output_path, pdf_bytes, cancel_event)


def _acquire_browser_slot(
    semaphore: _threading.Semaphore,
    *,
    waiter_registered: bool = False,
    cancel_event: _threading.Event | None = None,
) -> None:
    global _browser_active, _browser_waiters
    if not waiter_registered:
        with _browser_semaphore_lock:
            _browser_waiters += 1
    try:
        while True:
            effective_cancel = _effective_cancel_event(cancel_event)
            if effective_cancel is not None and effective_cancel.is_set():
                raise BrowserOperationCancelled("browser operation cancelled before launch")
            if not semaphore.acquire(timeout=0.1):
                continue
            effective_cancel = _effective_cancel_event(cancel_event)
            if effective_cancel is not None and effective_cancel.is_set():
                semaphore.release()
                raise BrowserOperationCancelled("browser operation cancelled before launch")
            with _browser_semaphore_lock:
                _browser_active += 1
            return
    finally:
        with _browser_semaphore_lock:
            _browser_waiters = max(0, _browser_waiters - 1)


def _release_browser_slot(semaphore: _threading.Semaphore) -> None:
    global _browser_active, _browser_shutdown_generation
    semaphore.release()
    with _browser_semaphore_lock:
        _browser_active = max(0, _browser_active - 1)
        if _browser_active == 0:
            _browser_shutdown_generation += 1


def _trim_process_heap() -> bool:
    """Return free glibc arena pages to Linux without making it a dependency."""
    import sys

    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        malloc_trim = ctypes.CDLL(None).malloc_trim
        malloc_trim.argtypes = [ctypes.c_size_t]
        malloc_trim.restype = ctypes.c_int
        return bool(malloc_trim(0))
    except (AttributeError, OSError):
        return False


def reclaim_idle_browser_memory() -> bool:
    """Collect closed Playwright cycles and trim idle allocator arenas once."""
    global _browser_reclaimed_generation

    if not _browser_memory_reclaim_lock.acquire(blocking=False):
        return False
    try:
        with _browser_semaphore_lock:
            target_generation = _browser_shutdown_generation
            if (
                _browser_active != 0
                or _browser_waiters != 0
                or target_generation <= _browser_reclaimed_generation
            ):
                return False
        with _retained_browser_resources_lock:
            if _retained_browser_resources:
                return False
        with _tabs_lock:
            if _tabs:
                return False

        # Browser/context/page handles have left their owner-thread stacks by
        # this boundary. CloakBrowser and Playwright create strong reference
        # cycles, so refcounting alone cannot release their response buffers.
        import gc

        collected = gc.collect()
        trimmed = _trim_process_heap()
        with _browser_semaphore_lock:
            _browser_reclaimed_generation = max(
                _browser_reclaimed_generation,
                target_generation,
            )
        logger.debug(
            "browser_engine: reclaimed idle browser memory "
            "(generation=%d, collected=%d, trimmed=%s)",
            target_generation,
            collected,
            trimmed,
        )
        return True
    except Exception as exc:
        logger.debug("browser_engine: idle memory reclamation failed: %s", exc)
        return False
    finally:
        _browser_memory_reclaim_lock.release()


def _build_browser_args(config: dict[str, Any] | None = None) -> list[str]:
    """Build Chromium launch args from config (proxy, flags, etc.)."""
    args = ["--disable-features=CrossOriginOpenerPolicy"]
    if config:
        proxy = config.get("browser_static_proxy", "")
        if proxy:
            args.append(f"--proxy-server={proxy}")
    return args


def _optional_boolean_state(obj: Any, name: str) -> bool | None:
    try:
        value = getattr(obj, name)
    except (AttributeError, TypeError):
        return None
    except Exception:
        return None
    if callable(value):
        try:
            value = value()
        except Exception:
            return None
    return value if isinstance(value, bool) else None


def _context_is_confirmed_closed(context: Any) -> bool:
    """Return True only when the driver exposes an affirmative closed state."""
    for attribute in ("is_closed", "closed", "_closed"):
        if _optional_boolean_state(context, attribute) is True:
            return True
    return False


def _browser_is_confirmed_closed(browser: Any) -> bool:
    """Return True when the browser driver confirms its process is disconnected."""
    if _optional_boolean_state(browser, "is_connected") is False:
        return True
    return _context_is_confirmed_closed(browser)


def _close_resource_with_confirmation(
    resource: Any,
    *,
    is_browser: bool = False,
    attempts: int = 2,
) -> tuple[bool, list[Exception]]:
    """Close a driver resource without treating an exception as successful cleanup."""
    errors: list[Exception] = []
    confirmed_closed = (
        _browser_is_confirmed_closed if is_browser else _context_is_confirmed_closed
    )
    for attempt in range(max(1, attempts)):
        try:
            resource.close()
            return True, errors
        except Exception as exc:
            errors.append(exc)
            if confirmed_closed(resource):
                return True, errors
            if attempt + 1 < attempts:
                time.sleep(0.05)
    return False, errors


def _shared_browser_is_usable(browser: Any, context: Any) -> bool:
    if browser is None or context is None:
        return False
    if _optional_boolean_state(browser, "is_connected") is False:
        return False
    for attribute in ("is_closed", "closed", "_closed"):
        if _optional_boolean_state(context, attribute) is True:
            return False
    return True


def _thread_browser_resources_present() -> bool:
    """Return whether this thread still owns shared-engine state to clean up."""
    return bool(
        getattr(_tls, "browser", None) is not None
        or getattr(_tls, "context", None) is not None
        or getattr(_tls, "browser_lease", None) is not None
        or getattr(_tls, "semaphore", None) is not None
        or getattr(_tls, "tab_ids", None)
        or _owner_retained_browser_resources()
    )

def _get_shared_browser(config: dict[str, Any] | None = None):
    """Get or create a browser for the current thread. Returns (browser, context)."""
    browser = getattr(_tls, "browser", None)
    context = getattr(_tls, "context", None)
    if _shared_browser_is_usable(browser, context):
        return browser, context
    if (
        browser is not None
        or context is not None
        or getattr(_tls, "browser_lease", None) is not None
        or getattr(_tls, "semaphore", None) is not None
        or getattr(_tls, "tab_ids", None)
    ):
        logger.warning("browser_engine: replacing stale thread-local browser")
        if not shutdown_shared_browser():
            raise RuntimeError(
                "browser_engine: stale thread-local browser could not be closed"
            )

    # Playwright Sync API cannot run inside an asyncio event loop
    try:
        import asyncio
        asyncio.get_running_loop()
        raise RuntimeError(
            "CloakBrowser (Playwright Sync API) cannot run inside an asyncio event loop. "
            "Use the HTTP download sources instead, or run outside of async context."
        )
    except RuntimeError as e:
        if "cannot run inside" in str(e):
            raise
        # No running loop — OK to proceed
        pass

    if not _check_cloakbrowser():
        raise RuntimeError("cloakbrowser not installed. Run: pip install cloakbrowser")

    from cloakbrowser import launch
    from .cloakbrowser_compat import launch_with_driver_cleanup

    # Auto-detect headless: Docker/CI environments have no DISPLAY
    import os
    _has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    headless = not _has_display  # default: headless when no display
    humanize = True
    if config:
        if _has_display:
            # Only respect config override when a display is available
            headless = config.get("browser_headless", False)
        humanize = config.get("browser_humanize", True)

    args = _build_browser_args(config)
    lease = _retain_browser_slot(config)
    browser = None
    context = None
    try:
        browser = launch_with_driver_cleanup(
            launch,
            headless=headless,
            humanize=humanize,
            args=args,
        )
        context = browser.new_context()
        _seed_context_cookies(context, config)
    except Exception:
        if browser is None:
            lease.close()
        else:
            _tls.browser = browser
            _tls.context = context
            _tls.browser_lease = lease
            _tls.semaphore = lease.semaphore
            shutdown_shared_browser()
        raise
    _tls.browser = browser
    _tls.context = context
    _tls.browser_lease = lease
    _tls.semaphore = lease.semaphore
    logger.info(f"browser_engine: browser ready for thread {_threading.current_thread().name}")
    return browser, context


def get_persistent_context(
    profile_dir: str | Path,
    config: dict[str, Any] | None = None,
):
    """Get or create a persistent browser context for fingerprint consistency.

    Unlike launch() + cookie restore, persistent context preserves:
    - Browser fingerprint (canvas, WebGL, audio, fonts)
    - Cookies and localStorage across restarts
    - Login sessions without re-authentication

    This is the recommended approach for publisher sessions that need
    stable identity across multiple download runs.
    """
    if not _check_cloakbrowser():
        raise RuntimeError("cloakbrowser not installed. Run: pip install cloakbrowser")

    try:
        from .cloakbrowser_compat import prepare_cloakbrowser_runtime
        prepare_cloakbrowser_runtime()
    except Exception:
        pass

    from cloakbrowser import launch_persistent_context

    headless = False
    humanize = True
    if config:
        headless = config.get("browser_headless", False)
        humanize = config.get("browser_humanize", True)

    args = _build_browser_args(config)
    profile_path = Path(profile_dir)
    profile_path.mkdir(parents=True, exist_ok=True)

    ctx = launch_persistent_context(
        str(profile_path),
        headless=headless,
        humanize=humanize,
        args=args,
    )
    logger.info(f"browser_engine: persistent context ready at {profile_path}")
    return ctx


def get_browser_page(config: dict[str, Any] | None = None):
    """Get a new page from the shared browser (for custom browser interactions).
    
    Returns a Playwright Page object that the caller must close after use.
    Returns None if the browser is not available.
    """
    if not _check_cloakbrowser():
        return None
    try:
        _browser, context = _get_shared_browser(config)
        return context.new_page()
    except Exception:
        return None


def get_persistent_context(
    profile_dir: str | Path,
    config: dict[str, Any] | None = None,
):
    """Get or create a persistent browser context for fingerprint consistency.

    Unlike launch() + cookie restore, persistent context preserves:
    - Browser fingerprint (canvas, WebGL, audio, fonts)
    - Cookies and localStorage across restarts
    - Login sessions without re-authentication

    This is the recommended approach for publisher sessions that need
    stable identity across multiple download runs.
    """
    if not _check_cloakbrowser():
        raise RuntimeError("cloakbrowser not installed. Run: pip install cloakbrowser")

    from cloakbrowser import launch_persistent_context
    from .cloakbrowser_compat import launch_with_driver_cleanup

    import os
    _has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    headless = not _has_display
    humanize = True
    if config:
        if _has_display:
            headless = config.get("browser_headless", False)
        humanize = config.get("browser_humanize", True)

    args = _build_browser_args(config)
    profile_path = Path(profile_dir)
    profile_path.mkdir(parents=True, exist_ok=True)

    lease = _retain_browser_slot(config)
    ctx = None
    try:
        ctx = launch_with_driver_cleanup(
            launch_persistent_context,
            str(profile_path),
            headless=headless,
            humanize=humanize,
            args=args,
        )
        _seed_context_cookies(ctx, config)
    except Exception:
        context_closed = ctx is None
        if ctx is not None:
            context_closed, close_errors = _close_resource_with_confirmation(ctx)
            if not context_closed:
                retained_context = _LeasedPersistentContext(ctx, lease)
                _register_retained_browser_resource(retained_context)
                logger.warning(
                    "browser_engine: failed to close persistent context after "
                    "initialization error; retaining its owner-thread handle "
                    "and browser permit: "
                    + "; ".join(str(error) for error in close_errors)
                )
        if context_closed:
            lease.close()
        raise
    logger.info(f"browser_engine: persistent context ready at {profile_path}")
    return _LeasedPersistentContext(ctx, lease)


def shutdown_shared_browser() -> bool:
    """Shut down the current thread's browser. Call on thread exit or process exit."""
    context = getattr(_tls, "context", None)
    browser = getattr(_tls, "browser", None)
    lease = getattr(_tls, "browser_lease", None)
    semaphore = getattr(_tls, "semaphore", None)
    if (
        context is None
        and browser is None
        and lease is None
        and semaphore is None
        and not getattr(_tls, "tab_ids", None)
        and not _owner_retained_browser_resources()
    ):
        return True

    tab_close_errors = _close_thread_tabs()
    context_closed = context is None
    browser_closed = browser is None
    context_errors: list[Exception] = []
    browser_errors: list[Exception] = []

    if context is not None:
        context_closed, context_errors = _close_resource_with_confirmation(context)
    if browser is not None:
        browser_closed, browser_errors = _close_resource_with_confirmation(
            browser,
            is_browser=True,
        )
        if browser_closed:
            # A normally closed/disconnected browser owns and terminates all of
            # its contexts, even if an earlier context.close() call raised.
            context_closed = True

    _tls.context = None if context_closed else context
    _tls.browser = None if browser_closed else browser
    shared_shutdown_complete = context_closed and browser_closed
    if shared_shutdown_complete:
        _tls.browser_lease = None
        _tls.semaphore = None
        if lease is not None:
            lease.close()
        elif semaphore is not None:
            _release_browser_slot(semaphore)
    else:
        # Keep both the permit and every still-live driver handle. A later
        # shutdown call can retry without allowing a replacement Chromium to
        # exceed the configured browser limit.
        _tls.browser_lease = lease
        _tls.semaphore = semaphore

    retained_closed, retained_errors = _retry_retained_browser_resources()
    shutdown_complete = shared_shutdown_complete and retained_closed

    close_errors = list(tab_close_errors)
    if not context_closed:
        close_errors.append(
            "context: " + "; retry: ".join(str(error) for error in context_errors)
        )
    if not browser_closed:
        close_errors.append(
            "browser: " + "; retry: ".join(str(error) for error in browser_errors)
        )
    if not retained_closed:
        close_errors.append(
            "retained resource: "
            + "; retry: ".join(str(error) for error in retained_errors)
        )
    if close_errors:
        logger.warning(
            "browser_engine: browser shutdown incomplete: " + "; ".join(close_errors)
        )
    else:
        logger.info("browser_engine: browser shut down")
    return shutdown_complete


def _ensure_compat() -> bool:
    """Backward-compatible alias for the real, package-local runtime setup."""
    return _prepare_cloakbrowser_runtime()


# ---------------------------------------------------------------------------
# Public API — drop-in replacements for camofox.py functions
# ---------------------------------------------------------------------------

def is_available(config: dict[str, Any] | None = None) -> bool:
    """Check if CloakBrowser is available (importable)."""
    return _check_cloakbrowser()


# ---------------------------------------------------------------------------
# Tab registry — maps tab_id → (browser, context, page)
# Used by create_tab/evaluate_js/navigate_tab/close_tab for sequential
# tab-based workflows within a single operation (thread-safe: one thread per tab).
# ---------------------------------------------------------------------------

_tabs: dict[str, Any] = {}  # tab_id -> page
_tab_owners: dict[str, int] = {}
_captured: dict[str, list] = {}  # tab_id -> captured PDF responses
_tabs_lock = _threading.Lock()

_CookieKey = tuple[str, str, str]
_imported_cookies: dict[Path, dict[_CookieKey, dict[str, Any]]] = {}
_imported_cookies_lock = _threading.Lock()
_imported_cookie_path_locks: dict[Path, Any] = {}
_imported_cookie_path_locks_lock = _threading.Lock()


def _is_valid_imported_cookie(cookie: dict[str, Any]) -> bool:
    return bool(
        cookie.get("name")
        and (cookie.get("domain") or cookie.get("url"))
        and cookie.get("path", "/").startswith("/")
    )


def _persistent_cookie_path(config: dict[str, Any] | None) -> Path:
    from .config import DATA_DIR

    cache_dir = Path(
        (config or {}).get("cache_dir") or str(DATA_DIR / "cache")
    ).expanduser()
    return (cache_dir / "imported_browser_cookies.json").resolve()


def _imported_cookie_path_lock(path: Path):
    with _imported_cookie_path_locks_lock:
        lock = _imported_cookie_path_locks.get(path)
        if lock is None:
            lock = _threading.RLock()
            _imported_cookie_path_locks[path] = lock
        return lock


def _load_persisted_imported_cookies(
    config: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    path = _persistent_cookie_path(config)
    with _imported_cookie_path_lock(path):
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"browser_engine: failed to read imported cookies: {exc}")
            return []
        if not isinstance(payload, list):
            return []
        return [
            dict(cookie)
            for cookie in payload
            if isinstance(cookie, dict) and _is_valid_imported_cookie(cookie)
        ]


def _cookie_key(cookie: dict[str, Any]) -> _CookieKey:
    return (
        str(cookie.get("domain", "")),
        str(cookie.get("path", "/")),
        str(cookie.get("name", "")),
    )


def _remember_imported_cookies(
    config: dict[str, Any] | None,
    cookies: list[dict[str, Any]],
) -> None:
    path = _persistent_cookie_path(config)
    with _imported_cookie_path_lock(path):
        with _imported_cookies_lock:
            cookie_store = _imported_cookies.setdefault(path, {})
            for cookie in cookies:
                if not _is_valid_imported_cookie(cookie):
                    continue
                cookie_store[_cookie_key(cookie)] = dict(cookie)


def _persist_imported_cookies(config: dict[str, Any] | None) -> bool:
    path = _persistent_cookie_path(config)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with _imported_cookie_path_lock(path):
        with _imported_cookies_lock:
            cookies = [
                dict(cookie)
                for cookie in _imported_cookies.get(path, {}).values()
            ]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(path)
            return True
        except Exception as exc:
            logger.warning(f"browser_engine: failed to persist imported cookies: {exc}")
            return False
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _seed_context_cookies(context: Any, config: dict[str, Any] | None) -> None:
    path = _persistent_cookie_path(config)
    with _imported_cookie_path_lock(path):
        _remember_imported_cookies(
            config,
            _load_persisted_imported_cookies(config),
        )
        with _imported_cookies_lock:
            cookie_items = [
                (key, dict(cookie))
                for key, cookie in _imported_cookies.get(path, {}).items()
            ]
        rejected = []
        for key, cookie in cookie_items:
            try:
                context.add_cookies([cookie])
            except Exception as exc:
                rejected.append((key, cookie))
                logger.warning(
                    "browser_engine: rejected imported cookie "
                    f"{cookie.get('name', '')}: {exc}"
                )
        if rejected:
            with _imported_cookies_lock:
                cookie_store = _imported_cookies.get(path, {})
                for key, rejected_cookie in rejected:
                    if cookie_store.get(key) == rejected_cookie:
                        cookie_store.pop(key, None)
            _persist_imported_cookies(config)


def _register_tab(browser, context, page) -> str:
    tab_id = uuid.uuid4().hex[:12]
    with _tabs_lock:
        _tabs[tab_id] = page
        _tab_owners[tab_id] = _threading.get_ident()
        _captured[tab_id] = []
    tab_ids = getattr(_tls, "tab_ids", None)
    if tab_ids is None:
        tab_ids = set()
        _tls.tab_ids = tab_ids
    tab_ids.add(tab_id)

    # Record PDF response metadata without retaining response bodies in memory.
    def _on_response(response):
        try:
            ct = response.headers.get("content-type", "")
            if "pdf" in ct or "octet-stream" in ct:
                with _tabs_lock:
                    captured = _captured.get(tab_id)
                    if captured is not None:
                        captured.append({
                            "url": response.url,
                            "status": response.status,
                            "contentType": ct,
                        })
        except Exception:
            pass

    try:
        page.on("response", _on_response)
    except Exception:
        pass

    return tab_id


def _resolve_tab(tab_id: str):
    """Look up page for a tab_id. Returns page or None."""
    with _tabs_lock:
        owner = _tab_owners.get(tab_id)
        page = _tabs.get(tab_id)
    if owner is not None and owner != _threading.get_ident():
        logger.warning(f"browser_engine: tab {tab_id} belongs to another thread")
        return None
    return page


def _close_thread_tabs() -> list[str]:
    """Close and forget pages owned by the current Playwright thread."""
    tab_ids = tuple(getattr(_tls, "tab_ids", ()))
    _tls.tab_ids = set()
    errors: list[str] = []
    for tab_id in tab_ids:
        with _tabs_lock:
            page = _tabs.pop(tab_id, None)
            _tab_owners.pop(tab_id, None)
            _captured.pop(tab_id, None)
        if page is not None:
            try:
                page.close()
            except Exception as exc:
                errors.append(f"tab {tab_id}: {exc}")
    return errors


def solve_url(
    url: str,
    config: dict[str, Any],
    *,
    max_timeout: int = 60000,
) -> dict[str, Any] | None:
    """Fetch URL via CloakBrowser shared browser. Returns dict with status/solution keys."""
    page = None
    try:
        _, context = _get_shared_browser(config)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=int(max_timeout))

        raw_cookies = context.cookies()
        cookies = [{"name": c["name"], "value": c.get("value", "")} for c in raw_cookies if c.get("name")]

        html = ""
        try:
            html = page.content()
        except Exception as e:
            logger.info(f"browser_engine: HTML extraction failed: {e}")

        final_url = page.url
        logger.info(f"browser_engine: ok, final_url={final_url}, html_len={len(html)}")
        return {
            "status": "ok",
            "solution": {
                "url": final_url,
                "status": 200,
                "response": html,
                "cookies": cookies,
            },
        }
    except Exception as e:
        logger.info(f"browser_engine: error - {e}")
        return None
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


def get_cookies(
    url: str,
    config: dict[str, Any],
    *,
    max_timeout: int = 60000,
) -> dict[str, str] | None:
    """Solve and return cookies as a dict."""
    result = solve_url(url, config, max_timeout=max_timeout)
    if not result:
        return None
    solution = result.get("solution", {})
    cookies = solution.get("cookies", [])
    if isinstance(cookies, list):
        return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}
    return None


def get_html(
    url: str,
    config: dict[str, Any],
    *,
    max_timeout: int = 60000,
) -> str | None:
    """Solve and return page HTML."""
    result = solve_url(url, config, max_timeout=max_timeout)
    if not result:
        return None
    return result.get("solution", {}).get("response")


def import_cookies(cookie_file: str | Path, config: dict[str, Any], *, domain_suffix: str | None = None) -> int:
    """Remember cookies and add them to the current thread's context, if any."""
    try:
        text = Path(cookie_file).read_text(encoding="utf-8")
    except Exception as e:
        logger.info(f"browser_engine: failed to read cookie file: {e}")
        return 0
    cookies = _parse_netscape_cookies(text)
    if not cookies:
        return 0
    if domain_suffix:
        cookies = [c for c in cookies if domain_suffix in c.get("domain", "")]
    cookies = [cookie for cookie in cookies if _is_valid_imported_cookie(cookie)]
    if not cookies:
        return 0

    path = _persistent_cookie_path(config)
    with _imported_cookie_path_lock(path):
        _remember_imported_cookies(
            config,
            _load_persisted_imported_cookies(config),
        )
        _remember_imported_cookies(config, cookies)
        persisted = _persist_imported_cookies(config)

    ctx = getattr(_tls, "context", None)
    if ctx is not None:
        try:
            ctx.add_cookies(cookies)
        except Exception as e:
            logger.info(
                "browser_engine: import_cookies failed on the current context; "
                f"retiring the thread-local browser: {e}"
            )
            shutdown_shared_browser()
            return len(cookies) if persisted else 0
    if ctx is None and not persisted:
        return 0
    logger.info(f"browser_engine: imported {len(cookies)} cookies")
    return len(cookies)


def evaluate_js(
    tab_id: str,
    expression: str,
    config: dict[str, Any],
    *,
    timeout: float = 15.0,
) -> Any:
    """Evaluate JavaScript expression in a browser page. Returns the JS result."""
    page = _resolve_tab(tab_id)
    if not page:
        logger.info(f"browser_engine: evaluate_js - tab {tab_id} not found")
        return None
    try:
        return page.evaluate(expression)
    except Exception as e:
        logger.info(f"browser_engine: evaluate_js error: {e}")
        return None


def create_tab(url: str, config: dict[str, Any], *, timeout: float = 30.0) -> str | None:
    """Create a new tab (page) in the shared browser and navigate to URL. Returns tab_id or None."""
    page = None
    try:
        browser, context = _get_shared_browser(config)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        tab_id = _register_tab(browser, context, page)
        return tab_id
    except Exception as e:
        logger.info(f"browser_engine: create_tab failed - {e}")
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        shutdown_shared_browser()
        return None


def close_tab(tab_id: str, config: dict[str, Any]) -> None:
    """Close a browser tab (page only, not the shared browser)."""
    with _tabs_lock:
        owner = _tab_owners.get(tab_id)
        if owner is not None and owner != _threading.get_ident():
            logger.warning(f"browser_engine: refusing cross-thread close for tab {tab_id}")
            return
        page = _tabs.get(tab_id)
    if page is None:
        return

    close_error = None
    for _attempt in range(2):
        try:
            page.close()
            close_error = None
            break
        except Exception as exc:
            close_error = exc

    if close_error is not None:
        logger.warning(
            f"browser_engine: failed to close tab {tab_id} after retry; "
            f"retiring the thread-local browser: {close_error}"
        )
        shutdown_shared_browser()
        return

    with _tabs_lock:
        if _tabs.get(tab_id) is page:
            _tabs.pop(tab_id, None)
            _tab_owners.pop(tab_id, None)
            _captured.pop(tab_id, None)
    tab_ids = getattr(_tls, "tab_ids", None)
    if tab_ids is not None:
        tab_ids.discard(tab_id)


def navigate_tab(tab_id: str, url: str, config: dict[str, Any], *, timeout: float = 30.0) -> bool:
    """Navigate an existing page to a new URL."""
    page = _resolve_tab(tab_id)
    if not page:
        logger.info(f"browser_engine: navigate_tab - tab {tab_id} not found")
        return False
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        return True
    except Exception as e:
        logger.info(f"browser_engine: navigate failed - {e}")
        return False


def get_snapshot(tab_id: str, config: dict[str, Any], *, timeout: float = 15.0) -> dict[str, Any]:
    """Get page content as a snapshot dict."""
    page = _resolve_tab(tab_id)
    if not page:
        return {"url": "", "snapshot": "", "error": "tab not found"}
    try:
        html = page.content()
        url = page.url
        return {"url": url, "snapshot": html, "status": 200}
    except Exception as e:
        return {"url": "", "snapshot": "", "error": str(e)}


def download_pdf_via_browser(
    pdf_url: str,
    output_path: Path,
    config: dict[str, Any],
    *,
    timeout: float = 60.0,
    cancel_event: _threading.Event | None = None,
) -> bool:
    """Download a PDF URL via CloakBrowser shared browser. Returns True on success.

    4-strategy cascade:
    1. Network response capture (from page.on("response"))
    2. In-browser fetch API with credentials
    3. PDF link discovery in page DOM
    4. Download button click
    """
    context = None
    page = None
    capture_lock = _threading.Lock()
    captured_response: dict[str, Any] = {
        "body": None,
        "in_progress": False,
        "accepting": True,
    }
    cancel_event = _effective_cancel_event(cancel_event)
    try:
        if _cancelled(cancel_event):
            return False
        _, context = _get_shared_browser(config)
        if _cancelled(cancel_event):
            return False
        page = context.new_page()

        def _on_response(response):
            with capture_lock:
                if (
                    not captured_response["accepting"]
                    or _cancelled(cancel_event)
                    or captured_response["body"] is not None
                    or captured_response["in_progress"]
                ):
                    return
                captured_response["in_progress"] = True
            try:
                ct = response.headers.get("content-type", "")
                if "pdf" in ct or "octet-stream" in ct:
                    body = response.body()
                    if body[:5] == b"%PDF-" and not _cancelled(cancel_event):
                        with capture_lock:
                            if (
                                captured_response["accepting"]
                                and captured_response["body"] is None
                            ):
                                captured_response["body"] = body
            except Exception:
                pass
            finally:
                with capture_lock:
                    captured_response["in_progress"] = False

        try:
            page.on("response", _on_response)
        except Exception:
            pass

        page.goto(pdf_url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        if _wait_or_cancel(cancel_event, 3):
            return False

        # Check for anti-bot challenges
        html = ""
        try:
            html = page.content()
        except Exception:
            pass
        lower_html = html.lower()
        if any(sig in lower_html for sig in [
            "cf-browser-verification", "challenge-platform",
            "just a moment", "attention required",
            "security check", "captcha",
            "请稍候", "正在验证", "checking your browser",
            # ALTCHA anti-bot verification (used by sci-hub.ru and other Sci-Hub mirrors)
            "altcha", "你是机器人吗", "not a robot", "nope",
        ]):
            logger.info("browser_engine: anti-bot challenge detected, waiting...")
            if _wait_or_cancel(cancel_event, 10):
                return False

        current_url = page.url
        if _cancelled(cancel_event):
            return False

        # Strategy 0: Network response capture
        with capture_lock:
            pdf_bytes = captured_response["body"]
            captured_response["body"] = None
        if pdf_bytes is not None and len(pdf_bytes) > 5000:
            if _write_pdf_bytes_atomic(output_path, pdf_bytes, cancel_event):
                logger.info(f"browser_engine: downloaded {len(pdf_bytes)} bytes via network capture")
                return True
            return False

        # Build candidate fetch paths
        fetch_paths: list[str] = []
        parsed = urlparse(str(current_url))
        origin = f"{parsed.scheme}://{parsed.netloc}"

        # Extract DOI from URL for pdfdirect construction
        doi_from_url = None
        path = parsed.path
        for prefix in ["/doi/", "/doi/pdf/", "/doi/pdfdirect/", "/doi/epdf/", "/articles/"]:
            if prefix in path:
                doi_from_url = path.split(prefix)[-1].split("?")[0].split("#")[0]
                break

        if doi_from_url:
            fetch_paths.append(f"/doi/pdfdirect/{doi_from_url}")
            fetch_paths.append(f"/doi/pdf/{doi_from_url}")
            fetch_paths.append(f"/content/pdf/{doi_from_url}.pdf")

        if _is_pdf_url(pdf_url):
            parsed_orig = urlparse(pdf_url)
            if parsed_orig.netloc == parsed.netloc:
                fetch_paths.append(parsed_orig.path)

        # Strategy 1: In-browser fetch API
        for fetch_path in fetch_paths:
            if _cancelled(cancel_event):
                return False
            logger.info(f"browser_engine: trying in-browser fetch {origin}{fetch_path[:60]}")
            try:
                pdf_b64 = page.evaluate(f"""
                    (async () => {{
                        try {{
                            const resp = await fetch('{fetch_path}', {{
                                credentials: 'include',
                                headers: {{'Accept': 'application/pdf,*/*'}}
                            }});
                            if (!resp.ok) return 'status:' + resp.status;
                            const ct = resp.headers.get('content-type') || '';
                            if (!ct.includes('pdf') && !ct.includes('octet')) return 'ct:' + ct;
                            const blob = await resp.blob();
                            return new Promise((resolve) => {{
                                const reader = new FileReader();
                                reader.onload = () => resolve(reader.result);
                                reader.readAsDataURL(blob);
                            }});
                        }} catch(e) {{
                            return 'error:' + e.message;
                        }}
                    }})()
                """)
                if _cancelled(cancel_event):
                    return False

                if isinstance(pdf_b64, str) and pdf_b64.startswith("data:"):
                    header, data = pdf_b64.split(",", 1)
                    pdf_bytes = base64.b64decode(data)
                    if pdf_bytes[:5] == b"%PDF-" and len(pdf_bytes) > 5000:
                        if _write_pdf_bytes_atomic(output_path, pdf_bytes, cancel_event):
                            logger.info(f"browser_engine: downloaded {len(pdf_bytes)} bytes via in-browser fetch")
                            return True
                        return False
                    else:
                        logger.info(f"browser_engine: fetch returned non-PDF ({len(pdf_bytes)} bytes)")
                else:
                    logger.info(f"browser_engine: fetch result: {str(pdf_b64)[:80]}")
            except Exception as e:
                logger.info(f"browser_engine: fetch error: {e}")

        # Strategy 2: PDF link discovery in DOM
        if _cancelled(cancel_event):
            return False
        try:
            pdf_link = page.evaluate("""
                (() => {
                    for (const el of document.querySelectorAll('iframe, embed, object')) {
                        const src = el.src || el.data || '';
                        if (src.includes('.pdf') && !src.includes('supplement') && !src.includes('Suppl')) return src;
                    }
                    const viewer = document.querySelector('#viewer, .pdfViewer, [data-l10n-id="download"]');
                    if (viewer) return window.location.href;
                    for (const a of document.querySelectorAll('a[href]')) {
                        const href = (a.href || '').toLowerCase();
                        const text = (a.innerText || '').toLowerCase();
                        if (href.includes('supplement') || href.includes('supporting') || href.includes('downloadsupplement') || href.includes('pb-assets')) continue;
                        if (text.includes('supplement') || text.includes('supporting info')) continue;
                        if (href.includes('.pdf') || href.includes('/pdf/') || href.includes('pdfdirect')) {
                            if (a.href.startsWith('http')) return a.href;
                        }
                    }
                    const sdPdf = document.querySelector('a[aria-label*="PDF"], a[aria-label*="pdf"], a.pdf-download-btn-link');
                    if (sdPdf) return sdPdf.href;
                    return null;
                })()
            """)
            if _cancelled(cancel_event):
                return False

            if pdf_link and isinstance(pdf_link, str) and pdf_link.startswith("http"):
                logger.info(f"browser_engine: found PDF link: {pdf_link[:80]}")
                parsed_link = urlparse(pdf_link)
                link_path = parsed_link.path + ("?" + parsed_link.query if parsed_link.query else "")
                if parsed_link.netloc == parsed.netloc:
                    try:
                        pdf_b64 = page.evaluate(f"""
                            (async () => {{
                                try {{
                                    const resp = await fetch('{link_path}', {{
                                        credentials: 'include',
                                        headers: {{'Accept': 'application/pdf,*/*'}}
                                    }});
                                    if (!resp.ok) return 'status:' + resp.status;
                                    const ct = resp.headers.get('content-type') || '';
                                    if (!ct.includes('pdf') && !ct.includes('octet')) return 'ct:' + ct;
                                    const blob = await resp.blob();
                                    return new Promise((resolve) => {{
                                        const reader = new FileReader();
                                        reader.onload = () => resolve(reader.result);
                                        reader.readAsDataURL(blob);
                                    }});
                                }} catch(e) {{
                                    return 'error:' + e.message;
                                }}
                            }})()
                        """)
                        if _cancelled(cancel_event):
                            return False

                        if isinstance(pdf_b64, str) and pdf_b64.startswith("data:"):
                            header, data = pdf_b64.split(",", 1)
                            pdf_bytes = base64.b64decode(data)
                            if pdf_bytes[:5] == b"%PDF-" and len(pdf_bytes) > 5000:
                                if _write_pdf_bytes_atomic(output_path, pdf_bytes, cancel_event):
                                    logger.info(f"browser_engine: downloaded {len(pdf_bytes)} bytes via PDF link fetch")
                                    return True
                                return False
                    except Exception as e:
                        logger.info(f"browser_engine: PDF link fetch error: {e}")
        except Exception as e:
            logger.info(f"browser_engine: DOM scan error: {e}")

        # Strategy 3: Click download button
        if _cancelled(cancel_event):
            return False
        try:
            clicked = page.evaluate("""
                (() => {
                    for (const btn of document.querySelectorAll(
                        '#download, [aria-label*="download" i], [aria-label*="PDF" i], .pdf-download-btn-link, a[data-aa-name="download-pdf"]'
                    )) {
                        if (btn.offsetParent !== null) { btn.click(); return true; }
                    }
                    return false;
                })()
            """)
            if _cancelled(cancel_event):
                return False
            if clicked:
                logger.info("browser_engine: clicked download button, waiting...")
                if _wait_or_cancel(cancel_event, 5):
                    return False
        except Exception as e:
            logger.info(f"browser_engine: click error: {e}")

        return False
    except Exception as e:
        logger.info(f"browser_engine: download_pdf_via_browser error: {e}")
        if context is not None and page is None:
            shutdown_shared_browser()
        return False
    finally:
        with capture_lock:
            captured_response["accepting"] = False
            captured_response["body"] = None
        if page is not None:
            try:
                page.remove_listener("response", _on_response)
            except Exception:
                pass
            close_error = None
            for _attempt in range(2):
                try:
                    page.close()
                    close_error = None
                    break
                except Exception as exc:
                    close_error = exc
            if close_error is not None:
                logger.warning(
                    "browser_engine: failed to close PDF download page after retry; "
                    f"retiring the thread-local browser: {close_error}"
                )
                shutdown_shared_browser()


# Backward-compat alias
download_pdf_via_camofox = download_pdf_via_browser


def _is_pdf_url(url: str) -> bool:
    """Check if a URL looks like a direct PDF link."""
    lower = url.lower()
    return (
        lower.endswith(".pdf")
        or "/pdf/" in lower
        or "content/pdf" in lower
        or "pdfdirect" in lower
        or "/doi/pdf/" in lower
        or "type=printable" in lower
    )


def fetch_url(
    tab_id: str,
    url: str,
    config: dict[str, Any],
    *,
    timeout: float = 30.0,
) -> dict[str, Any] | None:
    """Navigate tab to URL and capture any PDF responses from the network layer."""
    page = _resolve_tab(tab_id)
    if not page:
        logger.info(f"browser_engine: fetch_url - tab {tab_id} not found")
        return None

    response = None
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
    except Exception as e:
        logger.info(f"browser_engine: fetch_url navigate error: {e}")

    if response is not None:
        try:
            body = response.body()
            if body[:5] == b"%PDF-" and len(body) > 5000:
                return {"status": "ok", "bytes": len(body), "data": body}
        except Exception:
            pass

    return None


def get_captured_responses(
    tab_id: str,
    config: dict[str, Any],
    *,
    consume: bool = True,
) -> list[dict[str, Any]]:
    """Get captured PDF responses for a tab. Optionally consume (clear) them."""
    with _tabs_lock:
        captured = _captured.get(tab_id, [])
        result = list(captured)
        if consume and tab_id in _captured:
            _captured[tab_id] = []
    return result


def close_all_tabs(config: dict[str, Any]) -> None:
    """Close tabs owned by the current Playwright thread."""
    owner = _threading.get_ident()
    with _tabs_lock:
        tab_ids = [
            tab_id
            for tab_id, tab_owner in _tab_owners.items()
            if tab_owner == owner
        ]
    for tab_id in tab_ids:
        close_tab(tab_id, config)


# ---------------------------------------------------------------------------
# Netscape cookie parser (shared utility)
# ---------------------------------------------------------------------------

def _parse_netscape_cookies(text: str) -> list[dict[str, Any]]:
    """Parse Netscape-format cookie file into structured cookie objects."""
    cookies: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line.removeprefix("#HttpOnly_")
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain = parts[0]
        cookie_path = parts[2]
        secure = parts[3].upper() == "TRUE"
        try:
            expires = int(parts[4])
        except (ValueError, IndexError):
            expires = 0
        name = parts[5]
        value = "\t".join(parts[6:])
        cookie: dict[str, Any] = {
            "name": name, "value": value,
            "domain": domain, "path": cookie_path,
            "secure": secure, "expires": expires,
        }
        if http_only:
            cookie["httpOnly"] = True
        cookies.append(cookie)
    return cookies
