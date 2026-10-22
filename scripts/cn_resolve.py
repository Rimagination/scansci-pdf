#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Chinese identifier normalizer/parser for literature lists.

Handles:
- full-width -> half-width normalization (titles, DOIs, punctuation)
- Chinese DOI families: 10.3969/j.issn.1000-1298.2020.01.001 (Wanfang),
  plus generic 10.xxxx DOIs mixed into Chinese text
- extraction of ISSN + year + issue + article-number components
- CJK tokenization helpers shared by verify_pdf.py

Usage:
    python cn_resolve.py --text "杨某. 光伏板对荒漠土壤水热影响[J]. 太阳能学报, 2026. DOI: 10.19912/j.0254-0096.2026.01.001"
    python cn_resolve.py --doi 10.3969/j.issn.1000-1298.2020.01.001
"""
import argparse
import json
import re
import unicodedata

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s，。；;\"'<>）)]+", re.I)
CN_DOI_RE = re.compile(
    r"^10\.3969/j\.issn\.(?P<issn>\d{4}-\d{3}[\dX])\.(?P<year>\d{4})\.(?P<issue>\d{1,3})\.(?P<article>\d{1,4})$",
    re.I)


def normalize_text(s):
    """全角->半角、去零宽字符、压缩空白。"""
    s = unicodedata.normalize("NFKC", str(s))
    s = s.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    s = re.sub(r"[ \t\u3000]+", " ", s)
    return s.strip()


def extract_dois(text):
    out = []
    for m in DOI_RE.finditer(normalize_text(text)):
        doi = m.group(0).rstrip(".,;)")
        if doi not in out:
            out.append(doi)
    return out


def parse_cn_doi(doi):
    """解析中文 DOI 族，返回结构化字段；非中文 DOI 返回 {'family': 'generic'}. """
    d = normalize_text(doi)
    m = CN_DOI_RE.match(d)
    if m:
        return {"family": "wanfang_issn", **{k: v for k, v in m.groupdict().items()},
                "issn": m.group("issn").upper()}
    if d.lower().startswith("10.3969/"):
        return {"family": "wanfang_other"}
    return {"family": "generic"}


def cjk_bigrams(text):
    """CJK 字符二元组（含单字），用于中文标题相似度。"""
    chars = [c for c in normalize_text(text) if "\u4e00" <= c <= "\u9fff"]
    grams = set(chars)
    grams |= {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}
    return grams


def strip_cn_title(title):
    """去掉中文标题里的 [J]/[D]/[M] 等文献类型标识与作者首段。"""
    t = normalize_text(title)
    t = re.sub(r"\[[JDMCRSP]\]", "", t)
    return t.strip(" .。;；")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", help="任意含 DOI/中文标题的文本")
    ap.add_argument("--doi", help="单个 DOI")
    a = ap.parse_args()
    out = {}
    if a.doi:
        out["dois"] = [normalize_text(a.doi)]
    elif a.text:
        out["dois"] = extract_dois(a.text)
        out["normalized_text"] = normalize_text(a.text)
    out["parsed"] = [parse_cn_doi(d) for d in out.get("dois", [])]
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
