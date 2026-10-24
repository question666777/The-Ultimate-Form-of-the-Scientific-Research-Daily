#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
scan_journals.py — 广角扫描（第 1 步固化）
把原先每天现写的扫描逻辑固化成脚本，并把 lessons.md 里 9 条 API 坑**写进代码**，
让它们从 active 变成 fixed（不再依赖"下次记得看经验库"）。

固化清单（每条对应一条 lesson）
--------------------------------
L007  Crossref cursor 深分页时传 sort=published 必 400 → 本脚本 cursor 模式**不传 sort**
L008  Cell Press 期刊用 e-ISSN 返回 404 → 本脚本用印刷版 ISSN（e-ISSN 例外见 JOURNALS 表）
L011  Crossref 混入大量新闻/评论 → 三层过滤：多人作者 / d41586 前缀 / 需有 abstract
L014  首发日期禁止从 DOI 字符串反推 → 本脚本日期一律取 Crossref published（见 _fmt_date）
L017  批量查 24+ 期刊触发 429 → 每刊间隔 --sleep（默认 1.0s），大刊排最后
L019  Europe PMC 默认 pageSize=1000 截断 → 本脚本用 cursorMark 翻页拉满
L023  Europe PMC resultType=core 返回 0 篇 → 本脚本优先 lite，core 作兜底
L027  Crossref 返回 DOI 与标题错配 → 保存前用 /works/{doi} 反查标题比对，标记 mismatch
L028  filter 带时间后缀 + type 组合必 400 → 只传日期，type 过滤放 Python 端

用法
----
    python scan_journals.py --days 7 --out tmp/scan_results.json
    python scan_journals.py --days 7 --journals Nature,Science --out tmp/scan_small.json
    python scan_journals.py --days 7 --no-verify-doi --out tmp/scan_fast.json
    python scan_journals.py --days 7 --europepmc --out tmp/scan_epmc.json

输出 JSON 结构
--------------
{
  "scanned_at": "...", "window": {"from": "...", "to": "..."},
  "journals": {"Nature": 12, ...},
  "counts": {"raw": 507, "kept": 431, "pmc": 0, "verified": 431, "mismatch": 0},
  "items": [ {"doi","title","journal","date","authors","abstract","type","source","doi_status"} ]
}

退出码：0 成功 / 2 参数或致命错误（单刊失败只降级不退出，符合 AGENTS「快失败」）
只用标准库（本机无 requests）。
"""

import argparse
import datetime
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
TIMEOUT = 30
RETRY = 3

# ---------------------------------------------------------------------------
# 期刊清单：名称 -> ISSN
# 数据源：tools/journals.json（由 sync_journals.py 维护，ISSN 经 Crossref/DOAJ 机器校验）
# L008：Cell Press 系用印刷版 ISSN；Cell Metabolism 的 e-ISSN 是例外（可用）
# 大刊（条目多）放列表末尾，配合 L017
# ---------------------------------------------------------------------------
def _load_journals():
    """从 journals.json 读 name->issn 映射（原 21 本硬编码内核已并入该文件）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journals.json")
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)["flat"]


JOURNALS = _load_journals()

# 大刊放最后（L017：小刊先查，响应快；大刊后查，减少限流叠加）
BIG_JOURNALS = {"Nature", "Science", "Nature Communications", "PNAS"}

# L011：Crossref 里的新闻/评论识别
PRESS_DOI_PREFIX = "10.1038/d41586"      # Nature 新闻稿专用前缀
STRONG_RESEARCH_WORDS = (
    "we ", "here we", "we report", "we show", "our results", "we found",
    "本研究", "我们发现", "we demonstrate", "this study",
)


def _get_json(url, accept="application/json"):
    """带重试的 GET -> (dict|None, err)。404 立即返回不重试。"""
    last = ""
    for i in range(RETRY):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
            return json.loads(raw.decode("utf-8", "replace")), ""
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "404"
            last = "HTTP %s" % e.code
        except Exception as e:
            last = type(e).__name__
        if i < RETRY - 1:
            time.sleep(2 * (i + 1))
    return None, last


def _norm_doi(d):
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(d or "").strip().lower()).rstrip(".")


def _clean_text(s):
    """Crossref 标题/摘要常带 JATS 标签与多余空白。"""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", str(s))
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", s).strip()


def _date_from_crossref(msg):
    """L014：日期一律以 Crossref published 为准，禁止从 DOI 字符串反推。"""
    for key in ("published", "published-print", "published-online", "issued", "created"):
        v = msg.get(key)
        if isinstance(v, dict):
            dp = v.get("date-parts") or []
            if dp and dp[0] and dp[0][0]:
                parts = list(dp[0]) + [1, 1]
                try:
                    return datetime.date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
                except (ValueError, TypeError):
                    continue
    return ""


def _looks_like_news(msg, title):
    """L011 三层过滤：返回 (是否剔除, 原因)。"""
    authors = msg.get("author") or []
    # (1) 单人作者 + 无摘要 → 多为评论/讣告
    if len(authors) <= 1 and not msg.get("abstract"):
        return True, "single-author-no-abstract"
    # (2) d41586 前缀是 Nature 新闻版，除非含强研究信号词
    doi = _norm_doi(msg.get("DOI"))
    if doi.startswith(PRESS_DOI_PREFIX):
        low = (title or "").lower()
        if not any(w in low for w in STRONG_RESEARCH_WORDS):
            return True, "press-doi"
    # (3) 无摘要且标题像新闻标题（含 ":" 极少、很短）
    if not msg.get("abstract") and len(title or "") < 40:
        return True, "no-abstract-short"
    return False, ""


def _looks_research_type(msg):
    """L028：type 过滤放 Python 端（filter 里带 type 会 400）。"""
    t = (msg.get("type") or "").lower()
    return t in ("journal-article", "posted-content")


def fetch_journal(name, issn, date_from, date_to, use_cursor=True):
    """拉单个期刊的窗口内条目。返回 (items, note)。"""
    items, note = [], ""
    base = "https://api.crossref.org/journals/%s/works" % issn
    # L028：filter 只放日期，不带时间后缀、不带 type
    flt = "from-pub-date:%s,until-pub-date:%s" % (date_from, date_to)
    cursor = "*"
    page = 0
    while True:
        params = {
            "filter": flt,
            "rows": "200",
            "select": "DOI,title,abstract,author,type,published,published-print,"
                      "published-online,issued,created,container-title",
        }
        if use_cursor:
            params["cursor"] = cursor
            # L007：cursor 模式下**绝不**传 sort/order，否则 400
        else:
            params["offset"] = str(page * 200)

        url = base + "?" + urllib.parse.urlencode(params)
        data, err = _get_json(url)
        if data is None:
            note = "第 %d 页失败(%s)" % (page + 1, err)
            break

        msg = data.get("message", {})
        batch = msg.get("items", []) or []
        for it in batch:
            if not _looks_research_type(it):
                continue
            title = _clean_text((it.get("title") or [""])[0])
            if not title:
                continue
            drop, why = _looks_like_news(it, title)
            if drop:
                continue
            doi = _norm_doi(it.get("DOI"))
            if not doi.startswith("10."):
                continue
            authors = it.get("author") or []
            first = ""
            if authors:
                a = authors[0]
                first = (a.get("family") or a.get("name") or "").strip()
            items.append({
                "doi": doi,
                "title": title,
                "journal": name,
                "date": _date_from_crossref(it),
                "authors": first,
                "n_authors": len(authors),
                "abstract": _clean_text(it.get("abstract"))[:1200],
                "type": (it.get("type") or "").lower(),
                "source": "crossref",
                "doi_status": "unverified",
            })

        total = msg.get("total-results", 0)
        next_cursor = msg.get("next-cursor")
        page += 1
        if not batch:
            break
        if use_cursor:
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        else:
            if page * 200 >= total:
                break
        if page >= 15:  # 安全阀
            note = "达到页数上限 15"
            break
        time.sleep(0.3)
    return items, note


def fetch_europepmc(query, date_from, date_to, page_size=1000):
    """L019/L023：用 cursorMark 翻页拉满；优先 lite 模式（core 曾返回 0 篇）。"""
    out, note = [], ""
    base = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    cursor = "*"
    for result_type in ("lite", "core"):  # L023：lite 优先，core 兜底
        out, cursor, got_any = [], "*", False
        for _ in range(10):
            params = {
                "query": "%s AND (FIRST_PDATE:[%s TO %s])" % (query, date_from, date_to),
                "format": "json",
                "resultType": result_type,
                "pageSize": str(page_size),
                "cursorMark": cursor,
            }
            url = base + "?" + urllib.parse.urlencode(params)
            data, err = _get_json(url)
            if data is None:
                note = "EuropePMC %s 模式失败(%s)" % (result_type, err)
                break
            res = data.get("resultList", {}).get("result", []) or []
            if not res and not got_any:
                note = "EuropePMC %s 模式返回 0 篇" % result_type
                break
            got_any = True
            for r in res:
                doi = _norm_doi(r.get("doi"))
                if not doi.startswith("10."):
                    continue
                out.append({
                    "doi": doi,
                    "title": _clean_text(r.get("title")),
                    "journal": _clean_text(r.get("journalTitle") or r.get("bookOrReportDetails") or "EuropePMC"),
                    "date": (r.get("firstPublicationDate") or "")[:10],
                    "authors": _clean_text(r.get("authorString") or "")[:80],
                    "n_authors": (r.get("authorString") or "").count(",") + 1,
                    "abstract": _clean_text(r.get("abstractText"))[:1200],
                    "type": "preprint" if r.get("source") in ("PPR",) else "journal-article",
                    "source": "europepmc",
                    "doi_status": "unverified",
                })
            nc = data.get("nextCursorMark")
            if not nc or nc == cursor:
                break
            cursor = nc
            time.sleep(0.5)
        if out:
            note = "EuropePMC 用 %s 模式取得 %d 篇" % (result_type, len(out))
            break
    return out, note


def _title_key(s):
    """标题归一化：只留小写字母数字。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def verify_doi_title(doi, expected_title, probe_len=40):
    """L027：用 /works/{doi} 反查标题并比对。

    判定用「前缀包含」而非严格相等——scan 侧标题常是 Crossref 完整标题的截断
    （期刊页副标题被砍掉），严格比较会误判为 mismatch（false negative）。
    只有当两边前 probe_len 字符**互不构成前缀**时才判错配。
    """
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    data, err = _get_json(url)
    if data is None:
        return "unreachable" if err != "404" else "not_found", ""
    msg = data.get("message", {})
    real = _clean_text((msg.get("title") or [""])[0])
    a = _title_key(expected_title)[:probe_len]
    b = _title_key(real)[:probe_len]
    if len(a) < 12 or len(b) < 12:
        return "unverifiable", real
    # 一方是另一方的前缀 → 视为同一篇（截断/补全）
    if a.startswith(b) or b.startswith(a):
        return "ok", real
    # 否则看前 20 字符的共同前缀比例，容忍标点/连字符差异
    n = 20
    common = 0
    for x, y in zip(a[:n], b[:n]):
        if x != y:
            break
        common += 1
    if common >= 15:
        return "ok", real
    return "mismatch", real


def main():
    ap = argparse.ArgumentParser(description="广角扫描（第 1 步）")
    ap.add_argument("--days", type=int, default=7, help="检索窗口天数（默认 7）")
    ap.add_argument("--journals", help="逗号分隔的期刊名，留空=全部内置")
    ap.add_argument("--out", default=os.path.join("tmp", "scan_results.json"))
    ap.add_argument("--sleep", type=float, default=1.0, help="期刊间间隔秒数（L017）")
    ap.add_argument("--no-verify-doi", action="store_true", help="跳过 DOI-标题一致性反查（L027）")
    ap.add_argument("--europepmc", action="store_true", help="额外拉 Europe PMC 预印本")
    ap.add_argument("--epmc-query", default="(SRC:PPR)", help="Europe PMC 查询串")
    args = ap.parse_args()

    today = datetime.date.today()
    date_from = (today - datetime.timedelta(days=args.days)).isoformat()
    date_to = today.isoformat()

    if args.journals:
        wanted = [j.strip() for j in args.journals.split(",") if j.strip()]
        unknown = [j for j in wanted if j not in JOURNALS]
        if unknown:
            print("[错误] 未知期刊：%s" % unknown)
            print("可用：%s" % ", ".join(sorted(JOURNALS)))
            return 2
        targets = {j: JOURNALS[j] for j in wanted}
    else:
        targets = dict(JOURNALS)

    # L017：小刊先查，大刊后查（减少限流叠加）
    ordered = [j for j in targets if j not in BIG_JOURNALS] + [j for j in targets if j in BIG_JOURNALS]

    print("扫描窗口：%s ~ %s（%d 天）" % (date_from, date_to, args.days))
    print("目标期刊：%d 个（小刊优先，大刊最后）" % len(ordered))
    print("-" * 62)

    all_items, per_journal, notes = [], {}, []
    for i, name in enumerate(ordered, 1):
        items, note = fetch_journal(name, targets[name], date_from, date_to)
        per_journal[name] = len(items)
        all_items += items
        flag = "✓" if items else "·"
        print(" %s [%2d/%2d] %-32s %3d 篇%s" % (
            flag, i, len(ordered), name, len(items), ("  ← " + note) if note else ""))
        if note:
            notes.append("%s: %s" % (name, note))
        if i < len(ordered):
            time.sleep(args.sleep)  # L017

    pmc_count = 0
    if args.europepmc:
        pmc_items, note = fetch_europepmc(args.epmc_query, date_from, date_to)
        pmc_count = len(pmc_items)
        all_items += pmc_items
        print(" ✓ [EuropePMC] %-32s %3d 篇%s" % ("", pmc_count, ("  ← " + note) if note else ""))
        if note:
            notes.append("EuropePMC: %s" % note)

    raw = len(all_items)

    # 去重（同一 DOI 可能在多源出现）
    seen, uniq = set(), []
    for it in all_items:
        if it["doi"] in seen:
            continue
        seen.add(it["doi"])
        uniq.append(it)

    # L027：DOI-标题一致性反查
    mismatch = verified = 0
    if not args.no_verify_doi and uniq:
        print("-" * 62)
        print("DOI-标题一致性反查（L027）：%d 条，可能较慢…" % len(uniq))
        kept = []
        for i, it in enumerate(uniq, 1):
            status, real_title = verify_doi_title(it["doi"], it["title"])
            it["doi_status"] = status
            if status == "mismatch":
                mismatch += 1
                it["crossref_title"] = real_title
                print("   ✗ 错配 %s" % it["doi"])
                print("       scan: %s" % it["title"][:70])
                print("       real: %s" % real_title[:70])
                continue  # 错配条目直接丢弃
            if status == "ok":
                verified += 1
            kept.append(it)
            if i % 50 == 0:
                print("   …已核对 %d/%d" % (i, len(uniq)))
            time.sleep(0.12)
        uniq = kept
    else:
        for it in uniq:
            it["doi_status"] = "skipped"

    # 窗口复查（双保险，防 API 忽略 filter）
    in_window = []
    for it in uniq:
        d = it.get("date") or ""
        if not d:
            in_window.append(it)  # 无日期保留，交下一步判断
            continue
        try:
            dd = datetime.date.fromisoformat(d[:10])
        except ValueError:
            in_window.append(it)
            continue
        if datetime.timedelta(0) <= (today - dd) <= datetime.timedelta(days=args.days):
            in_window.append(it)

    out_data = {
        "scanned_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "window": {"from": date_from, "to": date_to, "days": args.days},
        "journals": per_journal,
        "notes": notes,
        "counts": {
            "raw": raw, "deduped": len(uniq), "kept": len(in_window),
            "pmc": pmc_count, "verified": verified, "mismatch": mismatch,
        },
        "items": in_window,
    }

    out_dir = os.path.dirname(args.out)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out_data, fh, ensure_ascii=False, indent=2)

    c = out_data["counts"]
    print("-" * 62)
    print("原始 %d 篇 → 去重 %d → 窗口内 %d 篇" % (c["raw"], c["deduped"], c["kept"]))
    print("DOI 校验：通过 %d，错配丢弃 %d" % (c["verified"], c["mismatch"]))
    if notes:
        print("异常记录：")
        for n in notes:
            print("  - %s" % n)
    print("结果已写入：%s" % args.out)
    print("（只统计计数，原始 JSON 不要整段读入上下文 —— L016 漏斗式分层读取）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
