from __future__ import annotations

from contextlib import redirect_stdout
import io
import json

from scansci_pdf import main, search


def test_search_json_is_ascii_safe_and_contains_author_match(monkeypatch):
    monkeypatch.setattr(
        search,
        "search_papers",
        lambda *args, **kwargs: [
            {
                "title": "Soil ı study",
                "doi": "10.1234/example",
                "authors": ["Pınar Reich"],
                "_author_match": {
                    "name": "Pınar Reich",
                    "id": "A123",
                    "works_count": 10,
                    "cited_by_count": 20,
                },
            }
        ],
    )
    raw = io.BytesIO()
    ascii_stdout = io.TextIOWrapper(raw, encoding="ascii", errors="strict")

    with redirect_stdout(ascii_stdout):
        main.search_cmd(
            query="",
            limit=10,
            year_from=None,
            year_to=None,
            sort="cited_by_count",
            json_output=True,
            author="Pınar Reich",
            author_id="",
        )
    ascii_stdout.flush()
    output = raw.getvalue().decode("ascii")
    payload = json.loads(output)

    assert payload["results"][0]["title"] == "Soil ı study"
    assert payload["author_match"]["name"] == "Pınar Reich"
