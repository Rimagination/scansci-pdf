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
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

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
        lane_results = _run_fast_lane(fast, out, config, workers=workers_fast)
        results += lane_results
        fast_failures = [r["doi"] for r in lane_results if not r.get("success")]

    grey_ids = grey + fast_failures if allow_grey else list(grey)
    if grey_ids and allow_grey:
        from .sources import batch_download
        raw = batch_download(grey_ids, str(out), scihub_enabled=True)
        results += _normalize_engine_results(raw)

    if inst and allow_institution:
        from .institutional.config_adapter import ConfigAdapter
        from .institutional.fetcher import PaperFetcher

        cfg = ConfigAdapter.load()
        cfg._config["output_dir"] = str(out)
        fetcher = PaperFetcher(cfg)
        try:
            for i, doi in enumerate(inst, 1):
                print(f"  [institution {i}/{len(inst)}] {doi}")
                try:
                    r = fetcher.fetch_with_result(doi).to_dict()
                    r.setdefault("doi", doi)
                except Exception as exc:
                    r = {"doi": doi, "success": False, "error": str(exc)}
                results.append(r)
        finally:
            fetcher.close()

    return results


def _run_fast_lane(
    entries: list[QueueEntry],
    out: Path,
    config: dict[str, Any],
    workers: int = 8,
) -> list[dict[str, Any]]:
    """HTTP fast lane: known OA URLs and the Elsevier API, in parallel."""
    import requests

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

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(one, entries))


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
