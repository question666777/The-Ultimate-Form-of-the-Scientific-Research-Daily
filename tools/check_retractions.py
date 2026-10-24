#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_retractions.py — 撤稿 / 更正状态核查（Retraction Watch via Crossref + OpenAlex）

为什么需要它
------------
流水线目前只用 Crossref 校验「DOI 是否存在」「标题是否匹配」（见 lessons L014 / L027），
但**从不检查这篇论文是否已被撤稿**。约每 500 篇论文就有 1 篇最终被撤稿，
而报告是发给读者的，引用一篇撤稿论文是硬伤。
本脚本把「撤稿状态」补成一个确定性检查，独立于 LLM 判断。

数据来源（两路交叉，任一命中即判定）
------------------------------------
1. Crossref `/works/{doi}` 的 `update-to` 字段 —— 内含 Retraction Watch 数据库同步的记录
2. OpenAlex `/works/doi:{doi}` 的 `is_retracted` 标志

用法
----
    python check_retractions.py --file report_data.json
    python check_retractions.py --doi 10.1038/s41586-026-11030-4 [--doi ...]
    python check_retractions.py --file report_data.json --out retraction_report.json
    python check_retractions.py --html HTML/2026-09-22.html      # 从成品 HTML 里抠 DOI

退出码
------
    0  全部干净（或全部 unverifiable）
    1  发现撤稿 / 更正记录
    2  参数错误或文件读不到

无第三方依赖（标准库 urllib），符合本项目环境（无 requests）。
"""

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "frontier-bio-daily/1.0 (mailto:you@example.com)"
TIMEOUT = 25
RETRY = 3
SLEEP_BETWEEN = 0.8  # 礼貌间隔，避免 429

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")

# Crossref 里表示「有后续事件」的 update type
RETRACT_TYPES = {"retraction", "withdrawal"}
CORRECT_TYPES = {"correction", "erratum", "expression_of_concern", "concern"}


def _get_json(url):
    """带重试的 GET -> dict；失败返回 None 并附错误串。"""
    last_err = ""
    for attempt in range(RETRY):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8", "replace")), ""
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "404"
            last_err = "HTTP %s" % e.code
        except Exception as e:  # 网络/超时/解析
            last_err = type(e).__name__
        if attempt < RETRY - 1:
            time.sleep(2 * (attempt + 1))
    return None, last_err


def normalize_doi(doi):
    if not doi:
        return ""
    d = str(doi).strip().lower()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    d = re.sub(r"^doi:\s*", "", d)
    return d.strip().rstrip(".")


def _fmt_date(ent):
    """从 Crossref update 记录里取一个人类可读日期。"""
    for key in ("updated", "created", "published"):
        v = ent.get(key)
        if isinstance(v, dict):
            dp = v.get("date-parts") or []
            if dp and dp[0]:
                parts = [str(p).zfill(2) for p in dp[0][:3]]
                return "-".join(parts)
        elif isinstance(v, str) and v:
            return v[:10]
    return ""


def check_crossref(doi):
    """查 Crossref，看有没有 retraction / correction 类 update-to 记录。"""
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    data, err = _get_json(url)
    if data is None:
        return {"status": "unreachable" if err != "404" else "not_found", "source": "crossref", "events": [], "error": err}

    msg = data.get("message", {}) or {}
    events = []
    # Crossref 里：update-to = 本篇被谁更新；updated-by = 本篇更新了谁
    seen_keys = set()
    for key in ("update-to", "updated-by"):
        for ent in msg.get(key, []) or []:
            t = (ent.get("type") or "").lower().replace(" ", "_")
            if t not in RETRACT_TYPES and t not in CORRECT_TYPES:
                continue
            sig = (t, ent.get("DOI", ""), key)
            if sig in seen_keys:
                continue
            seen_keys.add(sig)
            events.append({
                "type": t,
                "doi": ent.get("DOI", ""),
                "date": _fmt_date(ent),
                "label": (ent.get("label") or "")[:200],
            })
    return {"status": "ok", "source": "crossref", "events": events, "error": "",
            "title": (msg.get("title") or [""])[0][:160]}


def check_openalex(doi):
    """查 OpenAlex 的 is_retracted 标志。"""
    url = "https://api.openalex.org/works/doi:" + urllib.parse.quote(doi, safe="")
    data, err = _get_json(url)
    if data is None:
        return {"status": "unreachable", "source": "openalex", "is_retracted": None, "error": err}
    return {
        "status": "ok",
        "source": "openalex",
        "is_retracted": bool(data.get("is_retracted")),
        "title": (data.get("title") or "")[:160],
        "error": "",
    }


def judge(cr, oa):
    """两路交叉判定。返回 (verdict, reasons)。"""
    reasons = []
    if oa.get("is_retracted") is True:
        reasons.append("OpenAlex 标记 is_retracted=true")

    retr_ev = [e for e in cr.get("events", []) if e["type"] in RETRACT_TYPES]
    corr_ev = [e for e in cr.get("events", []) if e["type"] in CORRECT_TYPES]
    if retr_ev:
        reasons.append("Crossref 记录撤稿/撤回：" + "; ".join(
            "%s%s" % (e["type"], (" @ " + str(e["date"]) if e["date"] else "")) for e in retr_ev))
    if corr_ev:
        reasons.append("Crossref 记录更正/关注声明：" + "; ".join(
            "%s%s" % (e["type"], (" @ " + str(e["date"]) if e["date"] else "")) for e in corr_ev))

    if reasons and retr_ev:
        return "retracted", reasons
    if reasons:
        return "corrected", reasons

    both_ok = cr.get("status") == "ok" and oa.get("status") == "ok"
    if both_ok:
        return "clean", ["Crossref 无撤稿记录，OpenAlex is_retracted=false"]
    if cr.get("status") == "not_found" and oa.get("status") == "unreachable":
        return "unverifiable", ["Crossref 404 且 OpenAlex 不可达"]
    return "unverifiable", ["状态未确定：crossref=%s, openalex=%s" % (cr.get("status"), oa.get("status"))]


def collect_dois_from_json(path):
    """从 report_data.json / scan_results.json 里递归抠 DOI。"""
    with io.open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    found = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and "doi" in k.lower():
                    d = normalize_doi(v)
                    if d.startswith("10."):
                        found.append(d)
                else:
                    walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)

    walk(data)
    seen, out = set(), []
    for d in found:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def collect_dois_from_html(path):
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    seen, out = set(), []
    for m in DOI_RE.finditer(text):
        d = normalize_doi(m.group(0))
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser(description="撤稿/更正状态核查")
    ap.add_argument("--file", help="含 DOI 的 JSON（report_data.json 等）")
    ap.add_argument("--html", help="成品 HTML，自动抠 DOI")
    ap.add_argument("--doi", action="append", default=[], help="直接指定 DOI，可重复")
    ap.add_argument("--out", help="把结果写成 JSON")
    ap.add_argument("--quiet", action="store_true", help="只输出异常项")
    args = ap.parse_args()

    dois = [normalize_doi(d) for d in args.doi]
    if args.file:
        if not os.path.isfile(args.file):
            print("[错误] 找不到文件：%s" % args.file)
            return 2
        dois += collect_dois_from_json(args.file)
    if args.html:
        if not os.path.isfile(args.html):
            print("[错误] 找不到文件：%s" % args.html)
            return 2
        dois += collect_dois_from_html(args.html)

    seen, uniq = set(), []
    for d in dois:
        if d and d.startswith("10.") and d not in seen:
            seen.add(d)
            uniq.append(d)

    if not uniq:
        print("[错误] 没有找到任何 DOI。用 --file / --html / --doi 指定来源。")
        return 2

    print("撤稿状态核查：%d 个 DOI" % len(uniq))
    print("-" * 62)

    results, bad = [], 0
    counts = {}
    for i, doi in enumerate(uniq, 1):
        cr = check_crossref(doi)
        oa = check_openalex(doi)
        verdict, reasons = judge(cr, oa)
        counts[verdict] = counts.get(verdict, 0) + 1
        rec = {"doi": doi, "verdict": verdict, "reasons": reasons,
               "crossref": cr, "openalex": oa}
        results.append(rec)

        if verdict in ("retracted", "corrected"):
            bad += 1
            tag = "撤稿" if verdict == "retracted" else "更正"
            print("[%s] %s" % (tag, doi))
            for r in reasons:
                print("        - %s" % r)
        elif not args.quiet:
            print("[%s] %s" % ({'clean': '干净', 'unverifiable': '无法判定'}.get(verdict, verdict), doi))
        if i < len(uniq):
            time.sleep(SLEEP_BETWEEN)

    print("-" * 62)
    print("汇总：" + "，".join("%s %d" % (k, v) for k, v in sorted(counts.items())))
    if bad:
        print("⚠ 有 %d 篇需要处理：撤稿条目应从报告中移除，更正条目需核对结论是否受影响。" % bad)
    else:
        print("✓ 未发现撤稿或更正记录。")

    if args.out:
        with io.open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "total": len(uniq), "counts": counts, "results": results},
                      fh, ensure_ascii=False, indent=2)
        print("结果已写入：%s" % args.out)

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
