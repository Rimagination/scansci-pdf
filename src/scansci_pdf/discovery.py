"""Discovery layer: ScanSci Find CLI facade.

Brings ScanSci Find's six capability areas into scansci-pdf by delegating to
the ``scansci-find`` CLI at runtime (no code copy):

- 13-source federated search with domain routing (OpenAlex/Crossref/S2/arXiv/
  PubMed/EuropePMC/DBLP/... plus research-data sources)
- Protocol workflow: plan / estimate / smoke / calibrate
- Citation chasing: expand_citations (Semantic Scholar + OpenCitations)
- Identifier verification: verify (DOI/PMID/arXiv)
- OA resolution: resolve-oa (Unpaywall)
- Audit artifacts: coverage / PRISMA / download_queue.json

The facade degrades gracefully: every call reports a clear error when the
``scansci-find`` CLI is missing, and ``search`` callers fall back to the
built-in three-source search.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CLI_NAME = "scansci-find"
DEFAULT_TIMEOUT = 180  # seconds; citation chasing / systematic searches are slow


class DiscoveryUnavailableError(RuntimeError):
    """Raised when the scansci-find CLI is missing or broken."""


def _cli_path() -> str:
    path = shutil.which(CLI_NAME)
    if not path:
        raise DiscoveryUnavailableError(
            f"'{CLI_NAME}' not found on PATH. Install ScanSci Find: "
            "pip install scansci-find (https://github.com/Rimagination/scansci-find). "
            "Alternatively use the local fallbacks: search / verify / resolve-oa / build-queue."
        )
    return path


def find_cli_available() -> bool:
    """Check whether the scansci-find CLI is installed and responds."""
    path = shutil.which(CLI_NAME)
    if not path:
        return False
    try:
        result = subprocess.run(
            [path, "sources"],
            capture_output=True, text=True, timeout=30,
            env=_build_env(),
        )
        return result.returncode == 0 and "catalog" in (result.stdout or "")
    except Exception:
        return False


def _build_env() -> dict[str, str]:
    """Merge scansci-pdf config credentials into the subprocess environment.

    ScanSci Find reads OPENALEX_API_KEY / UNPAYWALL_EMAIL / SEMANTIC_SCHOLAR_API_KEY
    from the environment; pass through what scansci-pdf already configures.
    """
    env = dict(os.environ)
    try:
        from .config import load_config
        cfg = load_config()
        openalex_key = str(cfg.get("openalex_api_key") or "").strip()
        if openalex_key and not env.get("OPENALEX_API_KEY"):
            env["OPENALEX_API_KEY"] = openalex_key
        email = str(cfg.get("email") or "").strip()
        if email and "@" in email and not env.get("UNPAYWALL_EMAIL"):
            env["UNPAYWALL_EMAIL"] = email
    except Exception:
        pass
    return env


def _run(args: list[str], *, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run scansci-find and parse its stdout JSON.

    Raises DiscoveryUnavailableError when the CLI is missing, and RuntimeError
    with the CLI's stderr when it fails.
    """
    cli = _cli_path()
    try:
        proc = subprocess.run(
            [cli, *args],
            capture_output=True, text=True, timeout=timeout,
            env=_build_env(),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"scansci-find {' '.join(args[:2])} timed out after {timeout}s") from None
    except OSError as exc:
        raise DiscoveryUnavailableError(f"Failed to run scansci-find: {exc}") from None

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise RuntimeError(
            f"scansci-find {' '.join(args[:2])} failed (exit {proc.returncode}): {detail}"
        )
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        # Not JSON — some commands print plain text (e.g. warnings); wrap it.
        return {"stdout": proc.stdout, "stderr": proc.stderr}


# ---------------------------------------------------------------------------
# Protocol workflow proxies
# ---------------------------------------------------------------------------

def plan(query: str, *, domain: str = "general", depth: str = "standard",
         question: str = "", year_from: int | None = None, year_to: int | None = None) -> dict[str, Any]:
    """Build an auditable search protocol without running a search."""
    args = ["plan", query, "--domain", domain, "--depth", depth]
    if question:
        args += ["--question", question]
    if year_from is not None:
        args += ["--year-from", str(year_from)]
    if year_to is not None:
        args += ["--year-to", str(year_to)]
    return _run(args)


def estimate(query: str, *, domain: str = "general", depth: str = "standard",
             question: str = "", year_from: int | None = None, year_to: int | None = None) -> dict[str, Any]:
    """Estimate result volume before spending a full search budget."""
    args = ["estimate", query, "--domain", domain, "--depth", depth]
    if question:
        args += ["--question", question]
    if year_from is not None:
        args += ["--year-from", str(year_from)]
    if year_to is not None:
        args += ["--year-to", str(year_to)]
    return _run(args)


def smoke(query: str, *, domain: str = "general",
          records_per_source: int = 4) -> dict[str, Any]:
    """Fetch a few records per source and validate the candidate contract."""
    return _run(["smoke", query, "--domain", domain,
                 "--records-per-source", str(records_per_source)])


def calibrate(query: str, *, domain: str = "general", depth: str = "standard",
              sample_size: int = 100) -> dict[str, Any]:
    """Run a bounded calibration sample before a high-recall search."""
    return _run(["calibrate", query, "--domain", domain, "--depth", depth,
                 "--sample-size", str(sample_size)])


# ---------------------------------------------------------------------------
# Search / citation chasing
# ---------------------------------------------------------------------------

def search(query: str, out_dir: str | Path, *, domain: str = "general",
           depth: str = "quick", limit: int = 20, year_from: int | None = None,
           year_to: int | None = None, sources: list[str] | None = None,
           expand_citations: bool = False, citation_source: str = "semantic",
           citation_rounds: int | None = None, verify_identifiers: bool = False,
           resolve_oa: bool = False, find_preprints: bool = False,
           code_links: bool = False, sort: str = "") -> dict[str, Any]:
    """Run a ScanSci Find search into ``out_dir`` and return its artifacts.

    Returns {"out": str, "total": int, "candidates": [...], "download_queue": [...]}.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    args = ["search", query, "--out", str(out), "--domain", domain,
            "--depth", depth, "--limit", str(limit)]
    if year_from is not None:
        args += ["--year-from", str(year_from)]
    if year_to is not None:
        args += ["--year-to", str(year_to)]
    if sources:
        args += ["--source", ",".join(sources)]
    if expand_citations:
        args += ["--expand-citations", "--citation-source", citation_source]
        if citation_rounds is not None:
            args += ["--citation-rounds", str(citation_rounds)]
    if verify_identifiers:
        args += ["--verify-identifiers"]
    if resolve_oa:
        args += ["--resolve-oa"]
    if find_preprints:
        args += ["--find-preprints"]
    if code_links:
        args += ["--code-links"]
    if sort:
        args += ["--sort", sort]
    summary = _run(args, timeout=600 if expand_citations else DEFAULT_TIMEOUT)

    payload: dict[str, Any] = {
        "out": str(out),
        "total": int(summary.get("total", 0)),
        "candidates": _read_json(out / "candidates.json", default=[]),
        "download_queue": _read_json(out / "download_queue.json", default=[]),
    }
    for key in ("coverage_report.json", "prisma.json", "source_report.json"):
        payload[key.replace(".json", "")] = _read_json(out / key, default={})
    return payload


def expand_citations(query: str, out_dir: str | Path, *, rounds: int = 1,
                     citation_source: str = "semantic", depth: str = "standard",
                     limit: int = 20, domain: str = "general") -> dict[str, Any]:
    """Search plus backward/forward citation chasing rounds."""
    return search(
        query, out_dir, domain=domain, depth=depth, limit=limit,
        expand_citations=True, citation_source=citation_source,
        citation_rounds=rounds,
    )


# ---------------------------------------------------------------------------
# Verification / OA resolution
# ---------------------------------------------------------------------------

def verify(candidates: list[dict[str, Any]] | str, *,
           limit: int | None = None) -> dict[str, Any]:
    """Verify DOI/PMID/arXiv identifiers against authoritative APIs."""
    return _identify_pipeline("verify", candidates, limit=limit)


def resolve_oa(candidates: list[dict[str, Any]] | str, *,
               limit: int | None = None) -> dict[str, Any]:
    """Resolve DOI open-access locations through Unpaywall."""
    return _identify_pipeline("resolve-oa", candidates, limit=limit)


def _identify_pipeline(command: str, candidates: list[dict[str, Any]] | str,
                       *, limit: int | None = None) -> dict[str, Any]:
    """Run verify/resolve-oa over a candidate list (path or inline JSON)."""
    temp_created = False
    if isinstance(candidates, str) and Path(candidates).exists():
        input_path = Path(candidates)
    else:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            if isinstance(candidates, str):
                tmp.write(candidates)
            else:
                json.dump(candidates, tmp, ensure_ascii=False)
            input_path = Path(tmp.name)
        temp_created = True
    args = [command, str(input_path)]
    if limit is not None:
        args += ["--limit", str(limit)]
    try:
        return _run(args)
    finally:
        if temp_created:
            try:
                input_path.unlink(missing_ok=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Queue building / legacy field adaptation
# ---------------------------------------------------------------------------

def build_download_queue(out_dir: str | Path) -> list[str]:
    """Extract download identifiers from a find output dir's download_queue.json.

    Returns a list of DOI/arXiv identifiers ready for ``scansci-pdf batch``.
    """
    out = Path(out_dir)
    queue = _read_json(out / "download_queue.json", default=[])
    identifiers: list[str] = []
    for entry in queue:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("identifier") or ""
        if identifier:
            identifiers.append(identifier)
        else:
            for key in ("doi", "arxiv_id"):
                value = entry.get(key)
                if value:
                    identifiers.append(str(value))
                    break
    return identifiers


def build_preprint_fallbacks(out_dir: str | Path) -> dict[str, list[str]]:
    """Map queue DOIs to their matched preprint arXiv IDs for fallback retries.

    Reads ``preprint_identifiers`` from each download_queue entry so a batch
    that fails on a paywalled DOI can retry the open preprint version.
    """
    out = Path(out_dir)
    queue = _read_json(out / "download_queue.json", default=[])
    fallbacks: dict[str, list[str]] = {}
    for entry in queue:
        if not isinstance(entry, dict):
            continue
        identifier = str(entry.get("identifier") or entry.get("doi") or "").strip()
        preprint_values = []
        for item in entry.get("preprint_identifiers") or []:
            value = str(item.get("value") or "").strip()
            if value:
                preprint_values.append(value)
        if identifier and preprint_values:
            fallbacks[identifier] = preprint_values
    return fallbacks


def to_legacy_results(candidates: list[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    """Adapt ScanSci Find candidates to the built-in search result fields.

    Keeps the existing return contract (doi/title/authors/year/cited_by_count/
    is_oa/oa_url/source) so downstream callers (MCP, scansci_html) are unchanged.
    """
    results: list[dict[str, Any]] = []
    for candidate in candidates[:limit]:
        if not isinstance(candidate, dict):
            continue
        source_hits = candidate.get("source_hits") or []
        links = candidate.get("links") or {}
        access = candidate.get("access") or {}
        oa_url = links.get("pdf") or links.get("fulltext") or candidate.get("oa_url") or ""
        results.append({
            "doi": candidate.get("doi") or None,
            "arxiv_id": candidate.get("arxiv_id") or None,
            "title": candidate.get("title") or "",
            "authors": candidate.get("authors") or [],
            "year": candidate.get("year"),
            "cited_by_count": len(source_hits) if source_hits else 0,
            "is_oa": bool(oa_url or access.get("oa_status")),
            "oa_url": oa_url or None,
            "source": "discovery",
            "abstract": candidate.get("abstract") or "",
            "journal": candidate.get("journal") or "",
        })
    return results


def _read_json(path: Path, *, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
