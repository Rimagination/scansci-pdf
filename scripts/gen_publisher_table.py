"""Generate the publisher coverage table from the live StrategyRegistry.

The registry is the source of truth; this script keeps the README table from
drifting. Usage:

    python scripts/gen_publisher_table.py              # print markdown
    python scripts/gen_publisher_table.py --update README.md
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

START = "<!-- publisher-table:start -->"
END = "<!-- publisher-table:end -->"


def build_table() -> str:
    from scansci_pdf.publisher_strategies import StrategyRegistry

    lines = [
        "| 策略 | DOI 前缀 | 域名 |",
        "|---|---|---|",
    ]
    for s in sorted(StrategyRegistry.list_all(), key=lambda x: x.name.lower()):
        prefixes = s.doi_prefixes or ()
        shown = ", ".join(f"`{p}`" for p in prefixes[:2])
        if len(prefixes) > 2:
            shown += f" 等 {len(prefixes)} 个"
        domain = next(iter(s.base_domains or ()), "—")
        lines.append(f"| {s.name} | {shown or '—'} | `{domain}` |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", metavar="README", help="replace the marked block in this file")
    args = ap.parse_args()
    table = build_table()
    if not args.update:
        print(table)
        return
    p = Path(args.update)
    text = p.read_text(encoding="utf-8")
    if START in text and END in text:
        text = re.sub(re.escape(START) + r".*?" + re.escape(END), START + "\n" + table + "\n" + END, text, flags=re.S)
    else:
        text = text.rstrip() + f"\n\n{START}\n{table}\n{END}\n"
    p.write_text(text, encoding="utf-8", newline="\n")
    print(f"updated {p}")


if __name__ == "__main__":
    main()
