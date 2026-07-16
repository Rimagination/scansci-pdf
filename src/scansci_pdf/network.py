"""Network session management and HTTP helpers."""

from __future__ import annotations

import os
import random
import threading
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from .config import load_config
from .log import get_logger

log = get_logger()

# Check SOCKS proxy support
try:
    import socks  # noqa: F401
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 ScanSci PDF/3"
)

CLOUDFLARE_CHALLENGE_SIGNALS = (
    "just a moment", "attention required", "verify", "security check",
    "请稍候", "正在验证", "checking", "cloudflare",
)


def is_cloudflare_challenge(title: str) -> bool:
    """Check if a page title indicates a Cloudflare/anti-bot challenge."""
    lower = title.lower()
    return any(sig in lower for sig in CLOUDFLARE_CHALLENGE_SIGNALS)

_session_pool: dict[str, requests.Session] = {}
_session_pool_lock = threading.Lock()


def _get_session(config: dict[str, Any]) -> requests.Session:
    proxy = os.environ.get("SCANSCI_PDF_PROXY") or config.get("network_proxy") or ""
    key = proxy or "__none__"
    with _session_pool_lock:
        session = _session_pool.get(key)
        if session is None:
            session = requests.Session()
            session.trust_env = False
            adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=0)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.headers.update({"User-Agent": USER_AGENT})
            if proxy:
                if proxy.startswith("socks") and not HAS_SOCKS:
                    log.warning("SOCKS proxy configured but PySocks not installed. Install: pip install requests[socks]")
                session.proxies = {"http": proxy, "https": proxy}
            _session_pool[key] = session
    return session


def request_timeout(config: dict[str, Any]) -> tuple[int, int]:
    return (int(config.get("connect_timeout", 3)), int(config.get("read_timeout", 7)))


def proxy_dict(proxy: str | None) -> dict[str, str] | None:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


_LIBGEN_HOSTS = {"libgen.li", "libgen.bz", "libgen.gs", "libgen.rs", "libgen.st"}


def _is_scihub_libgen_url(url: str, config: dict[str, Any]) -> bool:
    """Check if URL belongs to a Sci-Hub or LibGen domain."""
    from urllib.parse import urlparse
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    # LibGen mirrors
    if any(host.endswith(h) for h in _LIBGEN_HOSTS):
        return True
    # Sci-Hub domains from config
    for domain in config.get("scihub_domains") or []:
        try:
            d_host = urlparse(domain).netloc.lower()
            if host == d_host or host.endswith("." + d_host):
                return True
        except Exception:
            continue
    # Fallback: known Sci-Hub patterns
    return "sci-hub" in host


def select_proxy_for_url(
    url: str,
    config: dict[str, Any],
    use_tor: bool = False,
    cancel_event: Any = None,
) -> str | None:
    if use_tor:
        from .tor import ensure_tor
        tor_proxy = ensure_tor(config, cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            return None
        if tor_proxy:
            return tor_proxy
        log.warning("Tor requested but unavailable — falling back to direct connection")

    explicit = os.environ.get("SCANSCI_PDF_PROXY") or config.get("network_proxy")
    if not explicit:
        return None

    # When proxy_only_scihub is enabled, only route Sci-Hub/LibGen through proxy
    if config.get("proxy_only_scihub", False):
        if not _is_scihub_libgen_url(url, config):
            return None

    return explicit


def fetch(
    url: str,
    config: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    stream: bool = False,
    method: str = "GET",
    use_tor: bool = False,
    cancel_event: Any = None,
) -> requests.Response:
    if cancel_event is not None and cancel_event.is_set():
        raise requests.RequestException("request cancelled")
    domain_rate_limit(url)
    host_concurrency_acquire(url, config)
    released = False
    release_lock = threading.Lock()

    def release_slot() -> None:
        nonlocal released
        with release_lock:
            if not released:
                released = True
                host_concurrency_release(url, config)

    try:
        merged_headers = {"User-Agent": USER_AGENT}
        if headers:
            merged_headers.update(headers)
        session = _get_session(config)
        proxies = proxy_dict(
            select_proxy_for_url(
                url,
                config,
                use_tor=use_tor,
                cancel_event=cancel_event,
            )
        )
        if cancel_event is not None and cancel_event.is_set():
            raise requests.RequestException("request cancelled")
        response = session.request(
            method,
            url,
            headers=merged_headers,
            timeout=request_timeout(config),
            proxies=proxies,
            allow_redirects=True,
            stream=stream,
        )
        if not stream:
            release_slot()
            return response

        original_close = response.close
        original_iter_content = response.iter_content

        def close() -> None:
            try:
                original_close()
            finally:
                release_slot()

        def iter_content(*args, **kwargs):
            try:
                yield from original_iter_content(*args, **kwargs)
            finally:
                release_slot()

        response.close = close
        response.iter_content = iter_content
        return response
    except Exception:
        release_slot()
        raise


def fetch_json(
    url: str,
    config: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    use_tor: bool = False,
) -> dict[str, Any] | None:
    request_headers = {"Accept": "application/json", **(headers or {})}
    resp = None
    try:
        resp = fetch(
            url,
            config,
            headers=request_headers,
            use_tor=use_tor,
        )
        if resp.status_code >= 400:
            return None
        data = resp.json()
    except Exception:
        return None
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                # Cleanup is best-effort here: a non-requests response used by
                # an adapter or test double may not expose a working close().
                # Never turn a successfully decoded JSON response into a
                # failed probe solely because cleanup itself failed.
                pass
    return data


def _is_cloudflare_block(resp: requests.Response) -> bool:
    """Check if response is a Cloudflare/anti-bot challenge."""
    if resp.status_code in (403, 503):
        server = resp.headers.get("server", "").lower()
        if "cloudflare" in server:
            return True
        try:
            body = resp.text[:2000].lower()
            if "cf-browser-verification" in body or "challenge-platform" in body:
                return True
        except Exception:
            pass
    return False


def polite_delay(config: dict[str, Any]) -> None:
    # Polite delay is opt-in: only sleep when fixed_request_delay_enabled is True.
    # Without it, racing/parallel sources would be needlessly slowed.
    if config.get("fixed_request_delay_enabled") is not True:
        return
    lo = float(config.get("request_delay_min", 0))
    hi = float(config.get("request_delay_max", 0))
    if hi > 0:
        time.sleep(random.uniform(lo, max(lo, hi)))


# ============================================================
# Per-domain rate limiting: max 1 request per domain per interval
# ============================================================
_domain_locks: dict[str, threading.Lock] = {}
_domain_last_request: dict[str, float] = {}
_DOMAIN_RATE_LIMIT = 0.5  # seconds between requests to same domain


def _extract_domain(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return url


def domain_rate_limit(url: str) -> None:
    """Wait if needed to respect per-domain rate limit."""
    domain = _extract_domain(url)
    if not domain:
        return
    if domain not in _domain_locks:
        _domain_locks[domain] = threading.Lock()
    with _domain_locks[domain]:
        now = time.time()
        last = _domain_last_request.get(domain, 0)
        elapsed = now - last
        if elapsed < _DOMAIN_RATE_LIMIT:
            time.sleep(_DOMAIN_RATE_LIMIT - elapsed)
        _domain_last_request[domain] = time.time()


# ============================================================
# Per-host concurrency control (scimesh-style)
# ============================================================
_DEFAULT_HOST_CONCURRENCY: dict[str, int] = {
    "unpaywall.org": 3,
    "api.openalex.org": 5,
    "content.openalex.org": 3,
    "api.crossref.org": 5,
    "api.openaire.eu": 3,
    "doaj.org": 3,
    "api.semanticscholar.org": 3,
    "api.core.ac.uk": 3,
    "europepmc.org": 3,
    "www.ebi.ac.uk": 3,
    "arxiv.org": 2,
    "export.arxiv.org": 2,
}

_host_semaphores: dict[str, threading.Semaphore] = {}


def _get_host_concurrency(host: str, config: dict[str, Any]) -> int:
    """Get max concurrent requests for a host."""
    custom = config.get("host_concurrency", {})
    if isinstance(custom, dict) and host in custom:
        return int(custom[host])
    # Match by suffix (e.g., "sci-hub.ru" matches default)
    for pattern, limit in _DEFAULT_HOST_CONCURRENCY.items():
        if host == pattern or host.endswith("." + pattern):
            return limit
    return 0


def host_concurrency_acquire(url: str, config: dict[str, Any]) -> None:
    """Acquire a per-host concurrency slot. Blocks if at limit."""
    host = _extract_domain(url)
    if not host:
        return
    limit = _get_host_concurrency(host, config)
    if limit <= 0:
        return
    if host not in _host_semaphores:
        _host_semaphores[host] = threading.Semaphore(limit)
    _host_semaphores[host].acquire()


def host_concurrency_release(url: str, config: dict[str, Any]) -> None:
    """Release a per-host concurrency slot."""
    host = _extract_domain(url)
    if not host:
        return
    if host in _host_semaphores:
        _host_semaphores[host].release()
