#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""check_engine_health.py — 机制推理引擎 / Q 问题库健康检查（审计第 13 维）

把「推理引擎还在不在生成解释、Q 问题库还在不在生长」变成可复跑的确定性检查。
背景：2026-10-11 机制推理引擎（R1 桥接 / R2 缺口 / R3 张力 / R4 瓶颈）落地后，
产出的是「候选池 + Q 提案」，须人工复核才落库。若没人复核、闸门失效导致噪音爆炸、
或 C 条目退化成并置，系统就在「记录了很多、推理很少」地空转。本脚本专治这个。

检查项
------
  A. R1 回归（C004）：桥接候选仍应含 F051×F108 且正交 —— 失败 = 引擎核心能力退化
  B. Q 问题库：非空 + 状态全在闭集（open/explained/falsified）—— 空/越界 = 失败
  C. Q 假说闭环：explained >= 1（至少一条 Q 被 C 回答）—— 0 = 告警（问题库只进不出）
  D. 深度分级：C 条目中 D2+（桥接/瓶颈/颠覆）占比 —— D0-D1 占比 > 50% = 告警
  E. 闸门收敛：R2 层级跳跃 / R4 瓶颈候选数在合理区间 —— 爆炸 = 告警（闸门/词典失效）
  F. 候选池复核：R2/R4 候选里至少有一条已落库为 Q —— 候选非空但 0 落库 = 告警（空转）
  G. R5 同构：跨层级同构候选数在合理区间 —— 爆炸 = 告警（模式闸门失效）
  H. 批判闭环回归：已知张力主体应被「边界条件」命中 —— 未命中 = 失败（批判逻辑空转）
  I. 新颖性闸门精度回归：合成命中应被正确分档（双专有词=强，含泛化词=弱）—— 分错 = 失败

退出码：0 通过 / 1 告警 / 2 失败
只用标准库。
"""

import argparse
import datetime
import io
import json
import os
import re
import sys

sys.dont_write_bytecode = True  # 导入 insight_scan/reason_engine/novelty_gate 会产生 __pycache__，而自审 R2 正查它

import insight_scan as isc
import reason_engine as eng
import novelty_gate as ng

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

QSTATUS = {"open", "explained", "falsified"}
DEEP = {"D2", "D3", "D4"}
D_RE = re.compile(r"\bD([0-4])\b")


def d_grade(head):
    """从 C 条目头部（如「2026-10-01｜跨 2 期｜D2」）取深度级，无标记返回 None。"""
    m = D_RE.search(head)
    return "D" + m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser(description="机制推理引擎 / Q 问题库健康检查（审计第 13 维）")
    ap.add_argument("--cap-jumps", type=int, default=10, help="R2 层级跳跃候选数上限")
    ap.add_argument("--cap-btl", type=int, default=15, help="R4 瓶颈候选数上限")
    ap.add_argument("--cap-analogy", type=int, default=20, help="R5 同构候选数上限")
    ap.add_argument("--json", help="把结果写到该 JSON 路径")
    args = ap.parse_args()

    for p in (isc.FINDINGS, isc.ENTITIES, eng.MEDIA):
        if not os.path.isfile(p):
            print("[错误] 读不到 %s" % p)
            return 2

    text = isc.read_text(isc.FINDINGS)
    findings, links = isc.parse_findings(text)
    lex = isc.load_lexicon(isc.ENTITIES)
    media = eng.load_media(eng.MEDIA)
    aidx = isc.build_alias_index(lex)
    mats = isc.compile_matchers(lex)

    rows, _f_media, _fby = eng.r1_bridge(findings, media, aidx, mats)
    jumps, _hubs = eng.r2_gap(findings, lex, aidx, mats)
    btl = eng.r4_bottleneck(findings, lex, aidx)
    questions = isc.parse_questions(text)

    print("=" * 64)
    print("机制推理引擎 / Q 问题库健康检查（审计第 13 维）")
    print("=" * 64)

    fails, warns = [], []

    # A. R1 回归（C004）：引擎须能独立复现心磷脂桥
    hit = [r for r in rows if {r["a"], r["b"]} == {"F051", "F108"} and r["orth"]]
    print("\n[A] R1 回归（C004 F051×F108 桥接）：%s" % ("OK" if hit else "【失败】未复现"))
    if not hit:
        print("    引擎未能复现已知成果——介质词典或三步算法退化，须回炉。")
        fails.append("A: R1 未复现 F051×F108 桥接")

    # B. Q 库非空 + 状态合法
    bad_status = [q["id"] for q in questions if q["status"] not in QSTATUS]
    print("\n[B] Q 问题库：%d 条；状态越界 %d 条" % (len(questions), len(bad_status)))
    if not questions:
        print("    【失败】问题库为空——「未知」没有一等公民，推理退化为数据驱动找能连的。")
        fails.append("B: Q 问题库为空")
    elif bad_status:
        print("    【失败】状态越界：%s（闭集 open/explained/falsified）" % "、".join(bad_status))
        fails.append("B: Q 状态越界 %s" % "、".join(bad_status))
    else:
        n = {s: sum(1 for q in questions if q["status"] == s) for s in QSTATUS}
        print("    open %d｜explained %d｜falsified %d" % (n["open"], n["explained"], n["falsified"]))

    # C. Q 假说闭环：至少一条被回答
    n_expl = sum(1 for q in questions if q["status"] == "explained")
    print("\n[C] Q 假说闭环：explained %d 条" % n_expl)
    if questions and n_expl == 0:
        print("    【告警】无一条 Q 被 C 回答——问题库只进不出，假说生成停滞。")
        warns.append("C: 无 Q 被回答（explained=0）")
    elif n_expl > 0:
        print("    OK（假说生成闭环在运转）")

    # D. 深度分级：C 条目 D2+ 占比
    graded = [(c["id"], d_grade(c["head"])) for c in links]
    deep = [cid for cid, d in graded if d in DEEP]
    shallow = [cid for cid, d in graded if d in ("D0", "D1")]
    unknown = [cid for cid, d in graded if d is None]
    print("\n[D] 深度分级：C %d 条（D2+ %d｜D0-D1 %d｜未标 %d）"
          % (len(graded), len(deep), len(shallow), len(unknown)))
    if graded:
        ratio = len(shallow) / float(len(graded))
        if ratio > 0.5:
            print("    【告警】D0-D1（并置型）占比 %.0f%% > 50%%——洞见质量退化，逼新条目往 D2+ 走。" % (ratio * 100))
            warns.append("D: D0-D1 占比 %.0f%% >50%%" % (ratio * 100))
        else:
            print("    OK（D2+ 为主，桥接/瓶颈/颠覆型占多数）")
    if unknown:
        print("    未标 D 级：%s（C 条目应带 D 标记）" % "、".join(unknown))

    # E. 闸门收敛：候选数在区间内
    print("\n[E] 闸门收敛：R2 层级跳跃 %d（上限 %d）｜R4 瓶颈 %d（上限 %d）"
          % (len(jumps), args.cap_jumps, len(btl), args.cap_btl))
    if len(jumps) > args.cap_jumps:
        print("    【告警】R2 候选爆炸——闸门或实体词典失效，噪音涌入。")
        warns.append("E: R2 候选爆炸(%d>%d)" % (len(jumps), args.cap_jumps))
    if len(btl) > args.cap_btl:
        print("    【告警】R4 候选爆炸——闸门或机制谓词闭集失效。")
        warns.append("E: R4 候选爆炸(%d>%d)" % (len(btl), args.cap_btl))
    if len(jumps) <= args.cap_jumps and len(btl) <= args.cap_btl:
        print("    OK（候选数在合理区间，闸门仍在收敛噪音）")

    # F. 候选池复核：R2/R4 候选里至少一条已落库为 Q
    q_keys = {frozenset(q["triggers"]) for q in questions}
    cand_keys = set()
    for j in jumps:
        cand_keys.add(frozenset([j["fid"]]))
    for r in btl:
        cand_keys.add(frozenset([r["f_in"], r["f_out"]]))
    landed = q_keys & cand_keys
    print("\n[F] 候选池复核：R2/R4 候选 %d 个触发集；已落库为 Q %d 个"
          % (len(cand_keys), len(landed)))
    if cand_keys and not landed:
        print("    【告警】候选池只增不落库——引擎在产、没人复核，系统空转。")
        warns.append("F: 候选池 0 落库")
    elif landed:
        print("    OK（候选有被人工复核并落库）")

    # G. R5 同构：可运行 + 候选数在区间
    analogy, chains, groups = eng.r5_analogy(findings, lex, aidx)
    n_groups = sum(1 for v in groups.values() if len(v) >= 2)
    print("\n[G] R5 跨层级同构：两跳链 %d 条｜同模式组 %d 组｜同构候选 %d 对（上限 %d）"
          % (len(chains), n_groups, len(analogy), args.cap_analogy))
    if len(analogy) > args.cap_analogy:
        print("    【告警】R5 候选爆炸——模式闸门失效，噪音涌入。")
        warns.append("G: R5 候选爆炸(%d>%d)" % (len(analogy), args.cap_analogy))
    else:
        print("    OK（同构候选在合理区间；稀疏库为 0 属正常，宁缺毋滥）")

    # H. 生成-批判闭环回归：已知张力主体应被「边界条件」命中
    tensions = eng.r3_tension(findings, lex, aidx)
    edges = eng.build_edges(findings, aidx)
    adj = eng.build_causal_graph(edges)
    probe = sorted({t["s"] for t in tensions})
    print("\n[H] 生成-批判闭环：库内张力主体 %d 个%s"
          % (len(probe), ("（" + "、".join(probe) + "）") if probe else ""))
    if probe:
        cand = {"kind": "回归", "label": probe[0], "refs": [], "ents": [probe[0]], "chain": None}
        atk = eng.critique_candidate(cand, edges, tensions, adj)
        hit = [a for a in atk if a["kind"] == "边界条件"]
        if hit:
            print("    OK（批判环命中已知张力，逻辑不空转）")
        else:
            print("    【失败】批判环未命中已知张力——逻辑空转，须回炉。")
            fails.append("H: 批判环未命中已知张力 %s" % probe[0])
    else:
        print("    （库内无张力，批判逻辑无法用真实数据回归——跳过，不算失败）")

    # I. 新颖性闸门精度回归：合成命中应被正确分档（双专有词=强，含泛化词=弱）
    ta, tb = ["IRE1α", "hypoxia"], ["diabetes"]
    probe_items = [
        {"title": "IRE1α pathway in type 2 diabetes mellitus", "abstract": ""},  # 双专有词 → 强
        {"title": "Hypoxia and diabetes in wound healing", "abstract": ""},        # 左侧仅泛化 → 弱
        {"title": "Hypoxia in tumours", "abstract": ""},                           # 只命中左侧 → 不算命中
    ]
    hits = ng.confirm(probe_items, ta, tb)
    strong = [h for h in hits if h["strong"]]
    weak = [h for h in hits if not h["strong"]]
    print("\n[I] 新颖性闸门精度回归：命中 %d 条（强 %d｜弱 %d），期望 命中2/强1/弱1"
          % (len(hits), len(strong), len(weak)))
    if len(hits) == 2 and len(strong) == 1 and len(weak) == 1:
        print("    OK（双专有词判强、泛化词降档、单侧命中不算命中——精度闸门不空转）")
    else:
        print("    【失败】分档错误——确认逻辑退化，闸门可能误杀选题或放过抢先。")
        fails.append("I: 新颖性闸门分档错误（命中%d/强%d/弱%d）" % (len(hits), len(strong), len(weak)))

    verdict = 2 if fails else (1 if warns else 0)
    label = {0: "通过", 1: "告警", 2: "失败"}[verdict]
    print("\n" + "-" * 64)
    print("结论：%s（退出码 %d）" % (label, verdict))
    if warns:
        print("  告警项：" + "；".join(warns))
    if fails:
        print("  失败项：" + "；".join(fails))
    if verdict == 0:
        print("  含义：推理引擎在生成解释，Q 问题库在生长并被回答。")
    elif verdict == 1:
        print("  含义：引擎能跑，但闭环有松动（问题没人答 / 候选没人复核 / 洞见变浅）。")
    elif verdict == 2:
        print("  含义：引擎核心能力退化或问题库失效——必须处理。")

    if args.json:
        payload = {
            "checked_at": datetime.date.today().isoformat(),
            "r1_regression": bool(hit),
            "q_total": len(questions),
            "q_status": {s: sum(1 for q in questions if q["status"] == s) for s in QSTATUS},
            "q_bad_status": bad_status,
            "q_explained": n_expl,
            "c_total": len(graded),
            "c_deep": deep,
            "c_shallow": shallow,
            "c_unknown_grade": unknown,
            "jumps": len(jumps),
            "bottlenecks": len(btl),
            "cand_keys": len(cand_keys),
            "cand_landed": len(landed),
            "r5_chains": len(chains),
            "r5_groups": n_groups,
            "r5_candidates": len(analogy),
            "critique_tension_subjects": probe,
            "gate_precision": {"hits": len(hits), "strong": len(strong), "weak": len(weak)},
            "warnings": warns,
            "errors": fails,
            "verdict": verdict,
        }
        with io.open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print("明细已写入 %s" % args.json)

    return verdict


if __name__ == "__main__":
    sys.exit(main())