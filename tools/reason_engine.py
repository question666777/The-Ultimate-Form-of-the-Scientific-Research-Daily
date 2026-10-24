#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reason_engine.py — 机制推理引擎（insight-synthesis 执行层）

设计见 .agents/skills/insight-synthesis/reason_engine.md。
已实现：
  R1 介质桥接器：把 media_bridge.md 三步法（落地 → 同执行器 → 判正交）脚本化，
     对库内结论两两配对，输出「同执行器 + 正交旋钮」的桥接候选。
  R2 缺口检测器：层级跳跃（一条边两端 role 跨 >1 级）+ 孤立枢纽（process 实体
     只在 1 条边出现却被多条结论提及）→ 自动产出缺口类 Q 提案。
  R3 张力检测器：同主体反向分叉（同时促进 o1、抑制 o2）→ 自动产出张力类 Q 提案。
  R4 瓶颈/冗余检测器：机制图中间节点 M，枚举它中介的跨文献对 (X,Y)，数绕开 M 的
     替代路径数——0 条为【瓶颈】（必经，候选干预靶点），>0 条为【冗余】（备份路径，
     候选耐药机制）→ 自动产出瓶颈/冗余类 Q 提案。
  R5 跨层级同构对齐：两跳链抽象成 pattern（谓词序列 + role 序列），同 pattern 且三节点
     实体不相交 → 跨系统迁移假说候选。最易产噪音，只当候选池，须过批判环。
  生成-批判闭环：对 R1/R2/R4/R5 候选池逐条跑自我反驳环——反例（severity 2 高危）/
     边界条件、断链点（severity 1 需人工）→ 裁决 高危/需人工/存活。

与 insight_scan.py 分工：v2 负责召回（共现/二跳/冲突），本引擎做二次判定（加深度）。
解析与词典函数直接复用 insight_scan，不重复造。

验收：C004 回归——喂入全部 F，应输出 F051 × F108，
共同执行器 = OPA1/嵴重塑，正交 = 是。

用法：
  python tools\\reason_engine.py
  python tools\\reason_engine.py --top 15
  python tools\\reason_engine.py --write learning\\runs\\bridge_candidates.md
  python tools\\reason_engine.py --questions --q-out learning\\runs\\q_proposals.md
  python tools\\reason_engine.py --analogy --critique --c-out learning\\runs\\critique.md

只用标准库。
"""

import argparse
import io
import itertools
import json
import os
import sys

sys.dont_write_bytecode = True  # 导入 insight_scan 会产生 __pycache__，而自审 R2 正查它

import insight_scan as isc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA = os.path.join(ROOT, "tools", "media.json")

ORTH_BONUS = 1.5

# R2 层级序：从分子到疾病的组织层级轴。intervention/other 不在轴上（干预/工具不是
# 因果层级的一环），层级跳跃检测一律跳过。
LEVELS = ["molecular", "process", "cellular", "tissue", "organism", "disease"]
LEVEL_RANK = {r: i for i, r in enumerate(LEVELS)}

# R2 ① 只认机制型谓词：这类谓词断言「谁作用于谁」，跨层级直连才意味着「中间缺中介」。
# TREATS/INDICATES/ASSOCIATED/TARGETS 是标签/治疗/诊断关系，本身不构成机制问题，
# 若纳入会把「抗体抑制病毒」「A 标志 B」这类直连误报成缺口。
CAUSAL_PREDS = {"ACTIVATES", "INHIBITS", "CAUSES", "REGULATES"}


def load_media(path):
    data = json.load(io.open(path, encoding="utf-8"))
    return data.get("media", {})


def build_edges(findings, aidx):
    """归一后的三元组边：{s, o, p, fid}。谓词不在闭集内则丢弃；实体别名归一为规范名。"""
    edges = []
    for f in findings:
        for t in f["triples"]:
            if t["p"] not in isc.PREDICATES:
                continue
            edges.append({"s": isc.resolve(aidx, t["s"]) or t["s"],
                          "o": isc.resolve(aidx, t["o"]) or t["o"],
                          "p": t["p"], "fid": f["id"]})
    return edges


def f_entity_sets(findings, aidx, mats):
    """每条 F 的实体集合：三元组 s/o 归一 + 文本匹配。键为 F id。"""
    out = {}
    for f in findings:
        if not f["id"]:
            continue
        ents = set()
        for t in f["triples"]:
            for name in (t["s"], t["o"]):
                r = isc.resolve(aidx, name)
                ents.add(r if r else name)
        for canon, pat in mats:
            if pat.search(f["text"]):
                ents.add(canon)
        out[f["id"]] = ents
    return out


def f_media_map(f_ents, media):
    """F id → 关联的介质 key 集合 = 实体命中 ∪ 显式锚定(anchors)。

    实体命中：F 的实体 ∩ 介质 entities ≠ ∅（只含直接物理量，见 media.json schema）。
    anchors：经「落地」推理锚定的 F（人工维护）——落地是知识动作，字符串匹配做不到。
    """
    out = {}
    for fid, ents in f_ents.items():
        ms = {mk for mk, m in media.items() if ents & set(m.get("entities", []))}
        for mk, m in media.items():
            if fid in m.get("anchors", []):
                ms.add(mk)
        if ms:
            out[fid] = ms
    return out


def r1_bridge(findings, media, aidx, mats):
    f_ents = f_entity_sets(findings, aidx, mats)
    f_media = f_media_map(f_ents, media)
    f_by_id = {f["id"]: f for f in findings if f["id"]}
    dom_of, dom_ents = isc.domain_layer(findings, f_ents)

    rows = []
    for a, b in itertools.combinations(sorted(f_media), 2):
        for ma in f_media[a]:
            for mb in f_media[b]:
                if ma == mb:
                    continue
                ex = set(media[ma]["executors"]) & set(media[mb]["executors"])
                if not ex:
                    continue
                orth = not (set(media[ma]["knobs"]) & set(media[mb]["knobs"]))
                conf = (isc.CONF_W[f_by_id[a]["conf"]] + isc.CONF_W[f_by_id[b]["conf"]]) / 2.0
                shared = ((f_ents[a] & set(media[ma]["entities"]))
                          | (f_ents[b] & set(media[mb]["entities"])))
                xd = isc.xdom_of(dom_of, dom_ents, a, b)
                rows.append({
                    "a": a, "b": b, "ma": ma, "mb": mb,
                    "executor": sorted(ex), "orth": orth,
                    "score": conf * (ORTH_BONUS if orth else 1.0) * isc.xdom_factor(xd),
                    "conf": conf, "shared": sorted(shared), "xdom": xd,
                })
    rows.sort(key=lambda r: (-r["score"], r["a"], r["b"]))
    return rows, f_media, f_by_id


def brief(f, width=46):
    t = f["text"]
    return t if len(t) <= width else t[:width] + "…"


def r3_tension(findings, lex, aidx):
    """R3 张力检测器 · 同主体反向分叉。

    同一主体 S 同时以 +1 促进 o1、以 -1 抑制 o2（o1≠o2），提示「S 受两个相反
    下游牵制，可能存在开关」。设计文档 R3 ② 原要求「两客体同 role」，但本库
    实体词典稀疏、role 常落到 other；故先收紧为「两客体都能归一为词典内已知
    实体」，命中即报。R3 ① 条件依赖张力需 condition 标注，本库暂无，不实现。
    """
    edges = build_edges(findings, aidx)
    by_subj = {}
    for e in edges:
        by_subj.setdefault(e["s"], []).append(e)
    rows = []
    for s, es in by_subj.items():
        for e1, e2 in itertools.combinations(es, 2):
            p1, p2 = isc.PREDICATES[e1["p"]]["pol"], isc.PREDICATES[e2["p"]]["pol"]
            if p1 == 0 or p2 == 0 or p1 == p2 or e1["o"] == e2["o"]:
                continue
            r1, r2 = isc.role_of(lex, e1["o"]), isc.role_of(lex, e2["o"])
            if r1 == "other" or r2 == "other":
                continue
            rows.append({"s": s, "o1": e1["o"], "o2": e2["o"], "e1": e1, "e2": e2,
                         "r1": r1, "r2": r2, "same_role": r1 == r2})
    rows.sort(key=lambda r: (not r["same_role"], r["s"]))
    return rows


def r2_gap(findings, lex, aidx, mats, min_mention=2, min_jump=3):
    """R2 缺口检测器。

    ① 层级跳跃：一条边两端 role 跨 ≥min_jump 级（默认 3，即跳过 ≥2 个中间层级，
       如 molecular → disease 直连，中间缺 process/cellular/tissue）→ 报「中间缺
       中介」。收紧三点：
         · 层级差 ≥3（分子直连细胞是常规机制声明，不算缺口）；
         · 谓词须为机制型 CAUSAL_PREDS（标签/治疗型谓词不算「谁把两端接起来」）；
         · 两端都须是跨文献实体（各出现在 ≥2 篇结论的三元组里）。单篇论文自己给出
           的直连（如 F058 吲哚→空肠弯曲菌已在该篇讲清呼吸抑制）是同篇自给，不是
           缺口——R2 要找的是「库里分属不同论文、却缺一条中介边」的跨文献缺口。
    ③ 孤立枢纽：某实体在边图上度=1，但正文被 ≥min_mention 条结论提及，且
       type=process → 提示「该过程可能被系统性低估，值得补检」。
    ② 时间尺度错配需 condition 标注，本库暂无，不实现（同 R3 ①）。
    """
    edges = build_edges(findings, aidx)

    ent_fids = {}
    for e in edges:
        for name in (e["s"], e["o"]):
            ent_fids.setdefault(name, set()).add(e["fid"])

    jumps = []
    for e in edges:
        if e["p"] not in CAUSAL_PREDS:
            continue
        if len(ent_fids[e["s"]]) < 2 or len(ent_fids[e["o"]]) < 2:
            continue
        rs, ro = isc.role_of(lex, e["s"]), isc.role_of(lex, e["o"])
        if rs not in LEVEL_RANK or ro not in LEVEL_RANK:
            continue
        i, j = LEVEL_RANK[rs], LEVEL_RANK[ro]
        if abs(i - j) < min_jump:
            continue
        lo, hi = min(i, j), max(i, j)
        jumps.append({"s": e["s"], "o": e["o"], "p": e["p"], "fid": e["fid"],
                      "rs": rs, "ro": ro, "missing": LEVELS[lo + 1:hi],
                      "s_fids": sorted(ent_fids[e["s"]]), "o_fids": sorted(ent_fids[e["o"]])})
    jumps.sort(key=lambda r: (-len(r["missing"]), r["fid"], r["s"]))

    edge_deg = {}
    for e in edges:
        for name in (e["s"], e["o"]):
            edge_deg[name] = edge_deg.get(name, 0) + 1
    mention = {}
    for f in findings:
        if not f["id"]:
            continue
        for canon, pat in mats:
            if pat.search(f["text"]):
                mention.setdefault(canon, set()).add(f["id"])
    hubs = []
    for name, deg in edge_deg.items():
        if deg != 1 or isc.role_of(lex, name) != "process":
            continue
        fids = mention.get(name, set())
        if len(fids) < min_mention:
            continue
        hubs.append({"ent": name, "fids": sorted(fids), "mention": len(fids)})
    hubs.sort(key=lambda h: (-h["mention"], h["ent"]))
    return jumps, hubs


def build_causal_graph(edges):
    """有向机制图：只保留机制型谓词（CAUSAL_PREDS）的边。返回 s → [edge]。"""
    adj = {}
    for e in edges:
        if e["p"] in CAUSAL_PREDS:
            adj.setdefault(e["s"], []).append(e)
    return adj


def count_bypass(adj, src, dst, banned, max_depth, cap=64):
    """数 src→dst 的路径数，绕开 banned 节点，深度 ≤ max_depth（含直连）。

    只数「替代路径」：任何经过 banned 的路径都不计，故返回值即 paths_bypass。
    迭代 DFS + 访问集防环，cap 防止稠密图爆炸。
    """
    total = 0
    stack = [(src, 0, frozenset((src,)))]
    while stack:
        node, depth, seen = stack.pop()
        if depth >= max_depth:
            continue
        for e in adj.get(node, ()):
            o = e["o"]
            if o == banned or o in seen:
                continue
            if o == dst:
                total += 1
                if total >= cap:
                    return total
                continue
            stack.append((o, depth + 1, seen | {o}))
    return total


def r4_bottleneck(findings, lex, aidx, max_depth=4, min_mentions=2):
    """R4 反事实 / 瓶颈分析。

    对每个中间节点 M（同时有机制型入边与出边），枚举它中介的跨文献对 (X, Y)
    （X→M 与 M→Y 来自不同论文），数「绕开 M 的替代路径数」：
      bypass == 0 → M 是【瓶颈】：X→Y 必经 M，去掉 M 链断，候选干预靶点；
      bypass > 0  → M 是【冗余】：存在备份路径，候选冗余/耐药机制。

    四道闸门（沿用 R2 精神，防稀疏图上的平凡直连）：
      · 仅机制型谓词（CAUSAL_PREDS）——瓶颈问的是「因果必经」，标签/治疗边不算；
      · 两条边来自不同 F——同篇自给的 X→M→Y 不是跨文献链，无发现价值；
      · M 须是机制节点（role 在层级轴上，非 intervention/other）——瓶颈要找的是
        可干预的机制枢纽，而不是「化疗」这类本身就是干预的节点；
      · 终点 Y 须跨文献（出现在 ≥min_mentions 篇结论的三元组里）——Y 是链要抵达的
        表型/疾病，须是库内被反复锚定的实体，滤掉只在一篇里出现的单篇噪声端点。
    """
    edges = build_edges(findings, aidx)
    adj = build_causal_graph(edges)
    indeg, outdeg = {}, {}
    for e in edges:
        if e["p"] not in CAUSAL_PREDS:
            continue
        outdeg.setdefault(e["s"], []).append(e)
        indeg.setdefault(e["o"], []).append(e)
    ent_fids = {}
    for e in edges:
        for name in (e["s"], e["o"]):
            ent_fids.setdefault(name, set()).add(e["fid"])

    rows = []
    for m in sorted(set(indeg) & set(outdeg)):
        if isc.role_of(lex, m) not in LEVEL_RANK:
            continue
        for ein in indeg[m]:
            for eout in outdeg[m]:
                x, y = ein["s"], eout["o"]
                if x == y or x == m or y == m:
                    continue
                if ein["fid"] == eout["fid"]:
                    continue
                if len(ent_fids.get(y, ())) < min_mentions:
                    continue
                byp = count_bypass(adj, x, y, m, max_depth)
                rows.append({"m": m, "x": x, "y": y, "byp": byp,
                             "bottleneck": byp == 0,
                             "f_in": ein["fid"], "f_out": eout["fid"],
                             "p_in": ein["p"], "p_out": eout["p"],
                             "r_m": isc.role_of(lex, m)})
    rows.sort(key=lambda r: (not r["bottleneck"], -r["byp"], r["m"], r["x"], r["y"]))
    return rows


def causal_chains(findings, lex, aidx):
    """枚举两跳机制链 X -p1→ M -p2→ Y，供 R5 抽象成模式。

    条件：两条边均为机制型谓词（CAUSAL_PREDS）、来自不同 F（跨文献）、
    中间节点 M 为机制节点（role 在层级轴上）、三节点互不相同。
    """
    edges = build_edges(findings, aidx)
    adj = build_causal_graph(edges)
    chains = []
    for e1 in edges:
        if e1["p"] not in CAUSAL_PREDS:
            continue
        x, m = e1["s"], e1["o"]
        if isc.role_of(lex, m) not in LEVEL_RANK:
            continue
        for e2 in adj.get(m, ()):
            y = e2["o"]
            if e2["fid"] == e1["fid"] or y == m or y == x or x == m:
                continue
            chains.append({
                "x": x, "m": m, "y": y, "p1": e1["p"], "p2": e2["p"],
                "rx": isc.role_of(lex, x), "rm": isc.role_of(lex, m), "ry": isc.role_of(lex, y),
                "f1": e1["fid"], "f2": e2["fid"],
            })
    return chains


def r5_analogy(findings, lex, aidx, require_same_role=True):
    """R5 跨层级同构对齐。

    把每条两跳链抽象成 pattern（抹掉具体实体）：require_same_role=True 时用
    (p1, p2, roleX, roleM, roleY)，否则只用谓词序列 (p1, p2)。同 pattern 且两端
    实体不相交的两条链 → 跨系统迁移候选：「同一机制逻辑在不同系统里复现」。

    这类最易产噪音，输出只当候选池，**必须**过「生成-批判」闭环与人工复核才可入库。
    """
    chains = causal_chains(findings, lex, aidx)
    mats = isc.compile_matchers(lex)
    dom_of, dom_ents = isc.domain_layer(findings, f_entity_sets(findings, aidx, mats))
    groups = {}
    for c in chains:
        key = ((c["p1"], c["p2"], c["rx"], c["rm"], c["ry"]) if require_same_role
               else (c["p1"], c["p2"]))
        groups.setdefault(key, []).append(c)
    rows = []
    for key, cs in groups.items():
        if len(cs) < 2:
            continue
        for c1, c2 in itertools.combinations(cs, 2):
            s1 = {c1["x"], c1["m"], c1["y"]}
            s2 = {c2["x"], c2["m"], c2["y"]}
            if s1 & s2:
                continue
            xd = isc.xdom_of(dom_of, dom_ents, c1["f1"], c2["f1"])
            rows.append({"c1": c1, "c2": c2, "pattern": key, "xdom": xd})
    rows.sort(key=lambda r: (-r["xdom"], r["pattern"], r["c1"]["m"], r["c2"]["m"]))
    return rows, chains, groups


def _conflict_index(edges):
    """(s, o) → 涉及 F：库内方向相反的谓词冲突索引，供批判环检索反例。"""
    grp = {}
    for e in edges:
        grp.setdefault((e["s"], e["o"]), []).append(e)
    out = {}
    for k, lst in grp.items():
        pols = {isc.PREDICATES[e["p"]]["pol"] for e in lst}
        if +1 in pols and -1 in pols:
            out[k] = sorted({e["fid"] for e in lst})
    return out


def critique_candidate(cand, edges, tensions, adj, max_depth=4):
    """生成-批判闭环 · 对一条候选跑自我反驳环。

    三类攻击（对应 SKILL.md 第六节「找反例 / 找边界条件 / 找什么情况下会崩」）：
      反例     —— 库内与候选实体相关的谓词冲突（方向相反），候选所依赖的方向可能不成立；
      边界条件 —— R3 张力涉及候选实体，联系可能是条件依赖，须指明条件变量；
      断链点   —— 候选链的中介 M 可被绕行（bypass>0），联系非必经。
    severity 2=高危 / 1=需人工；空攻击不代表成立，只代表自动环没抓到把柄。
    """
    attacks = []
    ents = set(cand["ents"])
    for (s, o), fids in sorted(_conflict_index(edges).items()):
        if s in ents or o in ents:
            attacks.append({"kind": "反例", "severity": 2,
                            "detail": f"{s}→{o} 库内方向相反（{'、'.join(fids)}）——候选依赖的方向可能不成立"})
    for t in tensions:
        if t["s"] in ents:
            attacks.append({"kind": "边界条件", "severity": 1,
                            "detail": f"{t['s']} 同时促进 {t['o1']}、抑制 {t['o2']}——联系可能是条件依赖，须指明条件变量"})
    ch = cand.get("chain")
    if ch:
        byp = count_bypass(adj, ch["x"], ch["y"], ch["m"], max_depth)
        if byp > 0:
            attacks.append({"kind": "断链点", "severity": 1,
                            "detail": f"绕开 {ch['m']} 另有 {byp} 条路径——{ch['m']} 非必经，联系可被旁路"})
    return attacks


def build_candidates(rows, jumps, btl, analogy, media):
    """把流水线候选池（R1 桥接 + R2 缺口 + R4 瓶颈 + R5 同构）统一成批判环输入。

    批判环要覆盖「可能成为 C 的候选」，故不只桥接——R2/R4 的候选池同样要过自我反驳。
    """
    cands = []
    for r in rows:
        if not r["orth"]:
            continue
        cands.append({"kind": "R1 桥接", "label": f"{r['a']} × {r['b']}",
                      "refs": [r["a"], r["b"]],
                      "ents": sorted(set(r["shared"]) | {media[r["ma"]]["zh"], media[r["mb"]]["zh"]}),
                      "chain": None})
    for j in jumps:
        cands.append({"kind": "R2 缺口", "label": f"{j['s']} ↔ {j['o']}",
                      "refs": [j["fid"]], "ents": [j["s"], j["o"]], "chain": None})
    for r in btl:
        cands.append({"kind": "R4 瓶颈", "label": f"{r['x']}→{r['m']}→{r['y']}",
                      "refs": [r["f_in"], r["f_out"]], "ents": [r["x"], r["m"], r["y"]],
                      "chain": {"x": r["x"], "m": r["m"], "y": r["y"]}})
    for a in analogy:
        c1, c2 = a["c1"], a["c2"]
        cands.append({"kind": "R5 同构", "label": f"{c1['m']} ≅ {c2['m']}",
                      "refs": sorted({c1["f1"], c1["f2"], c2["f1"], c2["f2"]}),
                      "ents": [c1["x"], c1["m"], c1["y"], c2["x"], c2["m"], c2["y"]],
                      "chain": c1})
    return cands


def critique_loop(cands, edges, tensions, adj):
    """对候选池逐条跑批判环，给出裁决：高危 / 需人工 / 存活。"""
    out = []
    for c in cands:
        atk = critique_candidate(c, edges, tensions, adj)
        hi = any(a["severity"] == 2 for a in atk)
        mid = any(a["severity"] == 1 for a in atk)
        out.append({"cand": c, "attacks": atk,
                    "verdict": "高危" if hi else ("需人工" if mid else "存活")})
    return out


def render_analogy(analogy, top):
    L = ["── R5 跨层级同构（同模式、不同系统：迁移假说候选）" + "─" * 6]
    if not analogy:
        L.append("  （无）")
    for a in analogy[:top]:
        c1, c2 = a["c1"], a["c2"]
        p = a["pattern"]
        ptxt = f"{p[0]}·{p[1]}｜{p[2]}→{p[3]}→{p[4]}" if len(p) == 5 else f"{p[0]}·{p[1]}"
        L.append(f"  [模式 {ptxt}]｜跨域距离 {a['xdom']:.2f}")
        L.append(f"    A：{c1['x']} --{c1['p1']}→ {c1['m']} --{c1['p2']}→ {c1['y']}  ｜{c1['f1']}×{c1['f2']}")
        L.append(f"    B：{c2['x']} --{c2['p1']}→ {c2['m']} --{c2['p2']}→ {c2['y']}  ｜{c2['f1']}×{c2['f2']}")
        L.append(f"    迁移假说：{c1['m']} 与 {c2['m']} 是否共享同一类机制逻辑？A 的结论能否迁到 B 的系统？")
    return "\n".join(L)


def render_critique(results, top):
    L = ["── 生成-批判闭环（每条候选自动过自我反驳环：反例 / 边界条件 / 断链点）" + "─" * 2]
    if not results:
        L.append("  （无候选）")
    for r in results[:top]:
        c = r["cand"]
        L.append(f"  [{r['verdict']}] {c['kind']} {c['label']}（{'、'.join(c['refs'])}）")
        for a in r["attacks"]:
            L.append(f"      · {a['kind']}：{a['detail']}")
        if not r["attacks"]:
            L.append("      · 未发现自动攻击点（仍须人工过新颖性查重）")
    return "\n".join(L)


def propose_questions(rows, tensions, jumps, btl, existing, media):
    """R1 桥接 + R2 层级跳跃 + R3 张力 + R4 瓶颈/冗余 → Q 提案。

    触发 F 集与已有 Q（或本次已提案）重复则跳过——防同一 F 集反复生长多条 Q。
    孤立枢纽不入 Q：其输出是「该过程被低估、值得补检」，属元层面的标注提示，不满足
    Q 的三种问题型（为什么 / 什么连接了 / 什么条件下），单列为补检清单（render_hubs）。
    """
    seen = {frozenset(q["triggers"]) for q in existing}
    props, skipped = [], []

    def emit(src, trig, q, cand):
        key = frozenset(trig)
        if key in seen:
            skipped.append((src, sorted(trig)))
            return
        seen.add(key)
        props.append({"src": src, "triggers": sorted(trig), "q": q, "cand": cand})

    for r in rows:
        if not r["orth"]:
            continue
        ma, mb = media[r["ma"]]["zh"], media[r["mb"]]["zh"]
        emit("R1 介质桥接", [r["a"], r["b"]],
             f"什么把 {ma} 与 {mb} 连接起来（共同执行器 {'、'.join(r['executor'])}）？",
             f"{r['ma']} × {r['mb']}；正交旋钮；score {r['score']:.2f}")
    for j in jumps:
        emit("R2 缺口·层级跳跃", [j["fid"]],
             (f"{j['s']}（{j['rs']}）与 {j['o']}（{j['ro']}）分属不同论文却只被一条"
              f"直连边绑定，中间缺 {'/'.join(j['missing'])} 层级——什么把两端接起来？"),
             "")
    grp = {}
    for t in tensions:
        trig = frozenset({t["e1"]["fid"], t["e2"]["fid"]})
        key = (trig, t["s"])
        g = grp.setdefault(key, {"triggers": sorted(trig), "s": t["s"],
                                 "promote": set(), "inhibit": set()})
        for e in (t["e1"], t["e2"]):
            (g["promote"] if isc.PREDICATES[e["p"]]["pol"] > 0 else g["inhibit"]).add(e["o"])
    for (trig, s), g in grp.items():
        emit("R3 张力", sorted(trig),
             (f"{s} 为何一边促进 {'、'.join(sorted(g['promote']))}、"
              f"一边抑制 {'、'.join(sorted(g['inhibit']))}？是否存在共同的条件变量？"),
             "")
    for r in btl:
        trig = [r["f_in"], r["f_out"]]
        chain = f"{r['x']} --{r['p_in']}→ {r['m']} --{r['p_out']}→ {r['y']}"
        if r["bottleneck"]:
            emit("R4 瓶颈", trig,
                 f"{r['x']} 对 {r['y']} 的作用是否必经 {r['m']}？去掉 {r['m']} 后这条跨文献链是否断裂？",
                 f"{chain}；绕开 {r['m']} 的替代路径 0 条 → 候选干预靶点（D3）")
        else:
            emit("R4 冗余", trig,
                 f"{r['x']} 到 {r['y']} 除经 {r['m']} 外另有 {r['byp']} 条替代路径，两条路径在什么条件下切换？",
                 f"经 {r['m']} 的链 + {r['byp']} 条备份路径 → 候选冗余/耐药机制")
    return props, skipped


def render_questions(props, skipped, top):
    L = ["=" * 68, "机制推理引擎 · Q 问题库提案（R1 桥接 + R2 缺口 + R3 张力 + R4 瓶颈）", "=" * 68, ""]
    if not props:
        L.append("  （无新 Q 提案）")
    for p in props[:top]:
        L.append(f"[{p['src']}] 触发：{'、'.join(p['triggers'])}")
        L.append(f"    问题：{p['q']}")
        if p["cand"]:
            L.append(f"    候选解释：{p['cand']}")
        L.append("")
    for src, trig in skipped:
        L.append(f"  [跳过] {src} {'、'.join(trig)}：已有同触发 Q")
    return "\n".join(L)


def render_hubs(hubs, top):
    L = ["── R2 补检清单 · 孤立枢纽（被多条结论提及、却只在 1 条边里出现的过程）" + "─" * 4]
    if not hubs:
        L.append("  （无）")
    for h in hubs[:top]:
        L.append(f"  「{h['ent']}」（{h['mention']} 条结论提及）：{'、'.join(h['fids'])}")
    return "\n".join(L)


def render_bottleneck(btl, top):
    L = ["── R4 瓶颈/冗余（跨文献链的中间节点：必经 = 瓶颈，有备份 = 冗余）" + "─" * 4]
    if not btl:
        L.append("  （无）")
    for r in btl[:top]:
        kind = "瓶颈" if r["bottleneck"] else f"冗余(绕行{r['byp']})"
        L.append(f"  [{kind}] {r['x']} --{r['p_in']}→ {r['m']}（{r['r_m']}）--{r['p_out']}→ {r['y']}"
                 f"  ｜ {r['f_in']}×{r['f_out']}")
    return "\n".join(L)


def q_lines(props, date):
    out = []
    for p in props:
        out.append(f"- **[]**（{date}｜open｜触发：{'、'.join(p['triggers'])}）{p['q']}")
        if p["cand"]:
            out.append(f"  - 候选解释：{p['cand']}")
    return out


def render(rows, f_by_id, media, dom_of, top):
    L = ["=" * 68, "机制推理引擎 · R1 介质桥接器（reason_engine P1）", "=" * 68, ""]
    if not rows:
        L.append("  （无桥接候选）")
        return "\n".join(L)
    L.append(f"桥接候选（同执行器）：{len(rows)} 对｜★ = 正交旋钮（硬联系，score ×{ORTH_BONUS}）"
             f"｜跨域距离放大 ×(1+{isc.XDOM_BONUS}·xdom)")
    L.append("")
    for r in rows[:top]:
        star = "★" if r["orth"] else " "
        mzh = f'{media[r["ma"]]["zh"]} × {media[r["mb"]]["zh"]}'
        da, db = dom_of.get(r["a"], "?"), dom_of.get(r["b"], "?")
        L.append(f"[{r['score']:4.2f}]{star} {r['a']} × {r['b']} ｜ 介质：{mzh}")
        L.append(f"        共同执行器：{'、'.join(r['executor'])} ｜ 正交：{'是' if r['orth'] else '否'}"
                 f" ｜ 命中实体：{'、'.join(r['shared'])}")
        L.append(f"        跨域距离 {r['xdom']:.2f}｜{da} ⇄ {db}")
        L.append(f"        {r['a']}（{f_by_id[r['a']]['conf']}）{brief(f_by_id[r['a']])}")
        L.append(f"        {r['b']}（{f_by_id[r['b']]['conf']}）{brief(f_by_id[r['b']])}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="机制推理引擎 R1（介质桥接器）+ R3（张力）")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--write", default=None)
    ap.add_argument("--questions", action="store_true", help="额外跑 R3 张力并提案 Q 条目")
    ap.add_argument("--q-out", default=None, help="把 Q 提案（含可直接粘贴的条目行）落盘")
    ap.add_argument("--analogy", action="store_true", help="跑 R5 跨层级同构对齐")
    ap.add_argument("--critique", action="store_true", help="跑生成-批判闭环（R1/R5 候选过自我反驳环）")
    ap.add_argument("--c-out", default=None, help="把同构候选 + 批判结果落盘")
    args = ap.parse_args()

    for p in (isc.FINDINGS, isc.ENTITIES, MEDIA):
        if not os.path.isfile(p):
            print(f"[F] 读不到 {p}")
            return 2

    text = isc.read_text(isc.FINDINGS)
    findings, _links = isc.parse_findings(text)
    lex = isc.load_lexicon(isc.ENTITIES)
    media = load_media(MEDIA)
    aidx = isc.build_alias_index(lex)
    mats = isc.compile_matchers(lex)

    rows, f_media, f_by_id = r1_bridge(findings, media, aidx, mats)
    dom_of, _dom_ents = isc.domain_layer(findings, f_entity_sets(findings, aidx, mats))

    header = (f"库：{len(findings)} 条结论｜介质词典 {len(media)} 条｜"
              f"关联到介质的结论 {len(f_media)} 条｜桥接候选 {len(rows)} 对")
    body = render(rows, f_by_id, media, dom_of, args.top)

    print(header)
    print(body)

    if args.write:
        d = os.path.dirname(args.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with io.open(args.write, "w", encoding="utf-8") as fh:
            fh.write("# 介质桥接候选（reason_engine R1 自动生成）\n\n")
            fh.write(header + "\n\n")
            fh.write("> 候选 ≠ 结论。桥接候选仍需过三铁律与自我反驳，才可能成为 C 条目。\n\n")
            fh.write("```\n" + body + "\n```\n")
        print(f"已落盘：{args.write}")

    if args.questions or args.q_out:
        tensions = r3_tension(findings, lex, aidx)
        jumps, hubs = r2_gap(findings, lex, aidx, mats)
        btl = r4_bottleneck(findings, lex, aidx)
        existing = isc.parse_questions(text)
        props, skipped = propose_questions(rows, tensions, jumps, btl, existing, media)
        qbody = render_questions(props, skipped, args.top)
        print("")
        print(qbody)
        print("")
        print(render_bottleneck(btl, args.top))
        print("")
        print(render_hubs(hubs, args.top))
        print("")
        n_bot = sum(1 for r in btl if r["bottleneck"])
        print(f"层级跳跃 {len(jumps)}｜孤立枢纽 {len(hubs)}｜张力对 {len(tensions)}｜"
              f"瓶颈/冗余 {len(btl)}（瓶颈 {n_bot}）｜"
              f"已有 Q {len(existing)} 条｜新 Q 提案 {len(props)} 条｜跳过（重复）{len(skipped)} 条")
        if args.q_out:
            d = os.path.dirname(args.q_out)
            if d:
                os.makedirs(d, exist_ok=True)
            with io.open(args.q_out, "w", encoding="utf-8") as fh:
                fh.write("# Q 问题库提案（reason_engine R1 + R2 + R3 + R4 自动生成）\n\n")
                fh.write("> 提案 ≠ 入库。人工润色问题措辞后，用 memory.py add --type question 落库。\n\n")
                fh.write("```\n" + qbody + "\n```\n\n")
                fh.write("## 可直接粘贴的条目行\n\n")
                fh.write("\n".join(q_lines(props, __import__("datetime").date.today().isoformat())))
                fh.write("\n")
            print(f"已落盘：{args.q_out}")

    if args.analogy or args.critique or args.c_out:
        analogy, chains, groups = r5_analogy(findings, lex, aidx)
        edges = build_edges(findings, aidx)
        adj = build_causal_graph(edges)
        n_groups = sum(1 for v in groups.values() if len(v) >= 2)
        print("")
        print(render_analogy(analogy, args.top))
        print("")
        print(f"两跳机制链 {len(chains)} 条｜同模式组 {n_groups} 组｜同构候选 {len(analogy)} 对")
        cbody = ""
        if args.critique:
            tensions = r3_tension(findings, lex, aidx)
            jumps, _hubs = r2_gap(findings, lex, aidx, mats)
            btl = r4_bottleneck(findings, lex, aidx)
            cands = build_candidates(rows, jumps, btl, analogy, media)
            results = critique_loop(cands, edges, tensions, adj)
            print("")
            print(render_critique(results, args.top))
            n_hi = sum(1 for r in results if r["verdict"] == "高危")
            n_mid = sum(1 for r in results if r["verdict"] == "需人工")
            n_ok = sum(1 for r in results if r["verdict"] == "存活")
            print("")
            print(f"批判结果：候选 {len(results)} 条｜高危 {n_hi}｜需人工 {n_mid}｜存活 {n_ok}")
            cbody = render_critique(results, args.top)
        if args.c_out:
            d = os.path.dirname(args.c_out)
            if d:
                os.makedirs(d, exist_ok=True)
            with io.open(args.c_out, "w", encoding="utf-8") as fh:
                fh.write("# R5 跨层级同构 + 生成-批判闭环（reason_engine 自动生成）\n\n")
                fh.write("> 候选 ≠ 结论。同构候选须过批判环 + 人工复核；批判「存活」只代表自动环没抓到把柄。\n\n")
                fh.write("```\n" + render_analogy(analogy, args.top) + "\n```\n")
                if cbody:
                    fh.write("\n```\n" + cbody + "\n```\n")
            print(f"已落盘：{args.c_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())