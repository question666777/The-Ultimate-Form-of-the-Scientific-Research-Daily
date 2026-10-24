#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fulltext_probe.py — 开放获取全文探测（免费无门槛，只用标准库）

数据源两条、均免 key：
  1. OpenAlex work 的 open_access 字段 —— 是否 OA、OA 状态（gold/green/hybrid/bronze）、全文 URL
  2. Europe PMC fullText 查询 —— 能否拿到全文 XML（供 liteparse/markitdown 抽取机制细节）

为什么需要它：主扫描只抓标题+摘要，机制细节（通路上下游、实验条件、统计量）在全文里。
深挖步骤先跑本脚本，标记「哪些论文能免费拿到全文」，再决定要不要抽全文——
避免对每篇都做昂贵的全文抽取（L016 漏斗式分层）。

用法：
    python tools\\fulltext_probe.py --doi 10.xxxx
    python tools\\fulltext_probe.py --file tmp\\scan_results.json   # 批量探测，标 OA 状态
"""

import argparse
import io
import json
import sys
import time
import urllib.parse
import urllib.request

UA = "frontier-bio-fulltext/1.0 (mailto:you@example.com)"
TIMEOUT = 30
RETRY = 2
MAILTO = "you@example.com"


def _get_json(url):
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


def openalex_oa(doi):
    url = "https://api.openalex.org/works/doi:" + urllib.parse.quote(doi, safe="")
    data, err = _get_json(url)
    if data is None:
        return None
    return data.get("open_access") or {}


def europepmc_fulltext(doi):
    """Europe PMC 全文探测：返回能否拿全文 XML + 摘要/全文标志。"""
    q = 'DOI:"%s"' % doi
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
           + urllib.parse.urlencode({"query": q, "resultType": "core", "format": "json"}))
    data, err = _get_json(url)
    if data is None:
        return {}
    res = (data.get("resultList", {}).get("result") or [])
    if not res:
        return {}
    r = res[0]
    return {
        "has_fulltext": bool(r.get("hasTextMinedTerms") or r.get("fullTextUrlList")),
        "is_openaccess": r.get("isOpenAccess"),
        "in_epmc": r.get("inEPMC"),
        "fulltext_urls": [u.get("url") for u in (r.get("fullTextUrlList", {}).get("fullTextUrl") or [])],
    }


def probe_doi(doi):
    print("DOI：%s" % doi)
    oa = openalex_oa(doi)
    if oa is None:
        print("  OpenAlex 无记录")
    else:
        print("  OA 状态：%s（%s）" % ("是" if oa.get("is_oa") else "否", oa.get("oa_status") or "closed"))
        if oa.get("oa_url"):
            print("  全文 URL：%s" % oa["oa_url"])
        print("  仓库有全文：%s" % ("是" if oa.get("any_repository_has_fulltext") else "否"))
    ft = europepmc_fulltext(doi)
    if ft:
        print("  Europe PMC 全文：%s（OA=%s）" % ("有" if ft.get("has_fulltext") else "无", ft.get("is_openaccess")))
        for u in ft.get("fulltext_urls", []):
            print("    - %s" % u)
    elif ft == {}:
        print("  Europe PMC 无记录")
    return 0


def probe_file(path):
    with io.open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items", [])
    print("批量探测 %d 篇论文的 OA 状态…" % len(items))
    oa_n = fulltext_n = 0
    for it in items:
        doi = it.get("doi")
        if not doi:
            continue
        oa = openalex_oa(doi)
        is_oa = bool(oa and oa.get("is_oa"))
        if is_oa:
            oa_n += 1
        time.sleep(0.3)
    print("结果：%d/%d 篇可 OA 获取全文" % (oa_n, len(items)))
    return 0


def main():
    ap = argparse.ArgumentParser(description="开放获取全文探测（OpenAlex + Europe PMC）")
    ap.add_argument("--doi", help="单篇 DOI")
    ap.add_argument("--file", help="扫描结果 JSON（批量探测）")
    args = ap.parse_args()
    if args.doi:
        return probe_doi(args.doi)
    if args.file:
        return probe_file(args.file)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())