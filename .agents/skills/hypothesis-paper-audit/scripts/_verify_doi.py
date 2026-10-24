# -*- coding: utf-8 -*-
"""D14 参考文献可解析性：读稿件文献表，逐条 Crossref 核验 DOI 是否真实可解析。

退出码：0 全部可解析 / 1 存在无法解析的条目。
只用标准库。用法：
    python _verify_doi.py [--en <en_md_path>]

默认读取 example/manuscript_EN.md。Illustrates the deterministic DOI-verification
step of the hypothesis-paper-audit pipeline.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EN = os.path.join(HERE, "..", "example", "manuscript_EN.md")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--en", default=DEFAULT_EN, help="英文稿 Markdown 路径")
    args = ap.parse_args()

    EN = os.path.normpath(args.en)
    text = open(EN, encoding="utf-8").read()
    refs_block = text.split("## References")[1].split("## Figure legend")[0]
    entries = re.findall(r"^(\d+)\.\s(.*)$", refs_block, re.M)

    ok = fail = 0
    for num, line in entries:
        m = re.search(r"DOI:\s*(10\.\S+)", line)
        if not m:
            print(f"NO DOI  [{num}] {line[:80]}")
            fail += 1
            continue
        doi = m.group(1).rstrip(".")
        url = "https://api.crossref.org/works/" + doi
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "paper-audit/1.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                msg = json.load(r)["message"]
            journal = (msg.get("container-title") or ["?"])[0]
            year = None
            for k in ("published-print", "published-online", "issued"):
                if k in msg and msg[k].get("date-parts"):
                    year = msg[k]["date-parts"][0][0]
                    break
            print(f"OK   [{num}] {doi}  | {journal} {year}")
            ok += 1
        except Exception as e:
            print(f"FAIL [{num}] {doi} -> {e}")
            fail += 1
        time.sleep(0.2)

    print(f"\n核验完成：OK {ok} / FAIL {fail} / 共 {len(entries)}")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()