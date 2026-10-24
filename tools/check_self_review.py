#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""check_self_review.py — 内向敌对自审的可自动化向量（审计第 14 维）

背景：2026-10-13 用户指出本项目「缺少完全独立自主的审查机制」——现有自审计本质是
一份清单执行器，只能找到「已经写进去要找的东西」。四处盲区：
  (1) 派生标识漂移：AGENTS.md 写「十二维」、技能文件写「十三维」、运行日志模板写「十一维」；
  (2) 忽略区残留：tmp/ 与 *.bak-* 都在 .gitignore 里，git status 与脚本体检都看不见，
      「发现就清」的瘦身纪律因此实际是盲的；
  (3) 价值观矛盾：去 AI 痕迹 vs 提交 AI 使用声明，方向打架；
  (4) 缺席的 gate：有十五维事后诊断，却没有动笔前「该不该投这个刊」的前置否决权。
本脚本把 (1)(2) 变成可复跑的确定性检查；(3)(4) 需对抗姿态，由 SKILL.md 的
「内向敌对自审」协议承担（人工，不脚本化——它们问的是「清单本身缺了什么」，
任何脚本都只能检查清单里已有的条目，无法检查清单缺了什么）。

固化清单（行首 L### 供 check_closure_health.py 识别）：
L044  自审必须独立自主：派生标识从源头重算并全项目比对（R1）+ 瘦身扫文件系统含忽略区（R2）。
      已固化为本脚本 + SKILL.md「内向敌对自审」协议（矛盾扫描 / 缺席审查）。

检查项
------
  R1 派生事实重算：对每条「派生事实」（维度数、各类上限、候选主题数、项目技能数），从**源头**
    （结构性定义处，含 AST 解析代码常量、目录计数）重算真值，再扫**声明点**（live 文档里复述
    该值的地方，含 README）比对。不一致即漂移。**历史运行日志不是声明点**——改了反而是篡改历史。
  R1b 维度编号映射校验（2026-10-17 新增）：从 self-audit SKILL.md 评分表解析「编号→名称」的
    单一真相源，反向校验 lessons.md 里「第N维「名称」」引用——编号对但名称错（L047 型漂移，R1 总数
    校验看不见）与裸维度编号（不自描述）均被抓出。
  R2 忽略区扫描：直接走文件系统（含 .gitignore 覆盖区），找 tmp 残留 / *.bak-* /
     __pycache__ / *.pyc 等该清未清的垃圾。git status 看不见的东西，这里看得见。
  R3 数字普查（2026-10-13 新增，详见代码区注释）：不变量「live 文档里每个数字+单位声明，
     要么被 registry 声明点覆盖、要么属噪声白名单」。查不到源头又不属噪声的＝孤儿，
     浮出来由人工勾选——补进 FACTS（从此被 R1 永久校验）或确认噪声入白名单。
     「哪些算派生事实」由此从人工回忆变成机器列候选、人工勾选。

退出码：0 通过 / 1 告警 / 2 失败
  R1 任一派生事实漂移 → 失败（2）：文档与事实不一致是「根本目标」的硬伤
  R1 源头读不出 / 声明点正则未命中 → 告警（1）：registry 自身需同步
  R1b 维度编号↔名称映射漂移 → 失败（2）：编号对但名称错 / 编号不存在
  R1b 裸维度编号引用（未带「名称」） → 告警（1）：不自描述，应补「名称」防漂移
  R2 有残留垃圾       → 告警（1）：该清未清（瘦身纪律未执行）
  R3 有孤儿数字声明   → 告警（1）：待人工勾选（补 registry 或入白名单）
只用标准库。用法（在项目根运行）：
    python tools\\check_self_review.py
    python tools\\check_self_review.py --json tmp\\audit_self_review.json
"""

import argparse
import ast
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def cn_to_int(s):
    """中文数字（十三 / 十五 / 二十 / 三十一）与阿拉伯数字统一转 int。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if "十" not in s:
        return CN_DIGITS.get(s, 0)
    a, b = s.split("十", 1)
    left = CN_DIGITS.get(a, 1) if a else 1
    right = CN_DIGITS.get(b, 0) if b else 0
    return left * 10 + right


# ---------------------------------------------------------------------------
# 派生事实登记表（registry）
#
# 每条 = 一个「派生事实」：源头（结构性定义处）1 个，声明点（live 文档里复述该值的地方）若干。
# 新增派生事实 = 加一条 registry 条目，无需改代码逻辑。
# 纪律：声明点只列 **live 文档**（AGENTS.md / SKILL.md / README / 模板 / 数据文件）；
#       历史运行日志（learning\runs\、论文\audit\runs\）**不入声明点**——那是历史真实，
#       改了反而是篡改历史。彻底消除副本（如模板不再写死数字）时，把该声明点一并删掉。
# 局限与对策（2026-10-13 用户指出后修订）：registry 是人工登记表，初始完备性无法自证。
#       对策见下方 R3 数字普查——机器把 live 文档里的数字声明全捞出来，孤儿浮出交人工勾选，
#       registry 由此收敛而非靠回忆。
# ---------------------------------------------------------------------------
FACTS = [
    {
        "key": "self_audit_dims",
        "label": "self-audit 评分表维度数",
        "source": {"file": ".agents/skills/self-audit/SKILL.md",
                   "re": r"审计评分表（(十?[一二三四五六七八九十]+|\d+)维"},
        "sites": [
            {"file": ".agents/skills/self-audit/SKILL.md",
             "re": r"（(十?[一二三四五六七八九十]+|\d+)维各一行"},
            {"file": ".agents/skills/self-audit/assets/run-log-template.md",
             "re": r"##\s*(十?[一二三四五六七八九十]+|\d+)维审计"},
            {"file": ".agents/skills/insight-synthesis/SKILL.md",
             "re": r"（(十?[一二三四五六七八九十]+|\d+)维评分"},
            {"file": "tools/skill_playbooks.json",
             "re": r"(十?[一二三四五六七八九十]+|\d+)维复盘"},
            {"file": "README.md",
             "re": r"（(十?[一二三四五六七八九十]+|\d+)维评分"},
        ],
    },
    {
        "key": "paper_audit_dims",
        "label": "hypothesis-paper-audit 评分表维度数",
        "source": {"file": ".agents/skills/hypothesis-paper-audit/SKILL.md",
                   "re": r"当前 (\d+) 维"},
        "sites": [
            {"file": ".agents/skills/hypothesis-paper-audit/assets/run-log-template.md",
             "re": r"审计评分表（(\d+) 维）"},
        ],
    },
    {
        "key": "active_cap",
        "label": "lessons active 区上限",
        "source": {"file": "learning/lessons.md", "re": r"active 上限 (\d+) 条"},
        "sites": [
            {"file": "AGENTS.md", "re": r"active 上限 (\d+) 条"},
            {"file": ".agents/skills/self-audit/SKILL.md", "re": r"上限 (\d+) 条 active"},
        ],
    },
    {
        "key": "fixed_cap",
        "label": "lessons fixed 区上限",
        "source": {"file": "learning/lessons.md", "re": r"fixed 上限 (\d+) 条"},
        "sites": [
            {"file": "AGENTS.md", "re": r"fixed 上限 (\d+) 条"},
        ],
    },
    {
        "key": "sentinel_cap",
        "label": "sentinel_pool 上限",
        "source": {"file": "tools/select_topics.py", "re": r"POOL_MAX = (\d+)"},
        "sites": [
            {"file": "AGENTS.md", "re": r"哨兵池（上限\s*(\d+)"},
            {"file": "learning/lessons.md", "re": r"池上限 (\d+)"},
        ],
    },
    {
        "key": "canonical_topics",
        "label": "候选主题总表条数",
        "source": {"file": "tools/select_topics.py", "ast": "len:CANONICAL_TOPICS"},
        "sites": [
            {"file": "AGENTS.md", "re": r"(\d+) 个候选主题"},
            {"file": "tools/README.md", "re": r"(\d+) 个候选主题"},
            {"file": "AGENTS.md", "re": r"随候选总表 (\d+) 个"},
        ],
    },
    {
        "key": "project_skills",
        "label": "项目技能目录数",
        "source": {"file": ".agents/skills", "dir": True},
        "sites": [
            {"file": "README.md", "re": r"(\d+) 个技能"},
        ],
    },
    {
        "key": "journal_count",
        "label": "广角扫描期刊数",
        "source": {"file": "tools/journals.json", "json_len": "flat"},
        "sites": [
            {"file": "README.md", "re": r"(\d+) 本期刊"},
            {"file": "README.md", "re": r"(\d+) 刊最新文献"},
            {"file": "AGENTS.md", "re": r"(\d+) 刊 Crossref"},
            {"file": "tools/README.md", "re": r"(\d+) 刊 Crossref"},
            {"file": "tools/README.md", "re": r"全量 (\d+) 刊"},
        ],
    },
    {
        "key": "retrieval_checks",
        "label": "检索能力健康检查项数",
        "source": {"file": "tools/check_retrieval.py", "count_re": r'^\s*\("[A-F]",'},
        "sites": [
            {"file": ".agents/skills/self-audit/SKILL.md", "re": r"check_retrieval\.py`（(\d+) 项）"},
        ],
    },
    # ---- 2026-10-13 R3 首跑孤儿勾选入库（源头全部经人工核实为机器可读）----
    {
        "key": "closure_checks",
        "label": "闭环健康检查项数",
        "source": {"file": "tools/check_closure_health.py", "count_re": r'print\("\\n\[[A-F]\]'},
        "sites": [
            {"file": ".agents/skills/self-audit/SKILL.md", "re": r"check_closure_health\.py`（(\d+) 项检查）"},
            {"file": "AGENTS.md", "re": r"闭环健康（(\d+)项："},
            {"file": "tools/README.md", "re": r"第 11 维：(\d+)项检查"},
        ],
    },
    {
        "key": "engine_checks",
        "label": "引擎健康检查项数",
        "source": {"file": "tools/check_engine_health.py", "count_re": r"# [A-I]\. "},
        "sites": [
            {"file": ".agents/skills/self-audit/SKILL.md", "re": r"check_engine_health\.py`（(\d+) 项检查）"},
            {"file": "tools/README.md", "re": r"审计第 13 维，(\d+) 项"},
        ],
    },
    {
        "key": "humanize_dims",
        "label": "humanize 检测维数",
        "source": {"file": "tools/humanize_text.py", "ast": "len:detections"},
        "sites": [
            {"file": "AGENTS.md", "re": r"检测与修复 (\d+) 维"},
            {"file": "tools/README.md", "re": r"检测与修复 (\d+) 维"},
        ],
    },
    {
        "key": "memory_tags",
        "label": "记忆库标签闭集数",
        "source": {"file": "tools/memory.py", "ast": "len:TAGS"},
        "sites": [
            {"file": ".agents/skills/self-audit/SKILL.md", "re": r"(\d+) 个闭集"},
        ],
    },
    {
        "key": "predicates",
        "label": "谓词闭集数",
        "source": {"file": "tools/insight_scan.py", "ast": "len:PREDICATES"},
        "sites": [
            {"file": ".agents/skills/insight-synthesis/SKILL.md", "re": r"谓词闭集（(\d+) 个"},
            {"file": ".agents/skills/insight-synthesis/SKILL.md", "re": r"闭集 (\d+) 个"},
        ],
    },
    {
        "key": "min_f_refs",
        "label": "C 条目最小 F 依据数",
        "source": {"file": "tools/memory.py", "re": r'len\(c\["refs"\]\) < (\d+)'},
        "sites": [
            {"file": "AGENTS.md", "re": r"(?:挂|满足)[^≥]{0,4}≥(\d+) 条 F"},
            {"file": ".agents/skills/self-audit/SKILL.md", "re": r"(?:挂|满足)[^≥]{0,4}≥(\d+) 条 F"},
            {"file": ".agents/skills/insight-synthesis/SKILL.md", "re": r"(?:挂|满足)[^≥]{0,4}≥(\d+) 条 F"},
            {"file": ".agents/skills/hypothesis-paper-audit/SKILL.md", "re": r"(?:挂|满足)[^≥]{0,4}≥(\d+) 条 F"},
        ],
    },
    {
        "key": "core_business_skills",
        "label": "核心层业务技能数",
        "source": {"file": "AGENTS.md", "py": "check_skills.core_skills"},
        "sites": [
            {"file": "AGENTS.md", "re": r"共 (\d+) 个业务技能"},
        ],
    },
    {
        "key": "paper_apis",
        "label": "paper-lookup 文献 API 数",
        "source": {"file": ".agents/skills/paper-lookup/SKILL.md", "re": r"(\d+) academic literature APIs"},
        "sites": [
            {"file": "AGENTS.md", "re": r"(\d+) 个文献 API"},
        ],
    },
]

# 忽略区扫描规则（R2）
JUNK_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache"}
JUNK_FILE_RES = [re.compile(p) for p in
                 (r"\.bak-.*$", r"\.pyc$", r"\.orig$", r"\.rej$", r"\.tmp$", r"^~\$")]
SKIP_DIR_NAMES = {".git", "node_modules", ".venv", "venv", ".idea", ".vscode"}

# ---------------------------------------------------------------------------
# R3 数字普查（2026-10-13，三线合一：自动发现候选 + 溯源不变量 + 永久检查）
#
# 不变量：live 文档里的每个「数字+单位」声明，要么被某个 FACTS 声明点正则覆盖
# （其值由 R1 校验），要么命中噪声白名单。两者都不沾＝孤儿。
# 孤儿不是定罪的漂移，是「待人工勾选的候选派生事实」：
#   勾选补进 FACTS → 从此被 R1 永久校验（每条新发现都固化为检查，覆盖面只增不减）；
#   确认是噪声   → 补进白名单（下次不再浮出）。
# ---------------------------------------------------------------------------
LIVE_DOCS = [
    "AGENTS.md",
    "README.md",
    "tools/README.md",
    ".agents/skills/self-audit/SKILL.md",
    ".agents/skills/self-audit/assets/run-log-template.md",
    ".agents/skills/insight-synthesis/SKILL.md",
    ".agents/skills/hypothesis-paper-audit/SKILL.md",
    ".agents/skills/hypothesis-paper-audit/assets/run-log-template.md",
]
CLAIM_RE = re.compile(r"(\d+)\s*(维|条|个|刊|本|篇|项|层|步|区|类|份)")
# 只认阿拉伯数字：散文里的中文数词绝大多数是泛指（「一个章节」「三条铁律」），
# 真正的中文数字声明（「十四维」）已被 R1 声明点覆盖；普查的职责是捞漏网的，不是复述已知的。
ORDINAL_PREFIX_RE = re.compile(r"第[\s\d、,，/]*$")   # 「第 9 维」「第 1、2 步」「第 13/14 维」：序数引用不是数量声明
RANGE_PREFIX_RE = re.compile(r"\d\s*[-–~—]\s*$")     # 「2-3 个」：区间是经验法则不是定点值
IDENT_PREFIX_RE = re.compile(r"[A-Za-z0-9]$")        # 「MRTX1133 类」：标识符片段不是数量声明
ORPHAN_ALLOWLIST = [re.compile(p) for p in (
    r"第\s*.{0,3}步",            # 步骤编号是顺序记号，不是事实
    r"由 \d+ 个项目技能（",       # 历史叙事：2026-10-01 扩库前的真实起点
    r"旧\s*\d+\s*维",            # 历史叙事：解释某维度为何新增时的旧口径
    r"扫描:Crossref \d+篇",       # tools/README 用法示例里的占位数字
    r"R\d\s*层级跳跃",           # 引擎规则编号引用（R2 层级跳跃）
    r"拆成 \d+ 条",              # insight-synthesis 里的举例说明
    r"(?:只有|只挂)\s*\**\s*1 条",  # ≥2 条 F 规则的反面表述（违规情形描述）
    r"≥\d+ 条 F 独立支持",        # 趋势汇聚阈值：单处定义点，无副本可漂移
    r"≥\d+ 条带 DOI",            # 投稿质量门槛：单处定义点，无副本可漂移
)]


def r3_claim_census():
    """返回 (orphans, total, covered)。orphans 每项为 '文件:行号 「声明」 …上下文…'。"""
    site_res = [re.compile(site["re"]) for fact in FACTS for site in fact["sites"]]
    # 源头文件里复述自身真值的位置也算已覆盖（源头不是孤儿，如 paper-audit SKILL.md 自述「当前 15 维」）
    src_res = {}
    for fact in FACTS:
        s = fact["source"]
        if "re" in s:
            src_res.setdefault(s["file"], []).append(re.compile(s["re"]))
    orphans, total, covered = [], 0, 0
    for rel in LIVE_DOCS:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        text = read_text(path)
        spans = [(m.start(), m.end()) for pat in site_res + src_res.get(rel, [])
                 for m in pat.finditer(text)]
        for m in CLAIM_RE.finditer(text):
            total += 1
            if any(m.start() < e and s < m.end() for s, e in spans):
                covered += 1
                continue
            prefix = text[max(0, m.start() - 20):m.start()]
            if ORDINAL_PREFIX_RE.search(prefix) or RANGE_PREFIX_RE.search(prefix) \
                    or IDENT_PREFIX_RE.search(prefix):
                continue
            ctx = text[max(0, m.start() - 14):m.end() + 14].replace("\n", " ")
            if any(p.search(ctx) for p in ORPHAN_ALLOWLIST):
                continue
            line_no = text[:m.start()].count("\n") + 1
            orphans.append("%s:%d 「%s」 …%s…" % (rel, line_no, m.group(0), ctx.strip()))
    return orphans, total, covered


def read_text(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def ast_value(path, spec):
    """从 Python 源码里取结构性真值（如 len(CANONICAL_TOPICS) / POOL_MAX）。
    len 模式：字面量集合/列表直接数；字典容非字面量值（如 detections 的函数调用），按键数计。"""
    mode, name = spec.split(":", 1)
    tree = ast.parse(read_text(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    if mode == "len":
                        try:
                            return len(ast.literal_eval(node.value))
                        except (ValueError, SyntaxError):
                            if isinstance(node.value, ast.Dict):
                                return len(node.value.keys)
                            raise
                    return ast.literal_eval(node.value)
    return None


def py_value(spec):
    """'mod.func' → 导入同目录脚本并调用，取 len(返回值)。用于真值在另一脚本解析结果里的情形。
    dont_write_bytecode：导入会产生 __pycache__，而 R2 正查它——不能自己制造自己要抓的垃圾。"""
    mod_name, func_name = spec.rsplit(".", 1)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.dont_write_bytecode = True
    import importlib
    return len(getattr(importlib.import_module(mod_name), func_name)())


def r1_derived_facts():
    """返回 (findings, verdict)。findings 每项：{fact, ok, level, detail}。"""
    findings = []
    level = 0
    for fact in FACTS:
        src = fact["source"]
        src_path = os.path.join(ROOT, src["file"])
        if not os.path.exists(src_path):
            findings.append({"fact": fact["label"], "ok": False, "level": 1,
                             "detail": "源头文件缺失：%s" % src["file"]})
            level = max(level, 1)
            continue
        if "ast" in src:
            truth = ast_value(src_path, src["ast"])
        elif "dir" in src:
            truth = len([d for d in os.listdir(src_path)
                         if os.path.isdir(os.path.join(src_path, d))])
        elif "count_re" in src:
            truth = len(re.findall(src["count_re"], read_text(src_path), re.M))
        elif "py" in src:
            truth = py_value(src["py"])
        elif "json_len" in src:
            node = json.loads(read_text(src_path))
            for k in src["json_len"].split("."):
                node = node[k]
            truth = len(node)
        else:
            m = re.search(src["re"], read_text(src_path))
            truth = cn_to_int(m.group(1)) if m else None
        if truth is None:
            findings.append({"fact": fact["label"], "ok": False, "level": 1,
                             "detail": "源头读不出真值（正则未命中）：%s" % src["file"]})
            level = max(level, 1)
            continue

        for site in fact["sites"]:
            site_path = os.path.join(ROOT, site["file"])
            if not os.path.exists(site_path):
                findings.append({"fact": fact["label"], "ok": False, "level": 1,
                                 "detail": "声明点文件缺失：%s" % site["file"]})
                level = max(level, 1)
                continue
            text = read_text(site_path)
            ms = list(re.finditer(site["re"], text))
            if not ms:
                findings.append({"fact": fact["label"], "ok": False, "level": 1,
                                 "detail": "%s：声明点正则未命中，registry 需同步（脚本检查项也会腐烂）"
                                           % site["file"]})
                level = max(level, 1)
                continue
            for m in ms:
                got = cn_to_int(m.group(1))
                line_no = text[:m.start()].count("\n") + 1
                if got != truth:
                    findings.append({
                        "fact": fact["label"], "ok": False, "level": 2,
                        "detail": "%s:%d 写「%s」＝%d，源头（%s）＝%d —— 漂移"
                                  % (site["file"], line_no, m.group(1), got, src["file"], truth),
                    })
                    level = max(level, 2)
                else:
                    findings.append({"fact": fact["label"], "ok": True, "level": 0,
                                     "detail": "%s:%d ＝ %d，与源头一致" % (site["file"], line_no, got)})
    return findings, level


# ---------------------------------------------------------------------------
# 维度编号映射校验（2026-10-17，源于 L047 维度号写错但 R1 漏抓）
#
# 背景：R1 只校验「维度总数」不校验「编号↔名称」映射。L047 把「论文姊妹线」写成
# 第 16 维（实际第 15 维），总数没错、映射错了，R1 完全看不见。本条从 SKILL.md 评分表
# 解析出「编号 → 维度名」的单一真相源，再扫 lessons.md 里所有「第N维「名称」」引用比对：
#   · 编号不在评分表          → 失败（2）：漂移
#   · 编号在但名称不符        → 失败（2）：编号↔名称映射漂移（正是 L047 那种）
#   · 「第N维」裸编号（不带名称）→ 告警（1）：不自描述，应补「名称」防漂移
# 纪律：lessons.md 引用维度一律写「第N维「名称」」，禁止裸编号。
# ---------------------------------------------------------------------------
SKILL_SELF_AUDIT = os.path.join(ROOT, ".agents", "skills", "self-audit", "SKILL.md")
DIM_ROW_RE = re.compile(r"^\|\s*(\d+)\.\s*([^|（(]+)", re.M)
DIM_NAME_RE = re.compile(r"第\s*(\d+)\s*维\s*[「『]\s*([^」』]+?)\s*[」』]")
DIM_BARE_RE = re.compile(r"第\s*(\d+)\s*维(?!\s*[「『])")


def parse_dim_map():
    """从 self-audit SKILL.md 评分表解析 {编号: 维度名}（名称去掉「（自动）」等括号后缀）。"""
    if not os.path.exists(SKILL_SELF_AUDIT):
        return None
    text = read_text(SKILL_SELF_AUDIT)
    dim_map = {}
    for m in DIM_ROW_RE.finditer(text):
        no = int(m.group(1))
        name = re.split(r"[（(]", m.group(2))[0].strip()
        dim_map[no] = name
    return dim_map


def r1b_dim_map():
    """校验 lessons.md 的维度引用「编号↔名称」映射一致。返回 (findings, level)。"""
    dim_map = parse_dim_map()
    if not dim_map:
        return [{"level": 1, "detail": "解析不到 self-audit SKILL.md 评分表维度映射"}], 1
    path = os.path.join(ROOT, "learning", "lessons.md")
    if not os.path.exists(path):
        return [{"level": 1, "detail": "learning/lessons.md 缺失"}], 1
    text = read_text(path)
    findings, level = [], 0
    covered = []
    for m in DIM_NAME_RE.finditer(text):
        covered.append((m.start(), m.end()))
        no = int(m.group(1))
        name = m.group(2).strip()
        line_no = text[:m.start()].count("\n") + 1
        if no not in dim_map:
            findings.append({"level": 2, "detail":
                "lessons.md:%d 引用「第%d维「%s」」，评分表无此编号——维度编号漂移" % (line_no, no, name)})
            level = max(level, 2)
        elif dim_map[no] != name:
            findings.append({"level": 2, "detail":
                "lessons.md:%d 写「第%d维「%s」」，评分表第%d维实为「%s」——编号↔名称映射漂移"
                % (line_no, no, name, no, dim_map[no])})
            level = max(level, 2)
        else:
            findings.append({"level": 0, "detail":
                "lessons.md:%d 第%d维「%s」一致" % (line_no, no, name)})
    for m in DIM_BARE_RE.finditer(text):
        if any(s <= m.start() < e for s, e in covered):
            continue
        no = int(m.group(1))
        line_no = text[:m.start()].count("\n") + 1
        findings.append({"level": 1, "detail":
            "lessons.md:%d 「第%d维」裸编号引用（未带「名称」）——应补「名称」防漂移" % (line_no, no)})
        level = max(level, 1)
    return findings, level


def r2_ignored_regions():
    """扫文件系统（含 .gitignore 覆盖区），返回 (buckets, verdict)。"""
    buckets = {"tmp_leftovers": [], "backup_files": [], "pycache_dirs": []}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel = os.path.relpath(dirpath, ROOT)
        if rel == ".":
            rel = ""
        # 剪枝：跳过 .git / 虚拟环境 / 缓存目录（缓存目录本身作为残留被记录）
        keep = []
        for d in dirnames:
            if d in SKIP_DIR_NAMES:
                continue
            if d in JUNK_DIR_NAMES:
                buckets["pycache_dirs"].append(os.path.join(rel, d) if rel else d)
                continue
            keep.append(d)
        dirnames[:] = keep

        for fn in filenames:
            full_rel = os.path.join(rel, fn) if rel else fn
            if full_rel.split(os.sep, 1)[0] == "tmp":
                buckets["tmp_leftovers"].append(full_rel)
                continue
            for pat in JUNK_FILE_RES:
                if pat.search(fn):
                    buckets["backup_files"].append(full_rel)
                    break
    verdict = 0
    if any(buckets.values()):
        verdict = 1
    return buckets, verdict


def main():
    ap = argparse.ArgumentParser(description="内向敌对自审：派生事实重算 + 忽略区扫描（审计第 14 维）")
    ap.add_argument("--json", help="把结果写到该 JSON 路径")
    args = ap.parse_args()

    print("=" * 68)
    print("内向敌对自审（审计第 14 维）：R1/R1b 派生事实重算 + R2 忽略区扫描 + R3 数字普查")
    print("=" * 68)

    # --- R1 ---
    print("\n[R1] 派生事实重算（源头 → 声明点比对；历史运行日志不作声明点）")
    findings, v1 = r1_derived_facts()
    drift = [f for f in findings if f["level"] == 2]
    stale = [f for f in findings if f["level"] == 1]
    if not drift and not stale:
        print("    OK（%d 个事实 / %d 个声明点，全部与源头一致）"
              % (len(FACTS), len([f for f in findings if f["ok"]])))
    for f in findings:
        if f["level"] == 0:
            continue
        tag = "【漂移】" if f["level"] == 2 else "【待同步】"
        print("    %s %s" % (tag, f["detail"]))
    if drift:
        print("    → 漂移是「根本目标·文档必须和事实一致」的硬伤：改源头或改副本，二者必居其一。")

    # --- R1b ---
    print("\n[R1b] 维度编号映射校验（从 self-audit 评分表解析「编号→名称」，反向校验 lessons.md 引用）")
    dim_findings, v1b = r1b_dim_map()
    dim_drift = [f for f in dim_findings if f["level"] == 2]
    dim_bare = [f for f in dim_findings if f["level"] == 1]
    if not dim_drift and not dim_bare:
        print("    OK（%d 处维度引用全部与评分表「编号↔名称」一致）"
              % len([f for f in dim_findings if f["level"] == 0]))
    for f in dim_findings:
        if f["level"] == 0:
            continue
        tag = "【漂移】" if f["level"] == 2 else "【待同步】"
        print("    %s %s" % (tag, f["detail"]))
    if dim_drift:
        print("    → 编号对但名称错 / 编号不存在：总数校验看不见的映射漂移，必须当场改。")

    # --- R2 ---
    print("\n[R2] 忽略区扫描（直接走文件系统，含 .gitignore 覆盖区）")
    buckets, v2 = r2_ignored_regions()
    if v2 == 0:
        print("    OK（tmp/ 空、无 *.bak-*、无 __pycache__）")
    else:
        if buckets["tmp_leftovers"]:
            print("    tmp/ 残留 %d 个文件（AGENTS.md：中间产物当日任务结束整个删除）" % len(buckets["tmp_leftovers"]))
            for p in buckets["tmp_leftovers"][:10]:
                print("        %s" % p)
            if len(buckets["tmp_leftovers"]) > 10:
                print("        …（其余 %d 个略）" % (len(buckets["tmp_leftovers"]) - 10))
        if buckets["backup_files"]:
            print("    备份/临时文件 %d 个（*.bak-* / *.pyc / *.orig …）" % len(buckets["backup_files"]))
            for p in buckets["backup_files"][:10]:
                print("        %s" % p)
            if len(buckets["backup_files"]) > 10:
                print("        …（其余 %d 个略）" % (len(buckets["backup_files"]) - 10))
        if buckets["pycache_dirs"]:
            print("    __pycache__ 目录 %d 个（第 6 步清理应清空）" % len(buckets["pycache_dirs"]))
            for p in buckets["pycache_dirs"][:10]:
                print("        %s" % p)
            if len(buckets["pycache_dirs"]) > 10:
                print("        …（其余 %d 个略）" % (len(buckets["pycache_dirs"]) - 10))
        print("    → 这些都在 .gitignore 里，git status 看不见；瘦身纪律「发现就清」必须扫这里。")

    # --- R3 ---
    print("\n[R3] 数字普查（live 文档全量「数字+单位」声明 → 已覆盖 / 孤儿）")
    orphans, n_claims, n_covered = r3_claim_census()
    v3 = 1 if orphans else 0
    if not orphans:
        print("    OK（%d 个数字声明：%d 个被 registry 覆盖，其余命中噪声白名单）" % (n_claims, n_covered))
    else:
        print("    已覆盖 %d / %d；孤儿 %d 个——查不到源头，人工勾选：补进 FACTS 或确认噪声入白名单"
              % (n_covered, n_claims, len(orphans)))
        for o in orphans[:20]:
            print("        %s" % o)
        if len(orphans) > 20:
            print("        …（其余 %d 个略）" % (len(orphans) - 20))

    verdict = max(v1, v1b, v2, v3)
    warnings, errors = [], []
    if drift:
        errors.append("R1: %d 处派生标识漂移" % len(drift))
    if stale:
        warnings.append("R1: %d 处声明点待同步" % len(stale))
    if dim_drift:
        errors.append("R1b: %d 处维度编号↔名称映射漂移" % len(dim_drift))
    if dim_bare:
        warnings.append("R1b: %d 处裸维度编号引用待补「名称」" % len(dim_bare))
    if v2 == 1:
        warnings.append("R2: 忽略区有残留（tmp %d / 备份 %d / pycache %d）"
                        % (len(buckets["tmp_leftovers"]), len(buckets["backup_files"]),
                           len(buckets["pycache_dirs"])))
    if v3 == 1:
        warnings.append("R3: %d 个孤儿数字声明待勾选（已覆盖 %d/%d）"
                        % (len(orphans), n_covered, n_claims))

    label = {0: "通过", 1: "告警", 2: "失败"}[verdict]
    print("\n" + "-" * 68)
    print("结论：%s（退出码 %d）" % (label, verdict))
    if warnings:
        print("  告警项：" + "；".join(warnings))
    if errors:
        print("  失败项：" + "；".join(errors))
    if verdict == 0:
        print("  含义：派生标识无漂移、忽略区干净、数字声明全部可溯源。清单与事实一致。")
    elif verdict == 1:
        print("  含义：有该清未清的垃圾 / registry 待同步 / 孤儿数字声明待勾选——不是系统故障，是纪律没执行。")
    elif verdict == 2:
        print("  含义：文档复述了与事实不符的值（或事实本身自相矛盾）——必须当场改，不许留。")

    if args.json:
        payload = {
            "r1_findings": findings,
            "r1_drift": [f["detail"] for f in drift],
            "r1_stale": [f["detail"] for f in stale],
            "r1b_findings": dim_findings,
            "r1b_drift": [f["detail"] for f in dim_drift],
            "r1b_bare": [f["detail"] for f in dim_bare],
            "r2_buckets": buckets,
            "r3_claims": n_claims,
            "r3_covered": n_covered,
            "r3_orphans": orphans,
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
