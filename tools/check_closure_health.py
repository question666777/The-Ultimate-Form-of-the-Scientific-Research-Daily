#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_closure_health.py — 闭环健康检查（审计第 11 维）

把「经验有没有进代码、提案有没有人处理」变成可复跑的确定性检查。
背景：2026-09-22 项目评估发现两处硬伤 ——
  (1) lessons.md 里 L027/L028/L029 写着「→ 修复：…」但 tools/ 里没有任何实现，
      属于「假 fixed」：修在同一天的临时脚本里，第二天重写时又踩同一个坑；
  (2) learning/proposals/ 累计 8 份提案，0 份标注状态，提案队列成了坟场。
跑再快，不闭环就是原地打转。本脚本专治这个。

检查项
------
  A. active 经验条数 <= 上限（默认 10，读 lessons.md 头部的「active 上限 N 条」）
  B. 「已固化未转 fixed」：扫描 tools/*.py 头部注释里的 Lxxx 编号声明，
     与 lessons.md 的 active 集合求交 —— 交集即「代码里已固化，但 lessons 里还挂 active」
  C. proposals/ 里未标注状态的文件数（应 0）
  D. proposals/ 里滞留 > N 天且仍无状态的文件数（默认 7 天，用 mtime）
  E. 经验库结构健康：
     E1. fixed 区条数 <= 上限（默认 15）
     E2. 是否存在 retired 区（只进不出 = 没消化）
     E3. active/fixed/retired 三区比例是否合理（active 应最少、retired 应最多）
  F. 「假 active」检测：active 里写着"已固化"或"下次运行继续执行"但实际已经进代码的，
     属于挂着 active 名字的 fixed，应清退

退出码：0 通过 / 1 告警 / 2 失败
  A 超限              → 告警
  B 有交集            → 告警（假 fixed，最隐蔽的一种）
  C 有未状态提案      → 失败
  D 有滞留提案        → 失败
  E1 fixed超限        → 告警（fixed也会膨胀，纯代码级的该退休就退休）
  E2 无retired区      → 告警（只进不出的经验库是负担不是资产）
  F 有假active        → 告警
只用标准库。
"""

import argparse
import datetime
import glob
import io
import os
import re
import sys

DEFAULT_ACTIVE_CAP = 10
DEFAULT_FIXED_CAP = 15
DEFAULT_STALE_DAYS = 7

# 提案状态标记（AGENTS.md 第六节：已采纳 / 已拒绝 / 待办 三种合法状态）
STATUS_PAT = re.compile(r"已采纳|已拒绝|已应用|已落地|部分已应用|部分采纳|路线变更|不具备条件|待办")
# 未结案状态：标了「待办」但长期未处理，由 D 项提醒
PENDING_PAT = re.compile(r"待办")


def read_text(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def read_active_cap(lessons_text):
    m = re.search(r"active\s*上限\s*(\d+)\s*条", lessons_text)
    if m:
        return int(m.group(1))
    return DEFAULT_ACTIVE_CAP


def split_lessons(lessons_text):
    """返回 (active_set, fixed_set, retired_set)，元素为 'L007' 形式。"""
    def ids(block):
        # 允许字母后缀（如 L035b），否则带后缀的条目会被漏计，上限校验少算一条
        return set(re.findall(r"^- \*\*(L\d+[a-z]?)\*\*", block, re.M))

    i_act = lessons_text.find("## active")
    i_fix = lessons_text.find("## fixed")
    i_ret = lessons_text.find("## retired")
    if i_act < 0:
        return set(), set(), set()
    act_text = lessons_text[i_act:i_fix] if i_fix > i_act else lessons_text[i_act:]
    fix_text = ""
    ret_text = ""
    if i_fix > 0:
        if i_ret > i_fix:
            fix_text = lessons_text[i_fix:i_ret]
            ret_text = lessons_text[i_ret:]
        else:
            fix_text = lessons_text[i_fix:]
    return ids(act_text), ids(fix_text), ids(ret_text)


def scan_codified(tools_dir):
    """扫 tools/*.py 头部注释，返回 {Lxxx: [脚本名...]}。

    判定规则（踩过两次坑后定的）：
    - 只认「**行首** L### 后跟内容」的写法 —— 这是脚本在明确列出自己的固化清单，
      如 `L007  Crossref cursor 深分页时传 sort=published 必 400 → 本脚本不传 sort`。
    - 句子中间顺带提及不算，如 check_retractions.py 里
      「（见 lessons L014 / L027）」、以及本脚本自己文档里的「背景：L027/L028…」。
    只扫文件前 60 行（固化清单写在头部 docstring）。
    """
    found = {}
    for path in sorted(glob.glob(os.path.join(tools_dir, "*.py"))):
        name = os.path.basename(path)
        text = read_text(path)
        head = "\n".join(text.split("\n")[:60])
        for line in head.split("\n"):
            m = re.match(r"^\s*(L\d{3})\b\s*\S", line)
            if m:
                lid = m.group(1)
                found.setdefault(lid, [])
                if name not in found[lid]:
                    found[lid].append(name)
    return found


def check_proposals(proposals_dir, stale_days, today):
    """返回 (无状态列表, 滞留列表, 总数)。

    无状态 = 完全没标状态（失败）；滞留 = 标了「待办」但超过 stale_days 未处理（告警）。
    修正（2026-10-11）：原 STATUS_PAT 漏「待办」，把 AGENTS.md 认可的合法状态误判为无状态。
    """
    no_status, stale = [], []
    files = [f for f in sorted(glob.glob(os.path.join(proposals_dir, "*.md")))
             if not os.path.basename(f).lower().startswith("readme")]
    for path in files:
        text = read_text(path)
        head = text[:900]  # 状态标注约定写在文件开头 900 字符内
        base = os.path.basename(path)
        mtime = datetime.date.fromtimestamp(os.path.getmtime(path))
        age = (today - mtime).days
        if not STATUS_PAT.search(head):
            no_status.append(base)
            if age > stale_days:
                stale.append("%s（无状态滞留 %d 天）" % (base, age))
        elif PENDING_PAT.search(head) and age > stale_days:
            stale.append("%s（待办滞留 %d 天）" % (base, age))
    return no_status, stale, len(files)


def main():
    ap = argparse.ArgumentParser(description="闭环健康检查（审计第 11 维）")
    ap.add_argument("--lessons", default=os.path.join("learning", "lessons.md"))
    ap.add_argument("--tools", default="tools")
    ap.add_argument("--proposals", default=os.path.join("learning", "proposals"))
    ap.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS)
    ap.add_argument("--json", help="把结果写到该 JSON 路径")
    args = ap.parse_args()

    for p in (args.lessons, args.tools, args.proposals):
        if not os.path.exists(p):
            print("[错误] 路径不存在：%s" % p)
            return 2

    today = datetime.date.today()
    lessons_text = read_text(args.lessons)
    cap = read_active_cap(lessons_text)
    active, fixed, retired = split_lessons(lessons_text)
    codified = scan_codified(args.tools)

    fake_fixed = sorted(active & set(codified))
    no_status, stale, n_prop = check_proposals(args.proposals, args.stale_days, today)

    # E. 经验库结构健康
    fixed_cap = DEFAULT_FIXED_CAP
    m_fixed = re.search(r"fixed.*?上限\s*(\d+)\s*条", lessons_text)
    if m_fixed:
        fixed_cap = int(m_fixed.group(1))
    fixed_over = len(fixed) > fixed_cap
    has_retired = len(retired) > 0
    # E3: 比例合理性——retired应 >= fixed（说明经验在消化），active应最少
    ratio_ok = len(retired) >= len(fixed) and len(active) <= len(fixed)

    # F. 假active检测：active里写着"已固化"或"下次运行继续执行"且已在codified中的
    fake_active = []
    i_act_end = lessons_text.find("## fixed")
    if i_act_end < 0:
        i_act_end = len(lessons_text)
    active_block = lessons_text[lessons_text.find("## active"):i_act_end]
    for lid in active:
        # 在active区块里找这条经验的行
        pattern = re.compile(r"- \*\*" + re.escape(lid) + r"\*\*.*$", re.M)
        m = pattern.search(active_block)
        if m and ("已固化" in m.group(0) or "已在" in m.group(0) or "已修复" in m.group(0)):
            if lid in codified:
                fake_active.append(lid)
    fake_active.sort()

    print("=" * 64)
    print("闭环健康检查（审计第 11 维）")
    print("=" * 64)

    # A. active 上限
    over = len(active) > cap
    print("\n[A] active 经验条数：%d / 上限 %d  %s" % (
        len(active), cap, "【超限】" if over else "OK"))
    if over:
        print("    超限 %d 条。可固化项：见下方 [B]；其余考虑 retired。" % (len(active) - cap))

    # B. 假 fixed
    print("\n[B] 已固化进 tools/ 但仍挂 active 的经验：%d 条" % len(fake_fixed))
    if fake_fixed:
        for lid in fake_fixed:
            print("    %s  ← 代码里已固化于 %s" % (lid, ", ".join(codified[lid])))
        print("    这些应从 active 转 fixed（否则 active 永远降不下来）。")
    else:
        print("    OK（无假 fixed）")

    # C/D. 提案队列
    print("\n[C] 提案总数：%d 份；未标注状态：%d 份" % (n_prop, len(no_status)))
    if no_status:
        for f in no_status:
            print("    无状态：%s" % f)
    else:
        print("    OK（全部已标注）")

    print("\n[D] 待办/无状态滞留 > %d 天：%d 份" % (args.stale_days, len(stale)))
    for s in stale:
        print("    %s" % s)
    if not stale:
        print("    OK")

    # E. 经验库结构健康
    print("\n[E] 经验库结构健康：active=%d  fixed=%d  retired=%d" % (
        len(active), len(fixed), len(retired)))
    # E1
    print("    [E1] fixed 条数：%d / 上限 %d  %s" % (
        len(fixed), fixed_cap, "【超限】" if fixed_over else "OK"))
    if fixed_over:
        print("         fixed 区也会膨胀，纯代码级细碎 bug 该退休就退休，别占注意力。")
    # E2
    print("    [E2] retired 区：%s" % ("存在（在消化）" if has_retired else "【缺失】只进不出 = 经验库是负担不是资产"))
    # E3
    print("    [E3] 三区比例合理：%s" % ("OK" if ratio_ok else "【告警】retired 应 >= fixed，active 应最少"))
    if not ratio_ok:
        print("         正常形态：retired（消化掉的）> fixed（固化的）> active（待处理的）")

    # F. 假active
    print("\n[F] active 里写着「已固化」但实际已进代码的：%d 条" % len(fake_active))
    if fake_active:
        for lid in fake_active:
            print("    %s  ← 固化于 %s" % (lid, ", ".join(codified.get(lid, ["?"]))))
        print("    挂着 active 名字的 fixed——转 fixed 或 retired。")
    else:
        print("    OK（无假 active）")

    # 结论
    verdict = 0
    warnings = []
    errors = []
    if len(no_status) > 0:
        errors.append("C: %d份提案无状态" % len(no_status))
    if len(stale) > 0:
        warnings.append("D: %d份提案滞留待处理" % len(stale))
    if over:
        warnings.append("A: active超限(%d/%d)" % (len(active), cap))
    if fake_fixed:
        warnings.append("B: %d条假fixed" % len(fake_fixed))
    if fixed_over:
        warnings.append("E1: fixed超限(%d/%d)" % (len(fixed), fixed_cap))
    if not has_retired:
        warnings.append("E2: 无retired区")
    if not ratio_ok:
        warnings.append("E3: 三区比例失衡")
    if fake_active:
        warnings.append("F: %d条假active" % len(fake_active))

    if errors:
        verdict = 2
    elif warnings:
        verdict = 1
    label = {0: "通过", 1: "告警", 2: "失败"}[verdict]
    print("\n" + "-" * 64)
    print("结论：%s（退出码 %d）" % (label, verdict))
    if warnings:
        print("  告警项：" + "；".join(warnings))
    if errors:
        print("  失败项：" + "；".join(errors))
    if verdict == 0:
        print("  含义：经验库在正常新陈代谢，提案队列无积压。")
    elif verdict == 1:
        print("  含义：有积压信号，但还没到「系统空转」的程度。")
    elif verdict == 2:
        print("  含义：提案队列存在无状态条目或严重结构问题——必须处理。")

    if args.json:
        import json
        payload = {
            "checked_at": today.isoformat(),
            "active_count": len(active),
            "active_cap": cap,
            "active_over": over,
            "fixed_count": len(fixed),
            "fixed_cap": fixed_cap,
            "fixed_over": fixed_over,
            "retired_count": len(retired),
            "has_retired": has_retired,
            "ratio_ok": ratio_ok,
            "fake_fixed": {lid: codified[lid] for lid in fake_fixed},
            "fake_active": fake_active,
            "proposals_total": n_prop,
            "proposals_no_status": no_status,
            "proposals_stale": stale,
            "warnings": warnings,
            "errors": errors,
            "verdict": verdict,
        }
        with io.open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print("明细已写入 %s" % args.json)

    return verdict


if __name__ == "__main__":
    sys.exit(main())
