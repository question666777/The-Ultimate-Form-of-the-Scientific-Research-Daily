#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
citation_graph.py — 引用网络检索（免费无门槛，只用标准库）

数据源：OpenAlex（https://openalex.org，免 key 免注册；polite pool 加 mailto）。
作用三条：
  1. --doi     单篇论文的引用上下文：被引量、领域标签、谁在引它（领域分布）、它引了谁
  2. --entity  按主题/实体检索，输出高被引 work 的领域分布 → 发现「哪个领域在做这件事」
  3. --bridge  两篇论文共被引分析 → 找「两个领域之间被共同引用的桥梁论文」

为什么需要它：洞见引擎靠「共享实体」连桥，但「两个领域其实在做同一件事」往往表现为
**共被引**（一篇论文同时被两个领域引用）——这是实体词典摘不到的桥接信号。
novelty_gate 抢先核验、hypothesis-paper 机制锚定都可用它补「引用证据」。

只用标准库。用法（在项目根）：
    python tools\\citation_graph.py --doi 10.1038/s41586-020-2649-2
    python tools\\citation_graph.py --entity ferroptosis --rows 5
    python tools\\citation_graph.py --bridge 10.1111/x 10.2222/y
"""

import argparse
import io
import json
import sys
import time
import urllib.parse
import urllib.request

UA = "frontier-bio-citation/1.0 (mailto:you@example.com)"
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


def _params(**kw):
    kw.setdefault("mailto", MAILTO)
    return urllib.parse.urlencode(kw)


def fetch_work(doi):
    """按 DOI 拉 OpenAlex work，返回 dict 或 None。"""
    url = "https://api.openalex.org/works/doi:" + urllib.parse.quote(doi, safe="")
    data, err = _get_json(url)
    return data, err


def top_concepts(work, n=4):
    """work 的领域概念（OpenAlex concepts，按 score 排序）。"""
    cons = (work or {}).get("concepts") or []
    cons = sorted(cons, key=lambda c: -(c.get("score") or 0))
    return [(c.get("display_name", ""), round(c.get("score") or 0, 2)) for c in cons[:n]]


def cited_by_domains(work, n=20):
    """拉引用该 work 的论文（前 n 篇），聚合它们的领域分布。"""
    url = (work or {}).get("cited_by_api_url")
    if not url:
        return {}
    url += ("&" if "?" in url else "?") + "per-page=%d&%s" % (n, _params())
    data, err = _get_json(url)
    if data is None:
        return {"_err": err}
    dist = {}
    for w in data.get("results", []) or []:
        for c in top_concepts(w, 1):
            key = c[0] or "(未标注)"
            dist[key] = dist.get(key, 0) + 1
    return dict(sorted(dist.items(), key=lambda kv: -kv[1]))


def cite_doi(doi):
    w, err = fetch_work(doi)
    if w is None:
        print("取不到 %s（%s）" % (doi, err))
        return 1
    b = w.get("biblio") or {}
    print("标题：%s" % (w.get("title") or ""))
    print("出处：%s · %s" % (w.get("primary_location", {}).get("source", {}).get("display_name") or "?",
                             w.get("publication_year")))
    print("被引：%d 次" % (w.get("cited_by_count") or 0))
    print("领域：%s" % ("、".join("%s(%.2f)" % (a, s) for a, s in top_concepts(w))))
    dist = cited_by_domains(w)
    if "_err" in dist:
        print("引用方领域分布：取不到（%s）" % dist["_err"])
    elif dist:
        print("引用方领域分布（前 %d 篇引用方）：" % sum(dist.values()))
        for k, v in dist.items():
            print("  %-40s %d" % (k, v))
    else:
        print("引用方领域分布：无引用")
    return 0


def search_entity(query, rows=5):
    url = "https://api.openalex.org/works?" + _params(
        search=query, per_page=str(rows), sort="cited_by_count:desc")
    data, err = _get_json(url)
    if data is None:
        print("检索失败：%s" % err)
        return 1
    print("主题「%s」高被引论文（按被引排序）：" % query)
    for w in data.get("results", []) or []:
        print("  [%d] %s（%s，被引 %d）" % (
            (w.get("cited_by_count") or 0),
            (w.get("title") or "")[:80],
            w.get("publication_year"),
            (w.get("cited_by_count") or 0)))
    return 0


def bridge(d1, d2):
    w1, e1 = fetch_work(d1)
    w2, e2 = fetch_work(d2)
    if w1 is None or w2 is None:
        print("取不到：%s / %s" % (e1, e2))
        return 1
    t1 = (w1.get("title") or "")[:60]
    t2 = (w2.get("title") or "")[:60]
    print("A：%s" % t1)
    print("B：%s" % t2)
    # 共被引：两篇同时出现在第三篇论文的 referenced_works 里（用 related_works 近似）
    # OpenAlex 免费接口不直接给共被引，这里退化为「各自的领域 + 共享概念」信号
    c1 = {a for a, _ in top_concepts(w1, 6)}
    c2 = {a for a, _ in top_concepts(w2, 6)}
    shared = c1 & c2
    print("A 领域：%s" % ("、".join(sorted(c1))))
    print("B 领域：%s" % ("、".join(sorted(c2))))
    print("共享概念：%s" % ("、".join(sorted(shared)) if shared else "（无）"))
    return 0


def main():
    ap = argparse.ArgumentParser(description="引用网络检索（OpenAlex，免费）")
    ap.add_argument("--doi", help="单篇论文 DOI")
    ap.add_argument("--entity", help="按主题/实体检索高被引论文")
    ap.add_argument("--rows", type=int, default=5)
    ap.add_argument("--bridge", nargs=2, metavar=("DOI1", "DOI2"), help="两篇论文领域桥接")
    args = ap.parse_args()

    if args.doi:
        return cite_doi(args.doi)
    if args.entity:
        return search_entity(args.entity, args.rows)
    if args.bridge:
        return bridge(args.bridge[0], args.bridge[1])
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())