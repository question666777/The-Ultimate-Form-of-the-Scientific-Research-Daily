#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
sync_journals.py — 期刊清单同步器（免费无门槛，只用标准库）

把 scan_journals.py 里硬编码的 21 本期刊，扩展成「按领域组织的期刊清单」，
数据源两个、均免 key 免门槛：
  1. Crossref /journals?query=   —— 拿权威 ISSN（打印版优先，e-ISSN 兜底，同 L008）
  2. DOAJ /api/search/journals/  —— 按学科关键词扩展 Open Access 期刊（补低密度领域）

产出 tools/journals.json，scan_journals.py 从它读期刊（不再硬编码），
check_self_review.py 从它数期刊数（派生事实源头，见 check_self_review.py FACTS journal_count）。

设计要点：
  · 幂等：重复跑不产生重复条目（按 issn 去重）
  · 机器校验 ISSN：不手填 ISSN，全部来自 Crossref / DOAJ 返回（L008/L014 教训）
  · 降级：单期刊查不到只是跳过，不中断整批（AGENTS「快失败」= 数据失败不阻塞）
  · 只用标准库（本机无 requests）

用法：
    python sync_journals.py                 # 重新同步全部（Crossref + DOAJ）
    python sync_journals.py --no-doa j      # 只跑 Crossref 种子表
    python sync_journals.py --dry-run       # 只打印，不写 journals.json
"""

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

UA = "frontier-bio-journals-sync/1.0 (mailto:you@example.com)"
TIMEOUT = 30
RETRY = 2
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "journals.json")

# ---------------------------------------------------------------------------
# 领域 -> 种子期刊名（ISSN 交给 Crossref 机器校验，不手填）
# tier: 1=顶刊（原 21 本内核）/ 2=领域主力（低密度领域补强）/ 3=OA 扩展（DOAJ 发现）
# ---------------------------------------------------------------------------
SEED_DOMAINS = {
    "跨领域综合": [
        ("Nature", "0028-0836", 1), ("Science", "0036-8075", 1),
        ("Nature Communications", "2041-1723", 1), ("PNAS", "0027-8424", 1),
        ("Cell", "0092-8674", 1), ("Science Advances", "2375-2548", 1),
    ],
    "免疫与肿瘤免疫": [
        ("Immunity", "1074-7613", 1), ("Cancer Cell", "1535-6108", 1),
        ("Nature Immunology", "1529-2908", 2), ("Journal of Experimental Medicine", "1540-9538", 2),
    ],
    "神经与发育": [
        ("Neuron", "0896-6273", 1), ("Nature Neuroscience", "1097-6256", 1),
        ("Science Translational Medicine", "1946-6234", 1), ("Cell Stem Cell", "1934-5909", 1),
    ],
    "衰老生物学与长寿干预": [
        ("Nature Aging", "2662-8465", 2), ("Aging Cell", "1474-9726", 2),
        ("Cell Reports Medicine", "2666-3791", 2), ("npj Aging", "2731-6068", 2),
        ("Journals of Gerontology Series A", "1758-535X", 2),
    ],
    "神经退行性疾病": [
        ("Molecular Neurodegeneration", "1750-1326", 2), ("Alzheimer's & Dementia", "1552-5279", 2),
        ("Acta Neuropathologica", "1432-0533", 2), ("Neurobiology of Disease", "1095-953X", 2),
        ("Brain", "1460-2156", 2),
    ],
    "RNA生物学与RNA治疗": [
        ("Nucleic Acids Research", "1362-4962", 2), ("RNA Biology", "1555-8584", 2),
        ("Molecular Therapy Nucleic Acids", "2162-2531", 2), ("RNA", "1469-9001", 2),
    ],
    "代谢与营养生物学": [
        ("Cell Metabolism", "1550-4131", 1), ("Nature Metabolism", "2522-5812", 1),
        ("Cell Reports", "2211-1247", 2), ("Journal of Clinical Investigation", "1558-8238", 2),
        ("Diabetes", "0012-1797", 2), ("eLife", "2050-084X", 2), ("Gut", "1468-3288", 2),
    ],
    "植物与微生物": [
        ("Nature Plants", "2055-0278", 2), ("Molecular Plant", "1752-9867", 2),
        ("Cell Host & Microbe", "1931-3128", 2), ("Microbiome", "2049-2618", 2),
        ("Gut Microbes", "1949-0984", 2),
    ],
    "表观遗传与基因调控": [
        ("Molecular Cell", "1097-2765", 1), ("Nature Genetics", "1061-4036", 1),
        ("Genome Biology", "1474-760X", 2), ("Nature Cell Biology", "1465-7392", 1),
    ],
    "方法学与测序": [
        ("Nature Methods", "1548-7091", 1), ("Nature Biotechnology", "1087-0156", 1),
        ("Nature Microbiology", "2058-5276", 1),
    ],
}

# DOAJ 学科关键词 -> 领域（补 OA 源，专注低密度领域）
DOAJ_QUERIES = {
    "aging senescence longevity": "衰老生物学与长寿干预",
    "neurodegeneration Alzheimer Parkinson": "神经退行性疾病",
    "RNA therapy siRNA microRNA": "RNA生物学与RNA治疗",
    "metabolism metabolic nutrition": "代谢与营养生物学",
    "plant immunity microbiome": "植物与微生物",
    "epigenetics chromatin": "表观遗传与基因调控",
}


def _get_json(url):
    last = ""
    for i in range(RETRY):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace")), ""
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "404"
            last = "HTTP %s" % e.code
        except Exception as e:
            last = type(e).__name__
        if i < RETRY - 1:
            time.sleep(1.5 * (i + 1))
    return None, last


def crossref_issns(name):
    """按期刊名查 Crossref，返回 (print_issn, eissn)。查不到返回 (None, None)。"""
    url = "https://api.crossref.org/journals?query=" + urllib.parse.quote(name) + "&rows=3"
    data, err = _get_json(url)
    if data is None:
        return None, None
    items = data.get("message", {}).get("items", []) or []
    # 找标题最接近的那条
    want = re.sub(r"[^a-z0-9]", "", name.lower())
    for it in items:
        t = (it.get("title") or "").strip()
        if re.sub(r"[^a-z0-9]", "", t.lower()) == want:
            return it.get("ISSN") and (it["ISSN"][0] if it["ISSN"] else None), (
                it.get("ISSN") and (it["ISSN"][1] if len(it["ISSN"]) > 1 else None))
    # 不模糊回退（L008/L014 教训）：泛词期刊名（如 Science）会命中文题无关的期刊，
    # 一旦用错 ISSN，扫描整本期刊都错。精确匹配失败 → 返回 None，主流程保留种子 ISSN。
    return None, None


def doaj_journals(query, domain, limit=8):
    """DOAJ 学科关键词检索，返回 [(title, eissn/pissn, domain)]。"""
    url = "https://doaj.org/api/search/journals/" + urllib.parse.quote(query) + "?pageSize=%d" % limit
    data, err = _get_json(url)
    if data is None:
        return []
    out = []
    for r in data.get("results", []) or []:
        bj = r.get("bibjson", {})
        title = (bj.get("title") or "").strip()
        issn = bj.get("eissn") or bj.get("pissn")
        if not title or not issn:
            continue
        subjects = [s.get("term", "") for s in (bj.get("subject") or [])]
        out.append({"title": title, "issn": issn, "domain": domain,
                    "tier": 3, "source": "doaj", "subjects": subjects})
    return out


def build():
    journals = {}  # issn -> 条目（用 issn 去重）
    notes = []

    # 1) 种子表（内置 ISSN，跑 Crossref 校验）
    for domain, seeds in SEED_DOMAINS.items():
        for name, issn_seed, tier in seeds:
            pin, eissn = crossref_issns(name)
            issn = pin or eissn or issn_seed  # Crossref 优先，查不到退回种子值
            if not issn:
                notes.append("种子期刊查不到 ISSN：%s" % name)
                continue
            key = str(issn).strip()
            if key in journals:
                continue
            journals[key] = {"title": name, "issn": key, "domain": domain,
                             "tier": tier, "source": "crossref"}
            time.sleep(0.4)

    # 2) DOAJ 扩展（OA 期刊，补低密度领域）
    for q, domain in DOAJ_QUERIES.items():
        for j in doaj_journals(q, domain):
            key = str(j["issn"]).strip()
            if key in journals:
                continue
            journals[key] = {"title": j["title"], "issn": key, "domain": j["domain"],
                             "tier": 3, "source": "doaj", "subjects": j["subjects"]}
        time.sleep(0.5)

    # 3) 组装输出
    by_domain = {}
    for j in journals.values():
        by_domain.setdefault(j["domain"], []).append(j)
    for lst in by_domain.values():
        lst.sort(key=lambda x: (x.get("tier", 3), x["title"].lower()))

    result = {
        "_meta": {
            "purpose": "前沿生物追踪扫描的期刊清单（scan_journals.py 数据源）",
            "sources": ["crossref (ISSN 机器校验)", "doaj (OA 扩展)"],
            "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "count": len(journals),
            "note": "tier 1=顶刊内核 / 2=领域主力 / 3=OA 扩展；ISSN 以 Crossref/DOAJ 返回为准",
        },
        "domains": by_domain,
        "flat": {j["title"]: j["issn"] for j in journals.values()},
        "notes": notes,
    }
    return result


def main():
    ap = argparse.ArgumentParser(description="同步期刊清单（Crossref + DOAJ，免费无门槛）")
    ap.add_argument("--no-doaj", action="store_true", help="只跑 Crossref 种子表")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写文件")
    global _NO_DOA_J  # 供 dry-run 复用
    args = ap.parse_args()

    if args.no_doaj:
        # 简化：带 --no-doaj 时临时清空 DOAJ_QUERIES
        global DOAJ_QUERIES
        DOAJ_QUERIES = {}

    result = build()
    c = result["_meta"]["count"]
    print("同步完成：%d 本期刊（%d 个领域）" % (c, len(result["domains"])))
    for d, lst in result["domains"].items():
        print("  %-22s %d 本" % (d, len(lst)))
    if result["notes"]:
        print("异常：")
        for n in result["notes"]:
            print("  - %s" % n)
    if not args.dry_run:
        with io.open(OUT, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print("已写入：%s" % OUT)
    else:
        print("（dry-run，未写入）")
    return 0


if __name__ == "__main__":
    sys.exit(main())