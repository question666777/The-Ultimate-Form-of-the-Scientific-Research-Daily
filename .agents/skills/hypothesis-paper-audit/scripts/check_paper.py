#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""假说论文确定性检查（hypothesis-paper-audit 第 8/9/11/12/13 维 + 第 1/5/15 维启发式线索）。

退出码：0 通过 / 1 告警 / 2 失败。
只用标准库。用法：
    python check_paper.py [--json <out.json>] [--en <path>] [--cn <path>]

D15（外部说服力/敌对评审）只给启发式线索，不作最终判定，也不计入退出码；
人工按四条攻击向量复核后定档。--en/--cn 可覆盖默认稿件路径（用于复跑归档稿）。
"""
import argparse
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
EXAMPLE = os.path.join(REPO, "example")
EN = os.path.join(EXAMPLE, "manuscript_EN.md")
CN = os.path.join(EXAMPLE, "manuscript_CN.md")
SUBMISSION = os.path.join(EXAMPLE, "submission")

# Medical Hypotheses 六段结构（英文稿标题）
EXPECTED_SECTIONS = [
    "1. Introduction",
    "2. Hypothesis",
    "3. Evaluation of the hypothesis",
    "4. Hypothesis testing",
    "5. Consequences of the hypothesis and discussion",
]

LIMITS = {"abstract": 250, "body": 3000, "figures": 3, "keywords": 6, "refs": 50}

# 中英关键术语对（EN token, CN token）
TERM_PAIRS = [
    ("RICTOR", "RICTOR"),
    ("mTORC2", "mTORC2"),
    ("methionine cycle", "蛋氨酸循环"),
    ("S-adenosylmethionine", "S-腺苷甲硫氨酸"),
    ("mitophagy", "线粒体自噬"),
    ("longevity", "长寿"),
]

PRIORITY_PATTERNS = [
    r"no study has", r"no prior study", r"never been", r"the first to",
    r"for the first time", r"unprecedented", r"no literature", r"none has",
    r"no work has", r"first demonstration",
]

# D15 敌对评审启发式线索
# ② 跨语境收敛：桥接/收敛类词
BRIDGE_PATTERNS = [
    r"\bbridge\b", r"\bconverg\w*", r"\bcoupl\w*", r"\blink(?:s|ed|ing)?\b",
    r"\bconnect\w*", r"\bunif\w*", r"\bmerg\w*",
]
# ④ 弱化/推断用语（用于判断优先权断言是否与支撑强度相称）
HEDGE_PATTERNS = [
    r"\bmay\b", r"\bmight\b", r"\bcould\b", r"\bto (?:our|my) knowledge\b",
    r"\b(?:we|I) (?:propose|suggest|hypothes\w*)", r"\bplausib\w*", r"\bputative\b",
    r"\bremains? to be\b", r"\bnot yet\b", r"\bpresum\w*",
]
# ③ 便宜/决定性证伪实验用语
DECISIVE_PATTERNS = [
    r"\bfalsif\w*", r"\brefut\w*", r"\bwould collapse\b", r"\bwould be inconsistent\b",
    r"\bdecisive\w*", r"\bsingle experiment\b", r"\bcheap\w*", r"\binexpensive\b",
    r"\blow[- ]cost\b", r"\breadily\b", r"\bstandard assay\b", r"\bcan be (?:readily )?tested\b",
]
# ① 自认未测/推断（原创增量押在最弱证据环节的信号）
UNMEASURED_PATTERNS = [
    r"(?:not yet|never been|remains? to be|has yet to be)\s+\w*\s*(?:measured|tested|verified|established|demonstrated|shown)",
    r"\bun(?:measured|tested|verified|established)\b",
    r"(?:尚未|未(?:被)?直接?)\s*(?:测定|验证|证实|检验)",
    r"no direct evidence",
]


def wc(s):
    return len(re.findall(r"[A-Za-z][A-Za-z'\-]*", s))


def split_en(t):
    abstract = t.split("## Abstract")[1].split("**Keywords**")[0].strip()
    keywords = t.split("**Keywords**:")[1].split("\n")[0].strip()
    body = t.split("## 1. Introduction")[1].split("## References")[0]
    refs_block = t.split("## References")[1].split("## Figure legend")[0]
    fig_block = t.split("## Figure legend")[1] if "## Figure legend" in t else ""
    return abstract, keywords, body, refs_block, fig_block


def check_format(t, abstract, keywords, body, refs_block, fig_block):
    items, status = [], 0

    def add(name, ok, detail, sev=1):
        nonlocal status
        items.append({"item": name, "ok": ok, "detail": detail})
        if not ok:
            status = max(status, sev)

    headings = re.findall(r"^## (.+)$", t, re.M)
    for s in EXPECTED_SECTIONS:
        add("section:" + s, any(h.strip() == s for h in headings),
            "found" if any(h.strip() == s for h in headings) else "MISSING")

    ab_w = wc(abstract)
    add("abstract<=250", ab_w <= LIMITS["abstract"], f"{ab_w} words", 2)
    body_w = wc(body)
    add("body<=3000", body_w <= LIMITS["body"], f"{body_w} words", 2)
    kw = [k.strip() for k in keywords.split(";") if k.strip()]
    add("keywords<=6", len(kw) <= LIMITS["keywords"], f"{len(kw)} keywords", 2)
    refs = re.findall(r"^\d+\.\s", refs_block, re.M)
    add("refs<=50", len(refs) <= LIMITS["refs"], f"{len(refs)} refs", 2)
    fig_n = len(re.findall(r"^\*\*Figure\s+\d+", fig_block, re.M)) or len(
        re.findall(r"Figure\s+\d+", fig_block))
    add("figures<=3", fig_n <= LIMITS["figures"], f"{fig_n} figures", 2)

    return {"status": status, "items": items,
            "counts": {"abstract": ab_w, "body": body_w, "keywords": len(kw),
                       "refs": len(refs), "figures": fig_n}}


def check_citations(t, abstract, body, refs_block):
    items, status = [], 0
    text = abstract + "\n" + body

    cited = set()
    for m in re.finditer(r"\[([\d,\u2013\-]+)\]", text):
        for part in m.group(1).split(","):
            part = part.strip()
            if "\u2013" in part:
                a, b = part.split("\u2013")
                cited.update(range(int(a), int(b) + 1))
            elif "-" in part and part.replace("-", "").isdigit():
                a, b = part.split("-")
                cited.update(range(int(a), int(b) + 1))
            elif part.isdigit():
                cited.add(int(part))

    listed = set(int(m.group(1)) for m in re.finditer(r"^(\d+)\.\s", refs_block, re.M))

    orphan_cite = sorted(cited - listed)
    orphan_ref = sorted(listed - cited)
    items.append({"item": "cited-but-not-listed", "ok": not orphan_cite, "detail": orphan_cite})
    items.append({"item": "listed-but-not-cited", "ok": not orphan_ref, "detail": orphan_ref})
    if orphan_cite or orphan_ref:
        status = max(status, 2)

    dois = re.findall(r"10\.\d{4,9}/[^\s\)\]]+", refs_block)
    bad_doi = [d for d in dois if not re.match(r"^10\.\d{4,9}/[A-Za-z0-9\.\-_\(\)/]+$", d)]
    items.append({"item": "doi-format", "ok": not bad_doi, "detail": bad_doi})
    if bad_doi:
        status = max(status, 1)

    seen, dup = set(), []
    for d in dois:
        k = d.rstrip(".").lower()
        if k in seen:
            dup.append(d)
        seen.add(k)
    items.append({"item": "no-duplicate-doi", "ok": not dup, "detail": dup})
    if dup:
        status = max(status, 2)

    return {"status": status, "items": items,
            "counts": {"cited": len(cited), "listed": len(listed), "dois": len(dois)}}


def check_consistency(t, abstract, body, cn):
    items, status = [], 0
    for en_tok, cn_tok in TERM_PAIRS:
        in_ab = en_tok.lower() in abstract.lower()
        in_body = en_tok.lower() in body.lower()
        in_cn = cn_tok.lower() in cn.lower()
        ok = in_ab and in_body and in_cn
        items.append({"item": f"term:{en_tok}/{cn_tok}", "ok": ok,
                      "detail": f"abstract={in_ab} body={in_body} cn={in_cn}"})
        if not ok:
            status = max(status, 1)
    return {"status": status, "items": items}


def check_priority_assertions(t):
    hits = []
    for pat in PRIORITY_PATTERNS:
        for m in re.finditer(pat, t, re.I):
            line = t[:m.start()].count("\n") + 1
            seg = t[max(0, m.start() - 60):m.start() + 80].replace("\n", " ")
            hits.append({"line": line, "phrase": m.group(0), "context": seg.strip()})
    return {"status": 1 if hits else 0, "items": hits}


def check_uncited_claims(body):
    empirical = re.compile(r"\b(shows?|demonstrat\w+|found|reported|revealed)\b", re.I)
    hits = []
    for line in body.split("\n"):
        if not line.strip() or line.strip().startswith(("#", "-", "*", ">")):
            continue
        for sent in re.split(r"(?<=[.])\s+", line):
            if empirical.search(sent) and not re.search(r"\[\d", sent):
                hits.append(sent.strip()[:110])
    return {"status": 1 if hits else 0, "items": hits}


def extract_section(t, header):
    m = re.search(r"^##\s*" + re.escape(header) + r"\s*$(.*?)(?=^##\s|\Z)",
                  t, re.M | re.S)
    return m.group(1) if m else ""


def check_adversarial_review(t, body, fig_block):
    """D15 外部说服力（敌对评审）：四条攻击向量的启发式命中点。

    只给线索、不作判定、不计入退出码。人工按四向量复核后定档。
    向量：① 原创增量是否押在最弱证据环节 ② 跨语境收敛是否为硬拼合
          ③ 有无便宜的决定性证伪实验 ④ 优先权断言是否与支撑强度相称
    """
    hits = {
        "originality_on_weakest_link": [],
        "forced_bridge": [],
        "no_cheap_decisive_test": [],
        "priority_unsupported": [],
    }
    lines = t.split("\n")
    # 只在正文区扫描（排除 References 与图注，避免参考文献标题里的 bridge/link 词误入）
    ref_line = next((i for i, ln in enumerate(lines)
                     if ln.strip().startswith("## References")), len(lines))
    ref_cut = len("\n".join(lines[:ref_line]))
    unmeasured = re.compile("|".join(UNMEASURED_PATTERNS), re.I)

    # ① 核心原创对象（心磷脂）出现在「自认未测/推断」句里 → 原创增量押在最弱证据环节
    for i, ln in enumerate(lines[:ref_line], 1):
        if re.search(r"SAM|methylation", ln, re.I) and unmeasured.search(ln):
            hits["originality_on_weakest_link"].append(
                {"line": i, "text": ln.strip()[:150]})

    # ② 桥接/收敛断言句引证 ≤1 条 → 硬拼合风险（缺同源证据）
    bridge = re.compile("|".join(BRIDGE_PATTERNS), re.I)
    for i, ln in enumerate(lines[:ref_line], 1):
        if ln.strip().startswith(("#", ">", "|")):
            continue
        if bridge.search(ln) and len(re.findall(r"\[\d", ln)) <= 1:
            hits["forced_bridge"].append({"line": i, "text": ln.strip()[:150]})

    # ③ Hypothesis testing 章节内未见「便宜/决定性证伪实验」表述
    testing = extract_section(t, "4. Hypothesis testing")
    if testing and not re.search("|".join(DECISIVE_PATTERNS), testing, re.I):
        hits["no_cheap_decisive_test"].append(
            {"line": 0, "text": "Section 4 未见 falsif/decisive/cheap/single experiment 类表述"})

    # ④ 优先权断言句未含弱化用语 → 断言强度可能超过支撑（按行去重）
    seen = set()
    for pat in PRIORITY_PATTERNS:
        for m in re.finditer(pat, t, re.I):
            if m.start() >= ref_cut:
                continue
            line = t[:m.start()].count("\n") + 1
            if line in seen:
                continue
            seg = lines[line - 1] if 0 <= line - 1 < len(lines) else ""
            if not any(re.search(h, seg, re.I) for h in HEDGE_PATTERNS):
                seen.add(line)
                hits["priority_unsupported"].append(
                    {"line": line, "text": seg.strip()[:150]})

    total = sum(len(v) for v in hits.values())
    return {"status": 1 if total else 0, "items": hits, "total": total}


def check_figure_consistency(body, fig_block):
    """D12：图注的证据分层与正文对同一环节的定性是否一致。"""
    items, status = [], 0

    def add(name, ok, detail, sev=1):
        nonlocal status
        items.append({"item": name, "ok": ok, "detail": detail})
        if not ok:
            status = max(status, sev)

    add("fig1-cited-in-body", bool(re.search(r"(Figure|Fig\.?)\s*1\b", body)),
        "Figure 1 referenced in body", 2)

    for tok in ("RICTOR", "SAM", "methionine", "mitophagy", "methylation", "succinate"):
        add(f"legend-term:{tok}", tok.lower() in body.lower(),
            f"'{tok}' in body", 1)

    add("legend-layering",
        bool(re.search(r"solid arrow", fig_block, re.I))
        and bool(re.search(r"dashed arrow", fig_block, re.I)),
        "solid/dashed convention stated in legend", 1)

    # 图注若声明「琥珀酸→线粒体自噬（已有证据，实线）」，正文须有带引用的对应陈述
    legend_est = bool(re.search(r"established", fig_block, re.I))
    body_est = [l for l in body.split("\n")
                if re.search(r"succinate", l, re.I) and re.search(r"mitophagy", l, re.I)
                and re.search(r"\[\d", l)]
    add("legend-claim:succinate-mitophagy", (not legend_est) or bool(body_est),
        f"legend={legend_est} body_cited_lines={len(body_est)}", 1)

    # 图注若声明「SAM→甲基化（本文提出，虚线）」，正文须把该环节标为提出/未测定
    legend_prop = bool(re.search(r"proposed here", fig_block, re.I))
    body_prop = bool(re.search(
        r"(not yet been|proposed, not yet|is proposed)", body, re.I))
    add("legend-claim:SAM-methylation-proposed", (not legend_prop) or body_prop,
        f"legend={legend_prop} body={body_prop}", 1)

    return {"status": status, "items": items}


def check_submission_package():
    """D13：投稿包五件套齐备 + docx 元数据卫生。"""
    items, status = [], 0

    def add(name, ok, detail, sev=1):
        nonlocal status
        items.append({"item": name, "ok": ok, "detail": detail})
        if not ok:
            status = max(status, sev)

    if not os.path.isdir(SUBMISSION):
        items.append({"item": "submission-package", "ok": True,
                      "detail": "未观察（无 submission 目录；示例未含投稿包，D13 不评分）"})
        return {"status": 0, "items": items}

    files = ["1_Manuscript.docx", "2_Highlights.docx",
             "3_Title_page_with_author_details.docx", "4_Cover_letter.docx",
             "5_Declaration_of_Generative_AI.docx"]
    for f in files:
        p = os.path.join(SUBMISSION, f)
        add(f"exists:{f}", os.path.exists(p), p, 2)
        if not os.path.exists(p):
            continue
        try:
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
                core = z.read("docProps/core.xml").decode("utf-8", "ignore")
                m = re.search(r"<dc:creator>([^<]*)</dc:creator>", core)
                creator = m.group(1) if m else ""
                thumb = any("thumbnail" in n for n in names)
            add(f"meta:{f}", ("python-docx" not in creator.lower()) and not thumb,
                f"creator='{creator}' thumbnail={thumb}", 1)
        except Exception as e:
            add(f"meta:{f}", False, f"unreadable: {e}", 1)

    return {"status": status, "items": items}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--en", default=EN, help="英文稿路径（默认投稿稿）")
    ap.add_argument("--cn", default=CN, help="中文底稿路径")
    args = ap.parse_args()

    for label, path in (("EN", args.en), ("CN", args.cn)):
        if not os.path.exists(path):
            print(f"[{label}] 稿件缺失，无法检查：{path}")
            return 2

    t = open(args.en, encoding="utf-8").read()
    cn = open(args.cn, encoding="utf-8").read()
    abstract, keywords, body, refs_block, fig_block = split_en(t)

    report = {
        "D8_format": check_format(t, abstract, keywords, body, refs_block, fig_block),
        "D9_citation": check_citations(t, abstract, body, refs_block),
        "D11_consistency": check_consistency(t, abstract, body, cn),
        "D12_figure": check_figure_consistency(body, fig_block),
        "D13_package": check_submission_package(),
        "D1_priority_assertions": check_priority_assertions(t),
        "D5_uncited_claims": check_uncited_claims(body),
        "D15_adversarial_review": check_adversarial_review(t, body, fig_block),
    }
    codes = [report[k]["status"] for k in
             ("D8_format", "D9_citation", "D11_consistency", "D12_figure", "D13_package")]
    report["exit"] = max(codes) if codes else 0

    print("=" * 66)
    print("假说论文确定性检查")
    print("=" * 66)
    for key in ("D8_format", "D9_citation", "D11_consistency", "D12_figure", "D13_package"):
        st = {0: "通过", 1: "告警", 2: "失败"}[report[key]["status"]]
        print(f"\n[{key}] {st}")
        show_all = key in ("D12_figure", "D13_package")
        for it in report[key]["items"]:
            if show_all or not it.get("ok", True):
                flag = "ok " if it.get("ok", True) else " ! "
                print(f"  {flag}{it['item']}: {it['detail']}")
        if "counts" in report[key]:
            print(f"   counts: {report[key]['counts']}")

    print(f"\n[D1 未核实优先权断言] {len(report['D1_priority_assertions']['items'])} 处")
    for h in report["D1_priority_assertions"]["items"]:
        print(f"   L{h['line']}: '{h['phrase']}' ... {h['context']}")

    print(f"\n[D5 未标引的实证陈述（启发式）] {len(report['D5_uncited_claims']['items'])} 处")
    for s in report["D5_uncited_claims"]["items"]:
        print(f"   - {s}")

    d15 = report["D15_adversarial_review"]
    labels = {
        "originality_on_weakest_link": "① 原创增量押在最弱证据环节",
        "forced_bridge": "② 跨语境收敛疑似硬拼合（桥接句引证≤1）",
        "no_cheap_decisive_test": "③ 未见便宜/决定性证伪实验",
        "priority_unsupported": "④ 优先权断言未含弱化用语",
    }
    print(f"\n[D15 外部说服力/敌对评审（启发式线索，不计入退出码）] 共 {d15['total']} 处")
    for key, lab in labels.items():
        rows = d15["items"][key]
        print(f"   {lab}: {len(rows)}")
        for r in rows:
            loc = f"L{r['line']}" if r["line"] else "-"
            print(f"      {loc} {r['text']}")

    print(f"\n退出码: {report['exit']}  (0 通过 / 1 告警 / 2 失败)")

    if args.json:
        os.makedirs(os.path.dirname(args.json), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"JSON -> {args.json}")

    return report["exit"]


if __name__ == "__main__":
    sys.exit(main())