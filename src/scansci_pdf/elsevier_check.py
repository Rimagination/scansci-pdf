"""Elsevier API key 权限自检与逐篇探测（纯查询，零正文下载）。

标准化流程（对应 docs/PLAYBOOK.md 的 key 权限自检）：
1. 固定自检样本：大刊（Lancet）/ OA 对照 / 无 key 基线 —— 诊断 key 本身
2. 用户样本：--doi 或 --file 逐篇双路由探测 —— 诊断具体文献
3. 产出规范表格（固定 7 列，判定为规范化枚举）+ key 画像汇总

判定枚举（杜绝自由文本）：
ENTITLED / NOT_ENTITLED / NOT_FOUND / QUOTA / NO_KEY / NETWORK
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests

PREMIUM_DOI = "10.1016/S0140-6736(20)30183-5"        # Lancet 知名付费论文（2026-08 实测 200）
OA_CONTROL_DOI = "10.1016/j.jenvman.2023.118901"      # 无 key 可得的公开对照（实测 200）

ENTITLED = "ENTITLED"
NOT_ENTITLED = "NOT_ENTITLED"
NOT_FOUND = "NOT_FOUND"
QUOTA = "QUOTA"
NO_KEY = "NO_KEY"
NETWORK = "NETWORK"

PROXY_ROUTE = "proxy"
DIRECT_ROUTE = "direct"

# 判定 → 建议通道（规范化映射，自动给结论）
ROUTE_ADVICE = {
    ENTITLED: "走 Elsevier API 车道（可达路由）",
    NOT_ENTITLED: "key 无此刊权益——机构订阅确认，或转 OA/灰色通道",
    NOT_FOUND: "DOI 未注册或写错——人工核对",
    QUOTA: "配额限流——冷却 15s/篇 节流重试",
    NO_KEY: "未配置 API key——运行 elsevier-setup",
    NETWORK: "网络不可达——检查代理连通性",
}


@dataclass
class ProbeRow:
    doi: str
    sample_type: str
    proxy: str
    direct: str
    verdict: str
    route_advice: str
    note: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


def normalize_status(code: int | None) -> str:
    """HTTP 状态码 → 规范化判定枚举。"""
    return {
        200: ENTITLED,
        401: NOT_ENTITLED,
        403: NOT_ENTITLED,
        404: NOT_FOUND,
        429: QUOTA,
        406: NO_KEY,
    }.get(code, NETWORK)


def _head_probe(doi: str, key: str, proxies: dict | None, *, http_accept: bool = True) -> tuple[str, str]:
    """HEAD 探针：200=有权获取 PDF（不下载），403=无权限，406=缺 key。

    http_accept=False 时不带 httpAccept 参数——用于 OA 对照（公开文章
    无 key 纯 HEAD 即 200；带参数反而 406）。
    """
    try:
        headers: dict[str, str] = {"Accept": "application/pdf", **UA_HEADERS}
        if key:
            headers["X-ELS-APIKey"] = key
        params = {"httpAccept": "application/pdf"} if http_accept else None
        r = requests.head(
            f"https://api.elsevier.com/content/article/doi/{doi}",
            params=params,
            headers=headers, timeout=(8, 12), proxies=proxies, allow_redirects=True,
        )
        return r.status_code, normalize_status(r.status_code)
    except Exception as e:
        return 0, f"{NETWORK}({type(e).__name__})"


def probe_dual_route(doi: str, key: str, config: dict[str, Any]) -> dict[str, Any]:
    """单篇双路由探测（代理 + 直连）。返回规范行 dict。"""
    proxy = config.get("network_proxy", "")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    p_code, p_verdict = _head_probe(doi, key, proxies)
    time.sleep(0.3)
    d_code, d_verdict = _head_probe(doi, key, None)
    verdict, advice = _combine(p_verdict, d_verdict)
    return {"doi": doi, "sample_type": "用户样本", "proxy": f"{p_code} {p_verdict}",
            "direct": f"{d_code} {d_verdict}", "verdict": verdict,
            "route_advice": advice, "note": ""}


def _combine(p: str, d: str) -> tuple[str, str]:
    """双路由判定合并：任一路由 ENTITLED 即可得。"""
    for v in (p, d):
        if v.startswith(ENTITLED):
            return ENTITLED, ROUTE_ADVICE[ENTITLED]
    for v in (p, d):
        if v.startswith(QUOTA):
            return QUOTA, ROUTE_ADVICE[QUOTA]
    if p.startswith(NOT_FOUND) and d.startswith(NOT_FOUND):
        return NOT_FOUND, ROUTE_ADVICE[NOT_FOUND]
    if NOT_ENTITLED in (p.split("(")[0], d.split("(")[0]):
        return NOT_ENTITLED, ROUTE_ADVICE[NOT_ENTITLED]
    return NETWORK, ROUTE_ADVICE[NETWORK]


def check_key_profile(config: dict[str, Any]) -> tuple[list[dict[str, Any]], str, str]:
    """固定自检样本 → (规范行, key 画像判定, 建议)。

    画像四选一：广覆盖机构key / key有效但无该订阅 / 无效key / 网络不可达
    """
    key = (config.get("elsevier_api_key") or "").strip()
    proxy = config.get("network_proxy", "")
    proxies = {"http": proxy, "https": proxy} if proxy else None

    rows: list[dict[str, Any]] = []

    # 1) 无 key 基线（证明端点连通且权限来自 key）
    base_code, base_verdict = _head_probe(PREMIUM_DOI, "", proxies)
    rows.append({"doi": PREMIUM_DOI, "sample_type": "自检-无key基线",
                 "proxy": f"{base_code} {base_verdict}", "direct": "-", "verdict": base_verdict,
                 "route_advice": "—", "note": "预期 406/NO_KEY"})

    # 2) OA 对照（无 key 应可得：证明端点工作且该篇公开）
    code_oa, verdict_oa = _head_probe(OA_CONTROL_DOI, "", proxies, http_accept=False)
    rows.append({"doi": OA_CONTROL_DOI, "sample_type": "自检-OA对照",
                 "proxy": f"{code_oa} {verdict_oa}", "direct": "-", "verdict": verdict_oa,
                 "route_advice": "—", "note": "预期 200/ENTITLED"})

    # 3) 大刊样本（有 key，双路由）：测订阅广度
    key_rows = []
    for route, px in ((PROXY_ROUTE, proxies), (DIRECT_ROUTE, None)):
        code, verdict = _head_probe(PREMIUM_DOI, key, px)
        key_rows.append((route, code, verdict))
        rows.append({"doi": PREMIUM_DOI, "sample_type": f"自检-大刊({route})",
                     "proxy": f"{code} {verdict}" if route == PROXY_ROUTE else "-",
                     "direct": "-" if route == PROXY_ROUTE else f"{code} {verdict}",
                     "verdict": verdict, "route_advice": "—", "note": "预期 200/ENTITLED"})

    ent = any(v == ENTITLED for _, _, v in key_rows)
    base_ok = base_verdict == NO_KEY or base_code in (401, 403, 406)
    oa_ok = verdict_oa == ENTITLED or code_oa == 200

    if not base_ok and not oa_ok:
        profile, advice = "网络/端点不可达", "检查代理连通性后重试"
    elif ent:
        profile = "广覆盖机构 key"
        advice = "Elsevier 车道可用：API 优先，批量走快车道"
    elif oa_ok:
        profile = "key 有效但无该刊订阅"
        advice = "该 key 未绑定机构权益或机构未订阅——用机构身份重新注册 key，或依赖 OA/灰色通道"
    else:
        profile = "无效 key"
        advice = "重新运行 elsevier-setup --api-key 配置有效 key"

    return rows, profile, advice


def check_dois(dois: list[str], config: dict[str, Any], progress: Any = None) -> list[dict[str, Any]]:
    """用户样本逐篇双路由探测（规范化行）。"""
    key = (config.get("elsevier_api_key") or "").strip()
    out = []
    for i, doi in enumerate(dois, 1):
        row = probe_dual_route(doi, key, config)
        row["sample_type"] = "用户样本"
        out.append(row)
        if progress is not None:
            progress.advance(row["verdict"] == ENTITLED, current=doi)
    return out


# UA 常量放底部避免循环导入（network.py 不导入本模块，可安全顶部导入；
# 这里保守处理，保持与其它模块一致）
UA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151.0 Safari/537.36"}
