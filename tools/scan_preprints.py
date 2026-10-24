#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
scan_preprints.py — 预印本直连扫描（bioRxiv / medRxiv，免费无门槛，只用标准库）

数据源：bioRxiv API（https://api.biorxiv.org，免 key 免注册）。预印本比正式发表早 2-6 个月，
是最早的前沿信号；主扫描 rely 正式发表（Crossref），本脚本补预印本那一段前置时间窗。

与 scan_journals.py 的 --europepmc（SRC:PPR）互补：本脚本直连 bioRxiv/medRxiv 拿全文摘要
与学科分类（category），Europe PMC 拿 PubMed 收录的预印本，两条都可做预印本来源。

用法（在项目根）：
    python tools\\scan_preprints.py --days 7 --out tmp\\preprints.json
    python tools\\scan_preprints.py --days 7 --server medrxiv
"""

import argparse
import io
import json
import sys
import time
import urllib.parse
import urllib.request

UA = "frontier-bio-preprint/1.0 (mailto:you@example.com)"
TIMEOUT = 30
RETRY = 2


def _get_json(url):
    import urllib.error
    last = ""
    for i in range(RETRY):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace")), ""
        except urllib.error.HTTPError as e:
            if e.code in (404, 403):
                return None, "HTTP %s" % e.code
            last = "HTTP %s" % e.code
        except Exception as e:
            last = type(e).__name__
        if i < RETRY - 1:
            time.sleep(1.5 * (i + 1))
    return None, last


def fetch(server, date_from, date_to):
    if server not in ("biorxiv", "medrxiv"):
        raise ValueError("server 必须是 biorxiv 或 medrxiv")
    out = []
    cursor = 0
    while True:
        url = ("https://api.biorxiv.org/details/%s/%s/%s/%d"
               % (server, date_from, date_to, cursor))
        data, err = _get_json(url)
        if data is None:
            print("  [bioRxiv] 第 %d 页失败(%s)" % (cursor // 100 + 1, err))
            break
        coll = data.get("collection") or []
        if not coll:
            break
        for c in coll:
            out.append({
                "doi": c.get("doi"),
                "title": c.get("title"),
                "authors": c.get("authors"),
                "date": (c.get("date") or "")[:10],
                "category": c.get("category"),
                "abstract": str(c.get("abstract") or "")[:1200],
                "server": server,
                "version": c.get("version"),
            })
        total = data.get("total", 0)
        cursor += len(coll)
        if cursor >= total:
            break
        time.sleep(0.3)
    return out


def main():
    ap = argparse.ArgumentParser(description="预印本直连扫描（bioRxiv/medRxiv）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--server", choices=["biorxiv", "medrxiv", "both"], default="both")
    ap.add_argument("--out", default="tmp/preprints.json")
    args = ap.parse_args()

    import datetime
    today = datetime.date.today()
    date_from = (today - datetime.timedelta(days=args.days)).isoformat()
    date_to = today.isoformat()

    servers = ["biorxiv", "medrxiv"] if args.server == "both" else [args.server]
    all_items = []
    for s in servers:
        print("扫描 %s：%s ~ %s" % (s, date_from, date_to))
        items = fetch(s, date_from, date_to)
        print("  %d 篇" % len(items))
        all_items += items

    out = {
        "scanned_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "window": {"from": date_from, "to": date_to, "days": args.days},
        "count": len(all_items),
        "items": all_items,
    }
    import os
    out_dir = os.path.dirname(args.out)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("预印本共 %d 篇，已写入 %s" % (len(all_items), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())