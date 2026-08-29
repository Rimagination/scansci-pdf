"""Generate the MCP tool table from the live FastMCP app.

The server is the source of truth; this keeps the README table from drifting.

    python scripts/gen_mcp_tool_table.py --update README.md
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

START = "<!-- mcp-tools:start -->"
END = "<!-- mcp-tools:end -->"


def build_table() -> str:
    from scansci_pdf.server import mcp_app

    tools = asyncio.run(mcp_app.list_tools())
    lines = [f"<details>\n<summary><strong>MCP 工具全表</strong>（{len(tools)} 个）</summary>\n",
             "| 工具 | 用途 |",
             "|---|---|"]
    for t in sorted(tools, key=lambda x: x.name):
        desc = (t.description or "").strip().split("\n")[0].replace("|", "/")
        lines.append(f"| `{t.name}` | {desc} |")
    lines.append("\n</details>")
    return "\n".join(lines)


def update(path: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    block = START + "\n" + build_table() + "\n" + END
    if START in text and END in text:
        text = re.sub(re.escape(START) + r".*?" + re.escape(END), block, text, flags=re.S)
    else:
        text = text.rstrip() + f"\n\n{block}\n"
    p.write_text(text, encoding="utf-8", newline="\n")
    print(f"updated {p}")


if __name__ == "__main__":
    targets = [a for a in sys.argv[1:] if not a.startswith("--")]
    update(targets[0] if targets else "README.md")
