#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_report_consistency.py — 报告自洽性确定性校验（对应审计第 5、6 维）

为什么需要它
------------
审计第 5 维（报告完整）和第 6 维（口径一致）目前靠 LLM 目测，容易漏。
本脚本把能机械判定的部分**变成确定性检查**：同一文献的日期/期刊/DOI
在卡片、图注、参考文献之间是否一致；必填版块是否齐备；篇数统计是否
前后自洽。LLM 只在脚本报错时才需要介入判断。

借自：ICH M10 / 生物分析 ISR（incurred sample reanalysis）的「同一样本
多次测量必须一致」思路 —— 同一文献在报告多处出现 = 同一样本多次测量。

用法
----
    python check_report_consistency.py --html HTML/2026-09-22.html
    python check_report_consistency.py --html HTML/2026-09-22.html --json out.json

退出码
------
    0  全部通过
    1  有告警（不致命，建议人工看一眼）
    2  有失败（必须修）或参数错误
无第三方依赖。
"""

import argparse
import io
import json
import os
import re
import sys

try:
    from html import unescape
except ImportError:  # pragma: no cover
    unescape = lambda s: s

# 必填版块（pipeline red line：报告面向非专业读者的固定结构）
# 每项为候选模式的「或」关系——同一版块的历史叫法可能不同，命中任一即算存在。
REQUIRED_BLOCKS = [
    ("摘要卡/核心结论", [r"一句话核心结论", r"核心结论"]),
    ("目录导航", [r"目录导航", r"目录"]),
    ("本章看点", [r"本章看点"]),
    ("一句话看懂", [r"一句话看懂"]),
    ("深入解读", [r"深入解读", r"专业摘要", r"专业解读", r"深度解读"]),
    ("生活启示", [r"生活启示"]),
    ("参考文献", [r"参考文献"]),
]

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")

# 篇数断言：只认「显式总量」句式（2026-10-21 修订，L048）
# 事故：highlights 里「3 篇 Science 背靠背拆解…」描述的是 8 篇中的 3 篇，
# 旧正则「解读\s*(\d+)\s*篇」借「深入解读」标签把它读成全报告总篇数，制造假失败。
# 现仅接受带总量标记的写法；散落的「N 篇 XX」修辞不参与断言。
TOTAL_COUNT_PATTERNS = (
    r"本期解读\s*(\d+)\s*篇",
    r"本期\s*共?\s*(\d+)\s*篇",
    r"(?:共|合计|全篇共)\s*(\d+)\s*篇",
)


def strip_html(raw):
    t = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.S | re.I)
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t]+", " ", unescape(t))


def norm_doi(d):
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(d).strip().lower()).rstrip(".")


def detect_declared_count(text):
    """返回报告显式声明的总篇数；无显式总量句式则返回 None。

    只认 TOTAL_COUNT_PATTERNS 里的总量标记写法，避免把「3 篇 Science」
    这类部分篇数修辞误读成总篇数（2026-10-21 假失败事故，L048）。
    """
    for pat in TOTAL_COUNT_PATTERNS:
        m = re.search(pat, text)
        if m:
            return int(m.group(1))
    return None


def selftest():
    """篇数断言回归用例：既要挡住部分篇数修辞，也要认得住显式总量。"""
    cases = [
        ("深入解读 3 篇 Science 背靠背拆解植物糖代码", None),
        ("本期共 8 篇论文，其中 3 篇来自 Science", 8),
        ("Science 连发三弹；另有 2 篇预印本未计入", None),
        ("本期解读 8 篇", 8),
        ("合计 8 篇文献", 8),
    ]
    bad = 0
    for text, expect in cases:
        got = detect_declared_count(text)
        ok = got == expect
        print("  %s %r -> %s（期望 %s）" % ("✓" if ok else "✗", text, got, expect))
        if not ok:
            bad += 1
    print("\n回归用例 %d 条，失败 %d 条" % (len(cases), bad))
    return 0 if bad == 0 else 2


def extract_refs(raw_text):
    """从参考文献区抽 [n] 作者 标题. 期刊. 日期. DOI: xxx"""
    refs = {}
    for m in re.finditer(
        r"\[(\d+)\]\s*(.+?)\s*DOI:\s*(10\.[^\s]+)", raw_text, flags=re.S
    ):
        no, body, doi = m.group(1), m.group(2), norm_doi(m.group(3))
        dates = DATE_RE.findall(body)
        # 期刊名：body 里最后一个 ". Xxx ." 形式，取粗糙匹配
        refs[no] = {
            "doi": doi,
            "date": "-".join(dates[0]) if dates else "",
            "body": body[:220],
        }
    return refs


def extract_card_dois(raw_text):
    """抽卡片区每个 DOI 及其所在论文卡的日期。

    论文卡结构：<span class="paper-date">DATE</span> ... <div class="paper-meta">...DOI...
    同一张卡里的 paper-date 与 paper-meta 之间的 span 数固定，但为稳妥起见
    用「以 paper-card 切块」的方式配对：每个 card 块内的 DOIs 对应块内所有 paper-date。
    """
    out = []
    # 以 paper-card 切块（卡片之间不会交叉）
    blocks = re.split(r'class="paper-card"', raw_text)
    for blk in blocks[1:]:
        dates = DATE_RE.findall(blk)
        # 卡片里第一个日期就是该论文的 paper-date
        cdate = "-".join(dates[0]) if dates else ""
        for m in DOI_RE.finditer(blk):
            out.append({"doi": norm_doi(m.group(0)), "date": cdate})
        # 参考文献区若混入同一块（末块），其 DOI 会带上错误日期——靠 ref 解析兜底，
        # 这里只保留块内出现的 DOI，日期取块内首个 paper-date
    return out


def main():
    ap = argparse.ArgumentParser(description="报告自洽性校验")
    ap.add_argument("--html", help="成品 HTML 路径")
    ap.add_argument("--json", help="结果写 JSON")
    ap.add_argument("--selftest", action="store_true", help="只跑篇数断言回归用例")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.html:
        ap.error("--html 必填（或改用 --selftest）")

    if not os.path.isfile(args.html):
        print("[错误] 找不到文件：%s" % args.html)
        return 2

    with io.open(args.html, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    text = strip_html(raw)

    fails, warns, oks = [], [], []

    # --- 检查 1：必填版块齐备（审计第 5 维）---
    print("【1】报告完整度")
    for name, pats in REQUIRED_BLOCKS:
        if any(re.search(p, text) for p in pats):
            oks.append("版块存在：%s" % name)
            print("   ✓ %s" % name)
        else:
            fails.append("缺少版块：%s" % name)
            print("   ✗ 缺少版块：%s" % name)

    # --- 检查 2：DOI 唯一性（同一文献是否重复计入）---
    print("\n【2】DOI 唯一性")
    all_dois = [norm_doi(m.group(0)) for m in DOI_RE.finditer(text)]
    uniq = []
    seen = set()
    for d in all_dois:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    print("   正文出现 DOI 引用 %d 次，去重后 %d 个" % (len(all_dois), len(uniq)))

    # --- 检查 3：卡片日期 vs 参考文献日期 一致性（审计第 6 维核心）---
    print("\n【3】同一文献日期口径一致性（卡片 vs 参考文献）")
    refs = extract_refs(text)
    cards = extract_card_dois(raw)
    card_by_doi = {}
    for c in cards:
        card_by_doi.setdefault(c["doi"], []).append(c["date"])

    mism = 0
    for no, r in sorted(refs.items(), key=lambda kv: int(kv[0])):
        doi, rdate = r["doi"], r["date"]
        cdates = [d for d in card_by_doi.get(doi, []) if d]
        if not cdates:
            warns.append("参考文献[%s] 的 DOI 未在卡片区找到对应：%s" % (no, doi))
            print("   ⚠ [%s] %s —— 卡片区未找到该 DOI" % (no, doi))
            continue
        if rdate and rdate not in cdates:
            mism += 1
            fails.append("日期不一致 [%s] %s：参考文献 %s vs 卡片 %s" % (no, doi, rdate, cdates))
            print("   ✗ [%s] %s：参考文献 %s ≠ 卡片 %s" % (no, doi, rdate, cdates))
        else:
            print("   ✓ [%s] %s 日期一致（%s）" % (no, doi, rdate or "卡片未标注"))

    # --- 检查 4：参考文献编号连续 ---
    print("\n【4】参考文献编号连续性")
    if refs:
        nums = sorted(int(k) for k in refs)
        expected = list(range(1, len(nums) + 1))
        if nums == expected:
            print("   ✓ 编号连续 1..%d" % len(nums))
        else:
            missing = sorted(set(expected) - set(nums))
            fails.append("参考文献编号不连续，缺 %s" % missing)
            print("   ✗ 编号不连续，缺 %s" % missing)
    else:
        warns.append("未解析到参考文献条目")
        print("   ⚠ 未解析到参考文献条目（格式可能变化）")

    # --- 检查 5：篇数统计自洽 ---
    print("\n【5】篇数统计自洽")
    declared = detect_declared_count(text)
    if declared is not None:
        if declared == len(uniq):
            print("   ✓ 声称 %d 篇，实得 %d 个唯一 DOI" % (declared, len(uniq)))
        else:
            fails.append("篇数不符：声称 %d 篇，实得 %d 个唯一 DOI" % (declared, len(uniq)))
            print("   ✗ 声称 %d 篇，实得 %d 个唯一 DOI" % (declared, len(uniq)))
    else:
        # 老版本报告无篇数字样：只告警，不算失败（不追溯历史格式）
        warns.append("未找到「本期解读 N 篇」字样（老版本模板，不判失败）")
        print("   ⚠ 未找到篇数字样（老版本模板，不判失败）")

    # --- 检查 6：日期红线（>30 天）---
    print("\n【6】日期红线（>30 天不作进展条目）")
    m = re.search(r"日期：\s*(20\d{2}-\d{2}-\d{2})", text)
    if m:
        import datetime
        today = datetime.date.fromisoformat(m.group(1))
        late = []
        for no, r in refs.items():
            if r["date"]:
                try:
                    d = datetime.date.fromisoformat(r["date"])
                    if (today - d).days > 30:
                        late.append("[%s] %s (%d 天前)" % (no, r["doi"], (today - d).days))
                except ValueError:
                    pass
        if late:
            fails.append("以下条目超出 30 天红线：%s" % "; ".join(late))
            print("   ✗ 超红线：")
            for l in late:
                print("       %s" % l)
        else:
            print("   ✓ 全部条目在 30 天窗口内")
    else:
        warns.append("报告中未找到日期字段")
        print("   ⚠ 未找到报告日期")

    # --- 汇总 ---
    print("\n" + "=" * 62)
    print("失败 %d 项 / 告警 %d 项" % (len(fails), len(warns)))
    if fails:
        print("失败明细：")
        for f in fails:
            print("  ✗ %s" % f)
    if warns:
        print("告警明细：")
        for w in warns:
            print("  ⚠ %s" % w)
    if not fails and not warns:
        print("✓ 报告自洽性全部通过。")

    if args.json:
        with io.open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"file": args.html, "fails": fails, "warns": warns, "oks": oks},
                      fh, ensure_ascii=False, indent=2)
        print("结果已写入：%s" % args.json)

    return 2 if fails else (1 if warns else 0)


if __name__ == "__main__":
    sys.exit(main())
