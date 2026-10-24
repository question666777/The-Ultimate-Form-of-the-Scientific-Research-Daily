#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
backfill_search.py — 历史机制锚定检索（免费无门槛，只用标准库）

数据源：OpenAlex（免 key）。作用：突破主扫描 7 天窗口、深挖 30 天窗口的限制，
按「实体/机制 + 年份范围」回溯该机制的历史锚定文献——机制的原发现、关键里程碑、
被后续反复引用的奠基论文。hypothesis-paper 的机制锚定（讲清某通路的前世今生）靠它。

为什么需要它：论文线需要的「机制锚定」要查 3-5 年前的原始发现文献。主扫描是 30 天
移动窗口，历史深度为零；没有历史锚定，机制叙述只能靠摘要推断，撑不起严谨的论文背景。

用法（在项目根）：
    python tools\\backfill_search.py --entity ferroptosis --from 2018 --to 2021
    python tools\\backfill_search.py --entity "TDP-43 phase separation" --from 2014 --to 2018 --top 8
"""

import argparse
import sys
import time
import urllib.parse
import urllib.request

UA = "frontier-bio-backfill/1.0 (mailto:you@example.com)"
TIMEOUT = 30
RETRY = 2
MAILTO = "you@example.com"


def _get_json(url):
    import json
    import urllib.error
    last = ""
    for i in range(RETRY):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
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


def search(entity, y_from, y_to, top):
    filter_s = "publication_year:%d-%d" % (y_from, y_to)
    url = ("https://api.openalex.org/works?"
           + urllib.parse.urlencode({
               "search": entity,
               "filter": filter_s,
               "per-page": str(top),
               "sort": "cited_by_count:desc",
               "mailto": MAILTO,
           }))
    data, err = _get_json(url)
    if data is None:
        print("检索失败：%s" % err)
        return 1
    res = data.get("results", []) or []
    print("「%s」%d-%d 年高被引论文（共 %d 条命中，取前 %d）：" % (
        entity, y_from, y_to, data.get("meta", {}).get("count", 0), len(res)))
    for w in res:
        src = (w.get("primary_location") or {}).get("source") or {}
        doi = (w.get("doi") or "").replace("https://doi.org/", "")
        print("  [被引 %5d] %s（%s，%s）" % (
            (w.get("cited_by_count") or 0),
            (w.get("title") or "")[:90],
            w.get("publication_year"),
            src.get("display_name") or "?"))
        if doi:
            print("           doi:%s" % doi)
    return 0


def main():
    ap = argparse.ArgumentParser(description="历史机制锚定检索（OpenAlex，免费）")
    ap.add_argument("--entity", required=True, help="机制/实体关键词")
    ap.add_argument("--from", dest="y_from", type=int, required=True, help="起始年份")
    ap.add_argument("--to", dest="y_to", type=int, required=True, help="结束年份")
    ap.add_argument("--top", type=int, default=10, help="返回条数（默认 10）")
    args = ap.parse_args()
    if args.y_from > args.y_to:
        print("错误：起始年份大于结束年份")
        return 2
    return search(args.entity, args.y_from, args.y_to, args.top)


if __name__ == "__main__":
    sys.exit(main())