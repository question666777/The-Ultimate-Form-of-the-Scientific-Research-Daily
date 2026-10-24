#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""insight_scan.py — 洞见候选扫描器（insight-synthesis 技能的执行层）

背景：跨文献联系长期停滞（109 条结论只连出 3 条 C）。v1 只做「提及共现」，
看到「两条都提到 STING」，看不到「一条在降解它、另一条在让它沉默」。
v2 在提及层之上加一层**类型化三元组图谱**（findings.md 每条结论下的
「  - 三元组：(主体, 谓词, 客体)」），于是：
  · 谓词带极性 → 沿路径做符号运算，给出连接的**方向**（促进 / 抑制 / 未知）
  · 实体带 type → 按 metapath（分子→过程→表型）给因果链加分、工具链降权
  · 同主客体、极性相反 → 直接判为**谓词冲突**，比标签对冲精确得多

四类候选：
  A 直接共现 —— 两条结论共享实体（提及层，召回用）
  B 二跳链 —— 三元组 o1==s2 串成 s1→M→o2 的 ABC 链（Swanson open discovery），带方向
  C 标签对冲 —— 同实体上一条 #干预、另一条 #安全信号
  D 谓词冲突 —— 同一 (主体, 客体) 上极性相反（精准，优先看）

跨领域距离层（第五类 E）：
  同一领域内的联系多是该领域专家的常识，跨领域越远越可能未被熟知。故给每条结论
  按其报告主题归入领域，算「跨域距离 xdom = 1 − inter/(inter+K)」（inter = 两领域
  在库内共享的实体数），用它放大 A/B/D 的分数；另单列「E 跨领域远链」（xdom ≥ 门槛，
  按总分排序）——这是引擎专门为「跳出成熟领域内连线」设的一层，产出候选再交
  novelty_gate 做抢先核验。共享泛化枢纽的候选由度惩罚压住，不会仅因跨域就冒头。

打分（借 Open Targets 乘式 × DWPC 度惩罚，取其思想不取其重器）：
  A: Σ 形态权重 / 度(E)^ALPHA × 平均置信度 × xdom 放大
  B: Σ PRED_W(p1)·PRED_W(p2)·metapath_bonus / 度(M)^ALPHA × 平均置信度 × xdom 放大
  D: 固定高权重 × 平均置信度 × xdom 放大
  形态权重：矛盾 2.0 / 机制-干预接口 1.5 / 同族 1.0；置信度权重：高 3 / 中 2 / 低 1

候选 ≠ 结论。候选要过 insight-synthesis 的新颖性查重与自我反驳环，才可能成为 C 条目。

用法：
  python tools\\insight_scan.py
  python tools\\insight_scan.py --top 15 --min-degree 2
  python tools\\insight_scan.py --write learning\\runs\\insight_candidates.md

只用标准库。
"""

import argparse
import io
import itertools
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FINDINGS = os.path.join(ROOT, "learning", "memory", "findings.md")
ENTITIES = os.path.join(ROOT, "tools", "entities.json")

F_RE = re.compile(r"^- \*\*\[(F\d{3})?\]\*\*（(\d{4}-\d{2}-\d{2})｜([^｜]+)｜(高|中|低)｜([^）]*)）(.+)$")
C_RE = re.compile(r"^- \*\*\[(C\d{3})?\]\*\*（([^）]+)）(.+)$")
Q_RE = re.compile(r"^- \*\*\[(Q\d{3})?\]\*\*（(\d{4}-\d{2}-\d{2})｜(open|explained|falsified)｜触发：([^）]+)）(.+)$")
CAND_RE = re.compile(r"^\s+- 候选解释：(.*)$")
TRIPLE_RE = re.compile(r"^\s+- 三元组：(.+)$")
TRIPLE_ITEM = re.compile(r"^\((.+?),\s*([A-Z_]+),\s*(.+?)\)$")

CONF_W = {"高": 3, "中": 2, "低": 1}
ALPHA = 1.0
W_CONTRADICT = 2.0
W_INTERFACE = 1.5
W_FAMILY = 1.0
W_CONFLICT = 3.0

# 跨领域距离层（第五类候选 E）。跨域距离 xdom = 1 − inter/(inter+XDOM_K)，
# inter = 两领域在库内共享的实体数；同域 = 0。两领域共享实体越少 → 距离越接近 1
# （该领域对在库内越少被打通 = 这条联系越不像两个领域的常识交集）。
# 按 xdom 放大候选分：同域 ×1，最远 ×(1+XDOM_BONUS)。实体本身的泛化程度由度惩罚
# （÷度^ALPHA）负责，故共享泛化枢纽（如「病毒」）的候选仍被压住。
XDOM_BONUS = 1.0
XDOM_K = 4.0        # 距离饱和尺度：共享 4 个实体 → 距离 0.5
XDOM_MIN = 0.5      # 「跨领域远链」章节的展示门槛（共享 ≤4 个实体）

# 领域归一：报告章节主题随期演进，近义主题须在此收敛为同一规范领域名
DOMAIN_ALIASES = {
    "病原体变异与新发传染病监测": "病原体变异与传染病监测",
}

# 谓词闭集：pol 极性（+1 促进 / -1 抑制 / 0 未知），w 机制信号权重，label 中文
PREDICATES = {
    "ACTIVATES":  {"pol": +1, "w": 1.0, "label": "激活/促进"},
    "INHIBITS":   {"pol": -1, "w": 1.0, "label": "抑制/阻断"},
    "CAUSES":     {"pol": +1, "w": 1.0, "label": "导致/驱动"},
    "REGULATES":  {"pol": 0,  "w": 0.7, "label": "调控"},
    "ASSOCIATED": {"pol": 0,  "w": 0.4, "label": "关联"},
    "TREATS":     {"pol": +1, "w": 0.8, "label": "治疗/改善"},
    "INDICATES":  {"pol": 0,  "w": 0.5, "label": "标志/诊断"},
    "TARGETS":    {"pol": 0,  "w": 0.8, "label": "靶向"},
    "PART_OF":    {"pol": 0,  "w": 0.3, "label": "组成/属于"},
    "USED_FOR":   {"pol": 0,  "w": 0.3, "label": "用于"},
    "HOMOLOG":    {"pol": 0,  "w": 0.6, "label": "跨物种同源"},
}

# 实体 type → 角色（用于 metapath 判因果链是否合理）
ROLE = {
    "protein": "molecular", "gene": "molecular", "rna": "molecular", "molecule": "molecular",
    "metabolite": "molecular", "cytokine": "molecular", "receptor": "molecular",
    "antigen": "molecular", "rna_feature": "molecular",
    "process": "process", "phenotype": "process", "biomarker": "process",
    "cell": "cellular", "organelle": "cellular",
    "organ": "tissue", "region": "tissue", "microenvironment": "tissue",
    "disease": "disease", "organism": "organism",
    "intervention": "intervention", "drug": "intervention", "method": "intervention", "factor": "intervention",
}


def read_text(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


# 硬度判定：只有「分子实体/通路」且不是泛词（低度数）才算机制硬桥接。
# 疾病名、表征词、方法/药物类共享只是背景共现，不能当跨文献联系（2026-10-21 修订）。
HARD_ROLES = {"molecular"}
HARD_TYPES = {"pathway"}
HARD_DEG_MAX = 4  # 度≥5 多为 DNA/代谢这类泛词，算不得硬联系


def is_hard_entity(lex, e, degree):
    info = lex.get(e, {})
    if info.get("type") in HARD_TYPES:
        return degree.get(e, 0) <= HARD_DEG_MAX
    if ROLE.get(info.get("type", ""), "other") in HARD_ROLES:
        return degree.get(e, 0) <= HARD_DEG_MAX
    return False


def parse_findings(text):
    """返回 (findings, links)；findings 每条带 triples=[{s,p,o}]。"""
    findings, links = [], []
    last_f = None
    for line in text.split("\n"):
        line = line.rstrip()
        m = TRIPLE_RE.match(line)
        if m:
            if last_f is not None:
                for part in m.group(1).split("；"):
                    part = part.strip()
                    tm = TRIPLE_ITEM.match(part)
                    if tm:
                        last_f["triples"].append({"s": tm.group(1).strip(),
                                                  "p": tm.group(2).strip(),
                                                  "o": tm.group(3).strip()})
            continue
        if Q_RE.match(line):
            last_f = None
            continue
        m = F_RE.match(line)
        if m:
            fid, date, topic, conf, tags, body = m.groups()
            body = re.sub(r"\s*→\s*", " → ", body)
            if " → 证据：" not in body:
                last_f = None
                continue
            concl, ev = body.split(" → 证据：", 1)
            f = {"id": fid or "", "date": date, "topic": topic.strip(), "conf": conf,
                 "tags": set(re.findall(r"#[^\s#]+", tags)), "text": concl.strip(),
                 "doi": ev.strip(), "triples": []}
            findings.append(f)
            last_f = f
            continue
        m = C_RE.match(line)
        if m:
            cid, head, body = m.groups()
            body = re.sub(r"\s*→\s*", " → ", body)
            if " → 依据：" not in body:
                continue
            refs = re.findall(r"F\d{3}", body.split(" → 依据：", 1)[1])
            links.append({"id": cid or "", "head": head.strip(), "refs": refs})
    return findings, links


def parse_questions(text):
    """解析「三、问题」区的 Q 条目，供 reason_engine 去重与触发。"""
    qs = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = Q_RE.match(line.rstrip())
        if not m:
            continue
        qid, date, status, trig, q = m.groups()
        cand = ""
        if i + 1 < len(lines):
            cm = CAND_RE.match(lines[i + 1].rstrip())
            if cm:
                cand = cm.group(1).strip()
        qs.append({"id": qid or "", "date": date, "status": status,
                   "triggers": re.findall(r"F\d{3}", trig), "q": q.strip(), "cand": cand})
    return qs


def load_lexicon(path):
    data = json.load(io.open(path, encoding="utf-8"))
    return data.get("entities", {})


def compile_matchers(lex):
    """返回 [(canon, compiled_pattern)]；全 ASCII 别名走词边界，含中文别名走子串。"""
    mats = []
    for canon, info in lex.items():
        for a in (set(info.get("aliases", [])) | {canon}):
            a = a.strip()
            if not a:
                continue
            if re.search(r"[\u4e00-\u9fff]", a):
                pat = re.compile(re.escape(a))
            else:
                pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])", re.I)
            mats.append((canon, pat))
    return mats


def _norm_key(s):
    """字面变体归一：去连字符/空格/下划线/斜杠 + 小写，捕获 TDP43↔TDP-43、CD8+T↔CD8+T、p53↔P53。"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", (s or "").lower())


def build_alias_index(lex):
    """别名/规范名 → 规范名（三元组里可能写别名，如 RICTOR，必须归一，否则边挂不上）。
    除精确键与 lower 键外，再加规范键（_norm_key，2026-10-17 新增）：TDP43 与 TDP-43、
    CD8+T 与 CD8+T 这类字面变体挂到同一规范名，减少「未入词典」的漏识别。"""
    idx = {}
    for canon, info in lex.items():
        idx[canon] = canon
        idx.setdefault(canon.lower(), canon)
        idx.setdefault(_norm_key(canon), canon)
        for a in info.get("aliases", []):
            a = a.strip()
            if a:
                idx[a] = canon
                idx.setdefault(a.lower(), canon)
                idx.setdefault(_norm_key(a), canon)
    return idx


def resolve(aidx, name):
    return aidx.get(name) or aidx.get(name.lower()) or aidx.get(_norm_key(name))


def role_of(lex, e):
    return ROLE.get(lex.get(e, {}).get("type", ""), "other")


def metapath_bonus(ra, rm, rc):
    """因果链合理性加分：分子→过程/细胞→表型/疾病 最像机制；工具/干预为端点则降权。"""
    if ra == "intervention" or rc == "intervention":
        return 0.6
    if ra == "molecular" and rm in ("process", "cellular") and rc in ("process", "disease", "cellular", "phenotype"):
        return 1.5
    if ra in ("molecular", "cellular") and rm == "process" and rc in ("disease", "process", "phenotype", "cellular"):
        return 1.5
    if ra == "organism" and rm in ("cellular", "process"):
        return 1.2
    return 1.0


def canon_domain(topic):
    """报告章节主题 → 规范领域名（近义主题合并，见 DOMAIN_ALIASES）。"""
    t = topic.strip()
    return DOMAIN_ALIASES.get(t, t)


def domain_layer(findings, f_to_ent):
    """领域层：F → 领域、领域 → 实体集（供跨域距离计算）。"""
    dom_of = {f["id"]: canon_domain(f["topic"]) for f in findings if f["id"]}
    dom_ents = {}
    for fid, ents in f_to_ent.items():
        d = dom_of.get(fid)
        if d:
            dom_ents.setdefault(d, set()).update(ents)
    return dom_of, dom_ents


def xdom_of(dom_of, dom_ents, a, b):
    """两条结论的跨域距离 = 1 − inter/(inter+XDOM_K)，inter = 两领域共享实体数。

    同域或领域缺失 = 0。inter 越大（两领域在库内越常被打通）距离越小。
    """
    da, db = dom_of.get(a), dom_of.get(b)
    if not da or not db or da == db:
        return 0.0
    inter = len(dom_ents.get(da, set()) & dom_ents.get(db, set()))
    return 1.0 - inter / (inter + XDOM_K)


def xdom_factor(d):
    """跨域距离 → 分数放大系数：同域 ×1，最远 ×(1+XDOM_BONUS)。"""
    return 1.0 + XDOM_BONUS * d


def sign_label(pols):
    net = 1
    for p in pols:
        if p == 0:
            return "未知"
        net *= p
    return "促进" if net > 0 else "抑制"


def tag_morphology(tags_a, tags_b):
    if (("#干预" in tags_a and "#安全信号" in tags_b) or
            ("#安全信号" in tags_a and "#干预" in tags_b)):
        return W_CONTRADICT
    if (("#机制" in tags_a and "#干预" in tags_b) or
            ("#干预" in tags_a and "#机制" in tags_b)):
        return W_INTERFACE
    return W_FAMILY


def scan(findings, lex, min_degree, xdom_min=XDOM_MIN):
    mats = compile_matchers(lex)
    f_by_id = {f["id"]: f for f in findings if f["id"]}

    # ── 提及层：结论-实体二部图
    f_to_ent = {f["id"]: {c for c, p in mats if p.search(f["text"])} for f in findings if f["id"]}
    ent_to_f = {}
    for fid, ents in f_to_ent.items():
        for e in ents:
            ent_to_f.setdefault(e, []).append(fid)
    for e in ent_to_f:
        ent_to_f[e] = sorted(ent_to_f[e])
    degree = {e: len(v) for e, v in ent_to_f.items()}

    # ── 领域层：跨领域距离（近义主题归一 → 领域实体集 → Jaccard 距离）
    dom_of, dom_ents = domain_layer(findings, f_to_ent)

    # ── 类型层：三元组边（别名归一为规范名）
    aidx = build_alias_index(lex)
    edges = []  # (s, p, o, fid)
    unknown_pred, unknown_ent = set(), set()
    for f in findings:
        for t in f["triples"]:
            if t["p"] not in PREDICATES:
                unknown_pred.add(t["p"])
                continue
            s, o = resolve(aidx, t["s"]), resolve(aidx, t["o"])
            if s is None:
                unknown_ent.add(t["s"])
                s = t["s"]
            if o is None:
                unknown_ent.add(t["o"])
                o = t["o"]
            edges.append((s, t["p"], o, f["id"]))

    # A. 直接共现
    direct = {}
    for e, fids in ent_to_f.items():
        if degree[e] < min_degree:
            continue
        for a, b in itertools.combinations(fids, 2):
            direct.setdefault((a, b), []).append(e)
    direct_rows = []
    for (a, b), shared in direct.items():
        fa, fb = f_by_id[a], f_by_id[b]
        base = 0.0
        for e in shared:
            base += tag_morphology(fa["tags"], fb["tags"]) / (degree[e] ** ALPHA)
        conf = (CONF_W[fa["conf"]] + CONF_W[fb["conf"]]) / 2.0
        d = xdom_of(dom_of, dom_ents, a, b)
        direct_rows.append({"a": a, "b": b, "score": base * conf * xdom_factor(d),
                            "base": base, "conf": conf, "xdom": d,
                            "shared": sorted(shared, key=lambda x: -degree[x])})
    direct_rows.sort(key=lambda r: -r["score"])

    # C. 标签对冲（A 的子集）
    contra_rows = [r for r in direct_rows
                   if any(tag_morphology(f_by_id[r["a"]]["tags"], f_by_id[r["b"]]["tags"]) == W_CONTRADICT
                          for _ in r["shared"])]

    # B. 二跳链（类型化 ABC：s1 -p1-> M -p2-> o2）
    by_subj = {}
    for i, (s, p, o, fid) in enumerate(edges):
        by_subj.setdefault(s, []).append(i)
    two = {}
    for i, (s1, p1, m, f1) in enumerate(edges):
        if degree.get(m, 0) < min_degree:
            continue
        r_m = role_of(lex, m)
        for j in by_subj.get(m, ()):
            s2, p2, o2, f2 = edges[j]
            if f2 == f1 or s1 == o2:
                continue
            w = (PREDICATES[p1]["w"] * PREDICATES[p2]["w"]
                 * metapath_bonus(role_of(lex, s1), r_m, role_of(lex, o2)))
            key = (f1, f2) if f1 < f2 else (f2, f1)
            slot = two.setdefault(key, {"base": 0.0, "paths": []})
            slot["base"] += w / (degree[m] ** ALPHA)
            slot["paths"].append((s1, p1, m, p2, o2))
    twohop_rows = []
    for (a, b), slot in two.items():
        fa, fb = f_by_id.get(a), f_by_id.get(b)
        if not fa or not fb:
            continue
        conf = (CONF_W[fa["conf"]] + CONF_W[fb["conf"]]) / 2.0
        paths = sorted(slot["paths"], key=lambda x: -PREDICATES[x[1]]["w"] * PREDICATES[x[3]]["w"])
        d = xdom_of(dom_of, dom_ents, a, b)
        twohop_rows.append({"a": a, "b": b, "score": slot["base"] * conf * xdom_factor(d),
                            "base": slot["base"], "conf": conf, "xdom": d, "paths": paths[:3]})
    twohop_rows.sort(key=lambda r: -r["score"])

    # D. 谓词冲突（同 (主体, 客体)、极性相反）
    grp = {}
    for s, p, o, fid in edges:
        grp.setdefault((s, o), []).append((p, fid))
    conflict_rows = []
    for (s, o), lst in grp.items():
        pols = {PREDICATES[p]["pol"] for p, _ in lst}
        if +1 in pols and -1 in pols:
            fids = sorted({fid for _, fid in lst})
            if len(fids) < 2:
                continue
            conf = sum(CONF_W[f_by_id[x]["conf"]] for x in fids) / float(len(fids))
            d = max((xdom_of(dom_of, dom_ents, x, y) for x, y in itertools.combinations(fids, 2)),
                    default=0.0)
            conflict_rows.append({"s": s, "o": o, "fids": fids, "edges": lst,
                                  "score": W_CONFLICT * conf * xdom_factor(d), "conf": conf, "xdom": d})
    conflict_rows.sort(key=lambda r: -r["score"])

    # E. 跨领域远链（跨域距离 ≥ xdom_min 的二跳链 / 直接共现，越远越可能未被领域专家熟知）
    # 每条打 hard 标记：只有共享/中继的是分子实体或通路（且非泛词）才算机制硬桥接。
    xdom_rows, seen_pairs = [], set()
    for kind, rows in (("二跳链", twohop_rows), ("直接共现", direct_rows)):
        for r in rows:
            if r["xdom"] < xdom_min:
                continue
            key = (r["a"], r["b"])
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            if kind == "直接共现":
                hard = any(is_hard_entity(lex, e, degree) for e in r["shared"])
            else:
                mids = [p[2] for p in r.get("paths", [])]
                hard = any(is_hard_entity(lex, m, degree) for m in mids)
            xdom_rows.append(dict(r, kind=kind, hard=hard))
    xdom_rows.sort(key=lambda r: (not r["hard"], -r["score"], -r["xdom"]))

    stats = {"f_total": len(findings), "f_annotated": sum(1 for f in findings if f["triples"]),
             "triples": len(edges), "unknown_pred": sorted(unknown_pred),
             "unknown_ent": sorted(unknown_ent), "domains": len(dom_ents)}
    return direct_rows, contra_rows, twohop_rows, conflict_rows, xdom_rows, dom_of, dom_ents, degree, f_by_id, stats


def brief(f, width=50):
    t = f["text"]
    return t if len(t) <= width else t[:width] + "…"


def covered_pairs(links):
    out = set()
    for c in links:
        for a, b in itertools.combinations(sorted(c["refs"]), 2):
            out.add((a, b))
    return out


def render(direct_rows, contra_rows, twohop_rows, conflict_rows, xdom_rows, dom_of,
           degree, f_by_id, covered, top):
    L = ["=" * 66, "洞见候选扫描（insight_scan v2 · 类型化图谱 + 跨域距离层）", "=" * 66]

    def pair_lines(a, b, flag, tagline):
        fa, fb = f_by_id[a], f_by_id[b]
        L.append(f"{tagline}{flag}")
        L.append(f"        {a}（{fa['conf']}｜{fa['topic']}）{brief(fa)}")
        L.append(f"        {b}（{fb['conf']}｜{fb['topic']}）{brief(fb)}")

    L.append("")
    L.append(f"── E. 跨领域远链（跨域距离 ≥{XDOM_MIN}：越远越可能未被领域专家熟知）" + "─" * 4)
    n_hard = sum(1 for r in xdom_rows if r.get("hard"))
    L.append(f"  硬桥接 {n_hard} 条 / 背景共现 {len(xdom_rows) - n_hard} 条（硬＝共享或中继是分子/通路实体且度数≤{HARD_DEG_MAX}）")
    if not xdom_rows:
        L.append("  （无）")
    for r in xdom_rows[:top]:
        flag = ("  [硬]" if r.get("hard") else "")
        flag += "  [已覆盖]" if (r["a"], r["b"]) in covered else ""
        da, db = dom_of.get(r["a"], "?"), dom_of.get(r["b"], "?")
        L.append(f"[{r['score']:5.2f}] 跨域 {r['xdom']:.2f}｜{da} ⇄ {db}{flag}")
        if r["kind"] == "二跳链":
            for s1, p1, m, p2, o2 in r["paths"]:
                d = sign_label([PREDICATES[p1]["pol"], PREDICATES[p2]["pol"]])
                L.append(f"        链：{s1} --{p1}→ {m} --{p2}→ {o2} ｜ 净方向：{d}")
        else:
            ent_s = "、".join(f"{e}(度{degree[e]})" for e in r["shared"])
            L.append(f"        共享：{ent_s}")
        L.append(f"        {r['a']}（{f_by_id[r['a']]['conf']}）{brief(f_by_id[r['a']])}")
        L.append(f"        {r['b']}（{f_by_id[r['b']]['conf']}）{brief(f_by_id[r['b']])}")

    L.append("")
    L.append("── D. 谓词冲突（同一主客体、极性相反，最该先看）" + "─" * 14)
    if not conflict_rows:
        L.append("  （无）")
    for r in conflict_rows[:top]:
        tag = "  ".join(f"{p}·{fid}" for p, fid in r["edges"])
        L.append(f"[{r['score']:5.2f}] {r['s']} ⟷ {r['o']}  涉及：{'、'.join(r['fids'])}")
        L.append(f"        冲突边：{tag}")

    L.append("")
    L.append("── B. 二跳链（s1 -p1-> M -p2-> o2，带方向）" + "─" * 22)
    if not twohop_rows:
        L.append("  （无）")
    for r in twohop_rows[:top]:
        flag = "  [已覆盖]" if (r["a"], r["b"]) in covered else ""
        L.append(f"[{r['score']:5.2f}] {r['a']} × {r['b']}{flag}")
        for s1, p1, m, p2, o2 in r["paths"]:
            d = sign_label([PREDICATES[p1]["pol"], PREDICATES[p2]["pol"]])
            L.append(f"        链：{s1} --{p1}→ {m} --{p2}→ {o2} ｜ 净方向：{d}")
        L.append(f"        {r['a']}（{f_by_id[r['a']]['conf']}）{brief(f_by_id[r['a']])}")
        L.append(f"        {r['b']}（{f_by_id[r['b']]['conf']}）{brief(f_by_id[r['b']])}")

    L.append("")
    L.append("── A. 直接共现（共享实体，召回）" + "─" * 30)
    if not direct_rows:
        L.append("  （无）")
    for r in direct_rows[:top]:
        flag = "  [已覆盖]" if (r["a"], r["b"]) in covered else ""
        ent_s = "、".join(f"{e}(度{degree[e]})" for e in r["shared"])
        L.append(f"[{r['score']:5.2f}] {r['a']} × {r['b']}{flag}  共享：{ent_s}")
        L.append(f"        {brief(f_by_id[r['a']])}")
        L.append(f"        {brief(f_by_id[r['b']])}")

    L.append("")
    L.append("── C. 标签对冲（同实体 #干预 × #安全信号）" + "─" * 20)
    if not contra_rows:
        L.append("  （无）")
    for r in contra_rows[:top]:
        L.append(f"[{r['score']:5.2f}] {r['a']} × {r['b']}  共享：{'、'.join(r['shared'])}")

    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="洞见候选扫描器 v2（类型化图谱 + 跨域距离层）")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--min-degree", type=int, default=2)
    ap.add_argument("--xdom-min", type=float, default=XDOM_MIN, help="跨领域远链展示门槛")
    ap.add_argument("--write", default=None)
    args = ap.parse_args()

    for p in (FINDINGS, ENTITIES):
        if not os.path.isfile(p):
            print(f"[F] 读不到 {p}")
            return 2

    findings, links = parse_findings(read_text(FINDINGS))
    lex = load_lexicon(ENTITIES)
    direct, contra, twohop, conflict, xdom_rows, dom_of, dom_ents, degree, f_by_id, stats = \
        scan(findings, lex, args.min_degree, args.xdom_min)

    pair_ents = {e for e, d in degree.items() if d >= args.min_degree}
    header = (f"库：{stats['f_total']} 条结论（已标注三元组 {stats['f_annotated']} 条 / "
              f"共 {stats['triples']} 条边）｜词典 {len(lex)} 实体｜领域 {stats['domains']} 个｜"
              f"可配对实体（度≥{args.min_degree}）{len(pair_ents)} 个｜已有联系 {len(links)} 条")
    if stats["unknown_pred"]:
        header += f"\n[W] 未知谓词（不在闭集，已忽略）：{'、'.join(stats['unknown_pred'])}"
    if stats["unknown_ent"]:
        header += f"\n[W] 未入词典的实体（补 entities.json）：{'、'.join(stats['unknown_ent'])}"

    covered = covered_pairs(links)
    body = render(direct, contra, twohop, conflict, xdom_rows, dom_of, degree, f_by_id,
                  covered, args.top)

    print(header)
    print(body)
    print("")
    print(f"候选计数：谓词冲突 {len(conflict)}｜二跳链 {len(twohop)}｜直接共现 {len(direct)}"
          f"（其中标签对冲 {len(contra)}）｜跨领域远链 {len(xdom_rows)}（门槛 {args.xdom_min}）")

    if args.write:
        d = os.path.dirname(args.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with io.open(args.write, "w", encoding="utf-8") as fh:
            fh.write("# 洞见候选清单（insight_scan 自动生成）\n\n")
            fh.write(header + "\n\n")
            fh.write("> 候选 ≠ 结论。每条都要过新颖性查重 + 自我反驳，才可能成为 C 条目。\n\n")
            fh.write("```\n" + body + "\n```\n")
        print(f"已落盘：{args.write}")
    return 0


if __name__ == "__main__":
    sys.exit(main())