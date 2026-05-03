"""Adaptive source scoring with exponential moving average (EMA).

Tracks per-source success rate and latency. Uses EMA so:
- Recent results have higher weight
- Temporary network blips don't permanently ruin a source's score
- Scores naturally recover when a source starts working again
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..config import DATA_DIR

_SCORES_FILE = DATA_DIR / "source_scores.json"

# EMA decay factor: higher = more weight on recent data (0.05-0.2 typical)
_ALPHA = 0.1

# Initial score for unknown sources (neutral)
_DEFAULT_SCORE = 0.5


def _load_scores() -> dict[str, dict[str, Any]]:
    if _SCORES_FILE.exists():
        try:
            with _SCORES_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_scores(scores: dict[str, dict[str, Any]]) -> None:
    _SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _SCORES_FILE.open("w", encoding="utf-8") as f:
        json.dump(scores, f, indent=2, ensure_ascii=False)


def record_result(source: str, success: bool, latency_ms: float = 0, error_type: str = "") -> None:
    """Record a download attempt result for adaptive scoring."""
    scores = _load_scores()
    entry = scores.get(source, {
        "success_ema": _DEFAULT_SCORE,
        "latency_ema": 5000.0,
        "attempts": 0,
        "last_error": "",
        "last_update": 0,
    })

    # Update success EMA
    success_val = 1.0 if success else 0.0
    entry["success_ema"] = _ALPHA * success_val + (1 - _ALPHA) * entry["success_ema"]

    # Update latency EMA (only on success)
    if success and latency_ms > 0:
        entry["latency_ema"] = _ALPHA * latency_ms + (1 - _ALPHA) * entry["latency_ema"]

    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["last_error"] = error_type if not success else ""
    entry["last_update"] = int(time.time())

    scores[source] = entry
    _save_scores(scores)


def get_score(source: str) -> float:
    """Get adaptive score for a source (0.0-1.0, higher = better)."""
    scores = _load_scores()
    entry = scores.get(source)
    if not entry:
        return _DEFAULT_SCORE
    return entry.get("success_ema", _DEFAULT_SCORE)


def get_latency(source: str) -> float:
    """Get EMA latency for a source in ms."""
    scores = _load_scores()
    entry = scores.get(source)
    if not entry:
        return 5000.0
    return entry.get("latency_ema", 5000.0)


def sort_sources(sources: list[tuple[Any, str]]) -> list[tuple[Any, str]]:
    """Sort sources by adaptive score (best first), with latency as tiebreaker."""
    def _key(item: tuple[Any, str]) -> tuple[float, float]:
        _, label = item
        score = get_score(label)
        latency = get_latency(label)
        # Higher score first, lower latency first
        return (-score, latency)
    return sorted(sources, key=_key)


def classify_error(resp_status: int = 0, exception: Exception | None = None, html: str = "") -> str:
    """Classify download error into a category."""
    if resp_status == 404:
        return "not_found"
    if resp_status == 403:
        return "forbidden"
    if resp_status == 429:
        return "rate_limited"
    if resp_status >= 500:
        return "server_error"
    if exception and "timeout" in str(exception).lower():
        return "timeout"
    if exception and "ssl" in str(exception).lower():
        return "ssl_error"
    if html and ("captcha" in html.lower() or "challenge" in html.lower()):
        return "captcha"
    return "unknown"


def get_user_advice(error_type: str, source: str) -> str:
    """Return user-friendly advice with actionable steps."""
    advice = {
        "not_found": "论文在此源不存在（404），跳过",
        "forbidden": "访问被拒绝（403）→ 建议：1) 配置代理 config_set network_proxy 2) 启用 WebVPN 3) 换用 strategy=legal_only",
        "rate_limited": "请求过于频繁（429）→ 建议：稍后重试，或配置 openalex_api_key 提升配额",
        "timeout": "连接超时 → 建议：1) 检查网络连通性 2) 配置代理绕过封锁 config_set network_proxy 3) 启用 Tor（tor_start）",
        "captcha": "触发 Cloudflare 防护 → 建议：1) 安装 FlareSolverr（docker run -d ghcr.io/flaresolverr/flaresolverr）2) 配置代理",
        "ssl_error": "SSL 连接错误 → 建议：1) 检查代理是否正确 2) 尝试不同代理协议（socks5/http）3) 更新证书",
        "server_error": "服务器错误（5xx）→ 暂时不可用，稍后重试",
        "dns_blocked": "DNS 解析失败 → 建议：1) 配置代理 config_set network_proxy 2) 更换 DNS（8.8.8.8）3) 启用 Tor",
        "network_blocked": "网络完全不通 → 建议：1) 检查代理配置 config_set network_proxy 2) 使用 WebVPN 机构代理 3) 换用海外网络",
    }
    return advice.get(error_type, "未知错误 → 建议：运行 network_diagnose 检查网络状态")


def get_all_scores() -> dict[str, dict[str, Any]]:
    """Return all source scores for diagnostics."""
    return _load_scores()


def diagnose_network(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run network diagnostics and return actionable report."""
    import os
    import socket

    report: dict[str, Any] = {
        "proxy": {"configured": False, "source": "none", "url": ""},
        "tests": [],
        "recommendations": [],
    }

    # Check proxy configuration
    env_proxy = os.environ.get("SCANSCI_PDF_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    cfg_proxy = (config or {}).get("network_proxy", "")
    active_proxy = env_proxy or cfg_proxy

    if active_proxy:
        report["proxy"] = {"configured": True, "source": "env" if env_proxy else "config", "url": active_proxy}
    else:
        # Check if system has proxy env vars that scansci-pdf ignores
        sys_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or ""
        if sys_proxy:
            report["recommendations"].append(
                f"检测到系统代理 {sys_proxy}，但 scansci-pdf 未使用。"
                f"运行: scansci-pdf config_set network_proxy \"{sys_proxy}\""
            )
        else:
            report["recommendations"].append(
                "未配置代理。如需访问被封锁的网站，运行: scansci-pdf config_set network_proxy \"socks5://127.0.0.1:1080\""
            )

    # Test DNS resolution
    test_domains = [
        ("sci-hub.st", "Sci-Hub"),
        ("api.openalex.org", "OpenAlex"),
        ("doi.org", "DOI"),
    ]
    for domain, label in test_domains:
        try:
            ip = socket.gethostbyname(domain)
            report["tests"].append({"target": label, "domain": domain, "dns": "ok", "ip": ip})
        except socket.gaierror:
            report["tests"].append({"target": label, "domain": domain, "dns": "failed", "ip": ""})
            report["recommendations"].append(f"DNS 解析 {domain} 失败 → 可能被封锁，建议配置代理")

    # Test TCP connectivity
    tcp_tests = [
        ("sci-hub.st", 443, "Sci-Hub HTTPS"),
        ("api.openalex.org", 443, "OpenAlex HTTPS"),
    ]
    for host, port, label in tcp_tests:
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.close()
            report["tests"].append({"target": label, "tcp": "ok"})
        except Exception as e:
            report["tests"].append({"target": label, "tcp": "failed", "error": str(e)[:50]})
            if "timed out" in str(e).lower():
                report["recommendations"].append(f"{label} 连接超时 → 网络被封锁，强烈建议配置代理")

    # Test proxy connectivity
    if active_proxy:
        try:
            import requests
            resp = requests.get("https://sci-hub.st", timeout=10,
                              proxies={"https": active_proxy, "http": active_proxy},
                              headers={"User-Agent": "Mozilla/5.0"})
            report["tests"].append({"target": "Sci-Hub via proxy", "status": resp.status_code})
            if resp.status_code == 200:
                report["recommendations"].append("代理访问 Sci-Hub 正常 ✓")
        except Exception as e:
            report["tests"].append({"target": "Sci-Hub via proxy", "error": str(e)[:50]})
            report["recommendations"].append(f"代理访问 Sci-Hub 失败 → 检查代理是否正确: {active_proxy}")

    # Check Tor status
    try:
        from ..tor import check_tor_circuit
        tor_ok = check_tor_circuit(config)
        report["tests"].append({"target": "Tor SOCKS5", "status": "running" if tor_ok else "not running"})
        if not tor_ok:
            report["recommendations"].append("Tor 未运行 → 如需匿名访问，运行: scansci-pdf tor_start")
    except Exception:
        report["tests"].append({"target": "Tor SOCKS5", "status": "unknown"})

    # Check FlareSolverr
    try:
        import requests
        resp = requests.get("http://localhost:8191/", timeout=3)
        report["tests"].append({"target": "FlareSolverr", "status": "running"})
    except Exception:
        report["tests"].append({"target": "FlareSolverr", "status": "not running"})
        report["recommendations"].append(
            "FlareSolverr 未运行 → 如遇 Cloudflare 封锁，运行: docker run -d -p 8191:8191 ghcr.io/flareSolverr/flareSolverr"
        )

    # Summary
    failed_tests = [t for t in report["tests"] if t.get("dns") == "failed" or t.get("tcp") == "failed"]
    if not failed_tests and not active_proxy:
        report["summary"] = "网络正常，直连可用"
    elif not failed_tests and active_proxy:
        report["summary"] = "网络正常，代理工作正常"
    elif failed_tests and active_proxy:
        report["summary"] = "部分连接失败，代理可能需要调整"
    else:
        report["summary"] = "网络受限，强烈建议配置代理"

    return report
