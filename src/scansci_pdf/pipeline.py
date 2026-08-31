"""Unified job pipeline: normalize any input into a channel-annotated download queue.

Queue file format (one paper per line, TAB-separated):

    identifier                          # plain DOI / arXiv ID
    identifier<TAB>channel              # with explicit channel hint
    identifier<TAB>channel<TAB>oa_url   # with a known open-access PDF URL

``channel`` is one of ``oa | elsevier | grey | institution | auto``. Lines
starting with ``#`` are comments. Plain DOI lists, doi.org/arXiv URLs, and
CSV / XLSX / TSV tables are all accepted and normalized into the same shape.

The channel is a *prediction* that feeds the lane scheduler (run_lanes):
lanes decide where a paper starts, not where it is allowed to end up —
failures overflow to slower lanes exactly like the racing tiers do.
"""

from __future__ import annotations

import csv
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)
from typing import Any

from .identifiers import normalize_doi, normalize_arxiv_id

CHANNELS = ("oa", "elsevier", "grey", "institution", "auto")

TABLE_SUFFIXES = (".csv", ".xlsx", ".tsv", ".tab")

# DOI prefix -> fast HTTP lane. Extend as new publisher API fast paths land.
CHANNEL_BY_PREFIX = {
    "10.1016": "elsevier",  # Elsevier / ScienceDirect / Cell / Lancet
}

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.I)


@dataclass
class QueueEntry:
    identifier: str = ""
    channel: str = "auto"
    oa_url: str = ""
    title: str = ""
    raw: str = ""
    unresolved: bool = False


def predict_channel(identifier: str) -> str:
    """Zero-cost channel prediction from the DOI prefix."""
    m = re.match(r"^(10\.\d{4,9})/", identifier.strip())
    if m:
        return CHANNEL_BY_PREFIX.get(m.group(1).lower(), "auto")
    return "auto"


from .supplementary import fetch_supplementary

_PDF_URL_HINT = re.compile(r"\.pdf($|[?#])|/pdf/|article-pdf|/epdf/", re.IGNORECASE)
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _looks_like_pdf_url(url: str) -> bool:
    return bool(url) and bool(_PDF_URL_HINT.search(url))


def _fetch_oa_pdf(doi: str, config: dict[str, Any]) -> str:
    """OpenAlex lookup: best OA direct-PDF URL for a DOI ('' when none/error).

    Cached per DOI (including negative results) so repeated batches and the
    retry pass don't re-query. All failures are silent — enrichment is an
    optimization, never a hard dependency.
    """
    try:
        from .cache import cache_get, cache_set
        cached = cache_get(f"oa:{doi}", config) if config.get("cache_dir") else None
        if cached is not None:
            return str(cached.get("pdf_url") or "")
    except Exception:
        pass

    import requests

    proxy = config.get("network_proxy", "")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    params: dict[str, str] = {"select": "open_access,best_oa_location"}
    if config.get("email"):
        params["mailto"] = str(config["email"])
    if config.get("openalex_api_key"):
        params["api_key"] = str(config["openalex_api_key"])
    pdf = ""
    lookup_ok = False
    try:
        resp = requests.get(
            f"https://api.openalex.org/works/doi:{doi}",
            params=params, timeout=(15, 20), proxies=proxies,
            headers={"User-Agent": _user_agent()},
        )
        if resp.status_code == 200:
            lookup_ok = True
            work = resp.json()
            oa = work.get("open_access") or {}
            loc = work.get("best_oa_location") or {}
            candidate = str(loc.get("pdf_url") or "") or str(oa.get("oa_url") or "")
            if _looks_like_pdf_url(candidate):
                pdf = candidate
        elif resp.status_code not in _RETRYABLE_STATUS:
            lookup_ok = True  # definitive negative (e.g. 404): cacheable
    except Exception:
        pass

    if not lookup_ok:
        # OpenAlex failed (timeout / 429 / 5xx) — Unpaywall mirrors the same
        # OA data on a different host; a transient hiccup must not lose the
        # enrichment.
        try:
            unpay_params = dict(params)
            unpay_params.pop("api_key", None)
            resp2 = requests.get(
                f"https://api.unpaywall.org/v2/{doi}",
                params=unpay_params, timeout=(15, 20), proxies=proxies,
                headers={"User-Agent": _user_agent()},
            )
            if resp2.status_code == 200:
                lookup_ok = True
                loc = resp2.json().get("best_oa_location") or {}
                candidate = str(loc.get("url_for_pdf") or "")
                if _looks_like_pdf_url(candidate):
                    pdf = candidate
            elif resp2.status_code not in _RETRYABLE_STATUS:
                lookup_ok = True
        except Exception:
            pass

    # Cache only completed lookups: a timed-out proxy handshake must not
    # poison the negative cache for cache_ttl_hours.
    if lookup_ok:
        try:
            if config.get("cache_dir"):
                from .cache import cache_set
                cache_set(f"oa:{doi}", {"pdf_url": pdf}, config)
        except Exception:
            pass
    return pdf


def _enrich_oa_urls(entries: list[QueueEntry], config: dict[str, Any]) -> None:
    """Route gold/hybrid OA papers into the fast lane before scheduling.

    The prefix DB only knows Elsevier, so plain-DOI batches sent every other
    publisher to the grey or institutional lanes — OA journals (NAR, PLoS,
    Nat Commun, …) ended up at publisher bot-walls needing manual Turnstile
    clicks. One parallel OpenAlex lookup per unknown DOI fills ``oa_url``;
    the fast lane's own %PDF/10KB validation keeps junk from landing.
    Failures and the kill switch (``lane_oa_enrich: false``) leave entries
    exactly as they were.
    """
    if not config.get("lane_oa_enrich", True):
        return
    targets = [
        e for e in entries
        if e.identifier.lower().startswith("10.")
        and not e.oa_url
        and predict_channel(e.identifier) not in ("elsevier", "institution")
    ]
    if not targets:
        return

    def lookup(entry: QueueEntry) -> None:
        try:
            pdf = _fetch_oa_pdf(entry.identifier, config)
        except Exception:
            return  # enrichment must never break scheduling
        if pdf:
            entry.oa_url = pdf
            if entry.channel == "auto":
                entry.channel = "oa"

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lookup, targets))


def extract_identifier(text: str) -> str | None:
    """Extract a DOI or arXiv ID from a raw string, URL, or citation fragment."""
    raw = text.strip().rstrip(".,;)")
    if not raw:
        return None
    arxiv = normalize_arxiv_id(raw)
    if arxiv and ("arxiv" in raw.lower() or re.match(r"^\d{4}\.\d{4,5}(v\d+)?$", raw)):
        return arxiv
    doi_m = DOI_RE.search(raw)
    if doi_m:
        doi = _clean_doi(doi_m.group(1))
        if doi:
            return doi
    return None


def _clean_doi(doi: str) -> str | None:
    doi = normalize_doi(doi)
    doi = doi.rstrip(".,;:)")
    # Trailing author glue, e.g. "10.1002/ird.2673Hamed" -> "10.1002/ird.2673"
    doi = re.sub(r"[A-Z][a-z\u00C0-\u024F]{1,}$", "", doi)
    return doi if re.match(r"^10\.\d{4,9}/\S", doi) else None


def parse_queue(text: str) -> list[QueueEntry]:
    """Parse queue/DOI-list text into entries; unresolvable lines are kept."""
    entries: list[QueueEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("\t")]
        ident = extract_identifier(parts[0])
        if not ident:
            entries.append(QueueEntry(raw=line, unresolved=True))
            continue
        channel = parts[1].lower() if len(parts) > 1 and parts[1].lower() in CHANNELS else predict_channel(ident)
        oa_url = parts[2] if len(parts) > 2 and parts[2].lower().startswith("http") else ""
        entries.append(QueueEntry(identifier=ident, channel=channel, oa_url=oa_url, raw=line))
    return entries


def read_table(path: str | Path) -> list[dict[str, str]]:
    """Read a csv / tsv / xlsx table into rows of string dicts."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".csv", ".tsv", ".tab"):
        delim = "\t" if suffix in (".tsv", ".tab") else ","
        with open(p, newline="", encoding="utf-8-sig") as f:
            return [dict(r) for r in csv.DictReader(f, delimiter=delim)]
    if suffix == ".xlsx":
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("xlsx 支持需要 openpyxl：pip install openpyxl") from exc
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        try:
            header = [str(c or "").strip() for c in next(rows)]
        except StopIteration:
            return []
        out = []
        for r in rows:
            if all(c is None for c in r):
                continue
            out.append({h: ("" if c is None else str(c)) for h, c in zip(header, r)})
        return out
    raise ValueError(f"Unsupported table format: {suffix}")


def entries_from_table(rows: list[dict[str, str]]) -> list[QueueEntry]:
    """Map table rows to queue entries; the DOI column is sniffed if needed."""
    if not rows:
        return []
    cols = list(rows[0].keys())
    doi_col = next((c for c in cols if re.search(r"\bdoi\b", c, re.I)), None)
    if doi_col is None:
        for c in cols:
            if any(DOI_RE.search(str(v)) for v in (r.get(c, "") for r in rows[:20])):
                doi_col = c
                break
    title_col = next((c for c in cols if "title" in c.lower() or "标题" in c or "题名" in c), "")
    entries: list[QueueEntry] = []
    for r in rows:
        raw_val = str(r.get(doi_col, "")).strip() if doi_col else ""
        ident = extract_identifier(raw_val) if raw_val else None
        if not ident:
            entries.append(QueueEntry(raw=str(r)[:200], unresolved=True))
            continue
        entries.append(QueueEntry(
            identifier=ident,
            channel=predict_channel(ident),
            title=(str(r.get(title_col, "") or "") if title_col else ""),
        ))
    return entries


def load_job(path: str | Path) -> list[QueueEntry]:
    """Load any supported input file into queue entries.

    Accepts queue TSV, plain DOI lists, doi.org/arXiv URLs, APA lists,
    BibTeX, and csv / xlsx / tsv tables.
    """
    p = Path(path)
    if p.suffix.lower() in TABLE_SUFFIXES:
        return entries_from_table(read_table(p))

    text = _read_text(p)
    apa_like = bool(re.search(r"[A-Z][a-z\u00C0-\u024F]+,\s+[A-Z]\.", text)) and "doi.org" in text.lower()
    if p.suffix.lower() == ".bib" or apa_like:
        from .paperlist import parse_paper_list
        return _from_paper_entries(parse_paper_list(p))
    return parse_queue(text)


def _from_paper_entries(paper_entries) -> list[QueueEntry]:
    out: list[QueueEntry] = []
    for e in paper_entries:
        if e.doi:
            out.append(QueueEntry(identifier=e.doi, channel=predict_channel(e.doi), title=e.title, raw=e.raw))
        else:
            out.append(QueueEntry(raw=e.title or e.raw[:200], unresolved=True))
    return out


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return p.read_text(encoding="latin-1")


def write_queue(entries: list[QueueEntry], path: str | Path) -> Path:
    """Persist entries as a queue file; unresolved lines are kept as comments."""
    lines = ["# identifier\tchannel\toa_url"]
    for e in entries:
        if e.unresolved or not e.identifier:
            lines.append(f"# unresolved: {e.raw[:160]}")
            continue
        row = [e.identifier, e.channel or predict_channel(e.identifier)]
        if e.oa_url:
            row.append(e.oa_url)
        lines.append("\t".join(row))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Lane scheduler
# ---------------------------------------------------------------------------

def grey_allowed(config: dict[str, Any] | None) -> bool:
    """Whether grey-source (Sci-Hub/LibGen/SciBban) authorization survives lane
    scheduling. A user-explicit restriction is a veto: ``scihub_enabled=false``
    or ``download_strategy=legal_only`` can never be widened by ``--lanes`` or
    any other scheduling mode. Without an explicit restriction the product
    default (grey sources participate in fastest / scihub_* strategies) holds.
    """
    cfg = config or {}
    if not cfg.get("scihub_enabled", True):
        return False
    return cfg.get("download_strategy", "fastest") != "legal_only"


def run_lanes(
    entries: list[QueueEntry],
    output_dir: str | Path,
    config: dict[str, Any] | None = None,
    allow_grey: bool = True,
    allow_institution: bool = True,
    workers_fast: int = 8,
) -> list[dict[str, Any]]:
    """Channel-lane scheduler: fast HTTP first, grey racing and institutional
    cascade as slower lanes. Failures overflow to slower lanes.

    Result dicts reuse the racing shape: {success, doi, file, source, error}.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    config = config or {}

    _enrich_oa_urls(entries, config)

    from . import progress_reporter as _progress
    _progress.start_task("文献下载", total=len(entries))
    _progress.set_output_dir(str(out))

    fast: list[QueueEntry] = []
    grey: list[str] = []
    inst: list[str] = []
    for e in entries:
        if e.unresolved or not e.identifier:
            continue
        ch = e.channel or predict_channel(e.identifier)
        if ch == "institution":
            inst.append(e.identifier)
        elif ch in ("oa", "elsevier") or e.oa_url:
            fast.append(e)
        else:
            grey.append(e.identifier)

    results: list[dict[str, Any]] = []
    fast_failures: list[str] = []

    if fast:
        lane_results = _run_fast_lane(fast, out, config, workers=workers_fast, progress=_progress)
        results += lane_results
        fast_failures = [r["doi"] for r in lane_results if not r.get("success")]

    grey_ids = grey + fast_failures if allow_grey else list(grey)
    # Lane scheduling must not widen source authorization: if the user has
    # disabled grey sources, overflow simply does not get a grey retry.
    if grey_ids and allow_grey and grey_allowed(config):
        from .sources import batch_download
        _progress.update(phase="灰色源竞速")
        raw = batch_download(
            grey_ids, str(out), scihub_enabled=True,
            progress_callback=lambda done, total, current, result: (
                _progress.advance(
                    bool(result and result.get("success")),
                    current=str(current),
                )
            ),
        )
        results += _normalize_engine_results(raw)

    if inst and allow_institution:
        from .institutional.config_adapter import ConfigAdapter
        from .institutional.fetcher import PaperFetcher

        _progress.update(phase="机构级联")

        def _inst_fetch_chunk(chunk: list[str]) -> list[dict[str, Any]]:
            cfg = ConfigAdapter.load()
            cfg._config["output_dir"] = str(out)
            fetcher = PaperFetcher(cfg)
            rows: list[dict[str, Any]] = []
            try:
                for doi in chunk:
                    print(f"  [institution] {doi}")
                    try:
                        r = fetcher.fetch_with_result(doi).to_dict()
                        r.setdefault("doi", doi)
                    except Exception as exc:
                        r = {"doi": doi, "success": False, "error": str(exc)}
                    _progress.advance(bool(r.get("success")), current=doi)
                    rows.append(r)
            finally:
                fetcher.close()
            return rows

        workers_inst = max(1, min(int(config.get("institutional_workers", 1) or 1), 2))
        if workers_inst <= 1 or len(inst) < 2:
            results.extend(_inst_fetch_chunk(inst))
        else:
            chunks = [inst[i::workers_inst] for i in range(workers_inst)]
            with ThreadPoolExecutor(max_workers=workers_inst) as ex:
                for rows in ex.map(_inst_fetch_chunk, chunks):
                    results.extend(rows)

    _transient_retry(results, entries, out, config)
    _progress.finish()
    _fetch_si_for_results(results, out, config)
    return results


def _transient_retry(results: list[dict[str, Any]], entries: list[QueueEntry],
                     out: Path, config: dict[str, Any]) -> None:
    """瞬时失败冷却重试：限流/超时类失败等 60s 后按原车道重试一次。

    实测（2026-08-31，Elsevier 批量）：64 篇失败中 56 篇为瞬时限流
    （冷却后 HEAD 探测 100% 可得），高速重试仅收回 9 篇——**下载通道也
    需要节流**。本函数在车道全部跑完后执行一次带退避的重试。
    权限类失败（403/NOT_ENTITLED）不重试——那是真实边界。
    """
    if not config.get("fast_retry", True):
        return
    transient_marks = ("fast lane failed", "no PDF found", "timeout", "ERR_",
                       "API miss", "timed out")
    failed = [r for r in results
              if not r.get("success") and r.get("doi")
              and any(m in (str(r.get("error", "")) + str(r.get("reason", "")))
                      for m in transient_marks)]
    if not failed:
        return
    wait_sec = max(15, int(config.get("fast_retry_wait_sec", 60)))
    failed_dois = {r["doi"] for r in failed}
    retry_entries = [e for e in entries if e.identifier in failed_dois]
    if not retry_entries:
        return
    logger.info(f"  瞬时失败冷却重试: {len(retry_entries)} 篇，等待 {wait_sec}s 后原车道重试")
    try:
        from . import progress_reporter as _progress
        _progress.update(phase="冷却重试")
    except Exception:
        pass
    time.sleep(wait_sec)
    retry_results = _run_fast_lane(retry_entries, out, config, workers=4, progress=None)
    recovered = 0
    by_doi = {r["doi"]: r for r in retry_results if r.get("doi")}
    for i, r in enumerate(results):
        d = r.get("doi")
        if d in by_doi and by_doi[d].get("success") and not r.get("success"):
            results[i] = by_doi[d]
            recovered += 1
    logger.info(f"  冷却重试收回 {recovered}/{len(retry_entries)} 篇")


def _fetch_si_for_results(results: list[dict[str, Any]], out: Path, config: dict[str, Any]) -> None:
    """抓取成功论文的附件/补充材料（config: download_si，默认关）。

    存到主 PDF 旁边，命名为 {DOI}_SI{n}.{ext}，并写 si_manifest.json 便于核对。
    任何失败只减少附件数量，不影响主结果。
    """
    if not config.get("download_si"):
        return
    ok = [r for r in results if r.get("success") and r.get("doi")]
    if not ok:
        return
    try:
        from . import progress_reporter as _progress
        _progress.update(phase="抓取附件")
    except Exception:
        pass
    manifest: dict[str, list[str]] = {}
    manifest_lock = threading.Lock()

    def _si_one(row: dict[str, Any]) -> None:
        doi = row["doi"]
        try:
            files = fetch_supplementary(doi, out, config)
        except Exception:
            files = []
        if files:
            with manifest_lock:
                manifest[doi] = files
            print(f"  [SI] {doi}: {len(files)} 个附件")

    workers_si = max(1, min(4, len(ok)))
    if workers_si == 1:
        for r in ok:
            _si_one(r)
    else:
        with ThreadPoolExecutor(max_workers=workers_si) as ex:
            list(ex.map(_si_one, ok))
    if manifest:
        try:
            (out / "si_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass


def _run_fast_lane(
    entries: list[QueueEntry],
    out: Path,
    config: dict[str, Any],
    workers: int = 8,
    progress: Any = None,
) -> list[dict[str, Any]]:
    """HTTP fast lane: known OA URLs and the Elsevier API, in parallel."""
    import requests

    from concurrent.futures import as_completed
    from .sources.elsevier_api import fetch_pdf

    api_key = config.get("elsevier_api_key", "")
    inst_token = config.get("elsevier_insttoken", "")
    proxy = config.get("network_proxy", "")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": _user_agent()}

    def one(e: QueueEntry) -> dict[str, Any]:
        if e.oa_url:
            path = _download_url(e.oa_url, out, e.identifier, headers, proxies)
            if path:
                return {"success": True, "doi": e.identifier, "file": str(path), "source": "oa_url"}
        if api_key and (e.channel == "elsevier" or e.identifier.lower().startswith("10.1016/")):
            try:
                pdf = fetch_pdf(e.identifier, api_key, inst_token)
            except Exception:
                pdf = None
            if pdf and pdf[:4] == b"%PDF":
                path = out / _safe_name(e.identifier)
                path.write_bytes(pdf)
                return {"success": True, "doi": e.identifier, "file": str(path), "source": "elsevier_api"}
        return {"success": False, "doi": e.identifier, "error": "fast lane failed (no OA url / API miss)"}

    if progress is not None:
        progress.update(phase="快车道")
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {ex.submit(one, e): e for e in entries}
        for fut in as_completed(futures):
            entry = futures[fut]
            try:
                r = fut.result()
            except Exception as exc:
                r = {"success": False, "doi": entry.identifier, "error": str(exc)}
            if progress is not None:
                progress.advance(bool(r.get("success")), current=entry.identifier)
            results.append(r)
    return results


def _download_url(
    url: str,
    out: Path,
    identifier: str,
    headers: dict[str, str],
    proxies: dict[str, str] | None,
) -> Path | None:
    """Stream a URL to disk, accepting only real PDFs (magic bytes, >=10KB)."""
    import requests

    try:
        s = requests.Session()
        s.trust_env = False
        resp = s.get(url, headers=headers, proxies=proxies, timeout=30, stream=True, allow_redirects=True)
        if resp.status_code != 200:
            return None
        first = next(resp.iter_content(chunk_size=8192), b"")
        if not first.startswith(b"%PDF"):
            return None
        path = out / _safe_name(identifier)
        with open(path, "wb") as f:
            f.write(first)
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        if path.stat().st_size < 10_000:
            path.unlink(missing_ok=True)
            return None
        return path
    except Exception:
        return None


def _safe_name(identifier: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", identifier.strip()).strip("_")
    return f"{name}.pdf"


def _user_agent() -> str:
    try:
        from .network import USER_AGENT
        return USER_AGENT
    except Exception:  # pragma: no cover
        return "Mozilla/5.0 (compatible; scansci-pdf)"


def _normalize_engine_results(raw: Any) -> list[dict[str, Any]]:
    """Engine outputs come in two shapes: a result list, or a summary dict
    ({"entries"/"results": [...], "total", "succeeded", ...}). Keep dicts only.
    """
    if isinstance(raw, dict):
        for key in ("entries", "results"):
            v = raw.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def collect_failures(results: Any) -> list[str]:
    """Extract failed identifiers from any results shape (racing summary dict,
    cascade list, or plain entries)."""
    if isinstance(results, dict):
        for key in ("entries", "results"):
            v = results.get(key)
            if isinstance(v, list):
                return collect_failures(v)
        return [f for f in (results.get("failed_dois") or []) if isinstance(f, str)]
    if not isinstance(results, list):
        return []
    failed = []
    for r in results:
        if not isinstance(r, dict) or r.get("success") or r.get("status") == "success":
            continue
        ident = r.get("doi") or r.get("identifier") or ""
        if ident and ident not in failed:
            failed.append(ident)
    return failed
