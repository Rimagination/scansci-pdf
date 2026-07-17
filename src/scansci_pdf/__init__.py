"""ScanSci PDF - Academic paper downloader MCP server."""

import os as _os
import sys as _sys

# -- Auto-fix: ZCode proxy CA cert breaks SSL verification -------------------
# ZCode's traffic proxy sets HTTP_PROXY/HTTPS_PROXY to 127.0.0.1:64844 and
# SSL_CERT_FILE to its own root CA. requests' trust_env=True routes all HTTPS
# through the proxy tunnel, but the proxy's SSL context fails to verify real
# server certs. We disable proxy usage project-wide so HTTPS goes direct.

# 1. Block proxy usage (must run unconditionally, before any other import)
_os.environ["NO_PROXY"] = "*"
_os.environ["no_proxy"] = "*"

def _fix_ssl_cert_file() -> None:
    _cert_file = _os.environ.get("SSL_CERT_FILE", "")
    if not _cert_file or "root-ca-cert" not in _cert_file:
        return
    _merged = _os.path.join(_os.path.dirname(_cert_file), "..", "..", "ca-bundle-merged.pem")
    _merged = _os.path.normpath(_merged)
    if not _os.path.exists(_merged):
        try:
            import certifi
            with open(_cert_file, "rb") as _f:
                _zc = _f.read()
            with open(certifi.where(), "rb") as _f:
                _rc = _f.read()
            with open(_merged, "wb") as _f:
                _f.write(_zc + b"\n")
                _f.write(_rc)
        except Exception:
            return
    # 2. Set merged CA bundle (proxy CA + real CAs) for direct HTTPS
    _os.environ["SSL_CERT_FILE"] = _merged
    _os.environ["REQUESTS_CA_BUNDLE"] = _merged
    _os.environ["CURL_CA_BUNDLE"] = _merged
    # 3. Monkey-patch SSLContext.load_default_certs for urllib3 direct usage
    import ssl as _ssl
    _orig_load_default_certs = _ssl.SSLContext.load_default_certs
    def _patched_load_default_certs(self, *args, **kwargs):
        _orig_load_default_certs(self, *args, **kwargs)
        self.load_verify_locations(_merged)
    _ssl.SSLContext.load_default_certs = _patched_load_default_certs

_fix_ssl_cert_file()

__version__ = "1.9.0"

__all__ = [
    "__version__",
    "download",
    "batch_download",
    "search_papers",
    "load_config",
    "update_config",
    "get_config_safe",
]

from .sources import download, batch_download
from .search import search_papers
from .config import load_config, update_config, get_config_safe
