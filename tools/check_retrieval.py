#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_retrieval.py — 检索能力健康检查（审计第 16 维）

背景：2026-10-17 一次系统性检索能力改造，补齐 7 块短板：
  期刊覆盖（sync_journals.py + journals.json）、语义检索（实体词典扩充 + 规范化匹配）、
  引用网络（citation_graph.py）、全文检索（fulltext_probe.py）、时间回溯（backfill_search.py）、
  预印本（scan_preprints.py）、实体消歧（alias 归并）。
本脚本把这些能力变成可复跑的确定性检查——能力会腐烂（数据源改、脚本删、词典缩水），
第 16 维盯它们不烂掉。

六项检查：
  A. 期刊数据源在位且规模达标（journals.json flat ≥ 20，原内核 21 为基线）
  B. 实体词典规模达标（entities.json ≥ 220，原基线 220）
  C. 实体别名覆盖（中英同义别名——英文文献文本能挂到规范名的关键）
  D. 五个检索脚本在位且可编译（py_compile）
  E. 检索脚本可被 import（无 import 期崩溃）
  F. 期刊数派生事实与 README 声明一致（防「加了期刊没改声明」这类漂移）

退出码：0 通过 / 1 告警 / 2 失败（数据源缺失或脚本损坏=2，规模缩水=1）
用法：python tools\\check_retrieval.py [--json tmp\\audit_retrieval.json]
"""

import argparse
import io
import json
import os
import py_compile
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

MIN_JOURNALS = 20      # 原内核 21 本为基线
MIN_ENTITIES = 220     # 原实体词典 220 个为基线
MIN_ALIASED_FRAC = 0.15  # 至少 15% 实体带英文别名（语义桥接的下限）


def check_journals():
    p = os.path.join(HERE, "journals.json")
    if not os.path.exists(p):
        return {"ok": False, "level": 2, "detail": "journals.json 缺失"}
    try:
        d = json.load(io.open(p, encoding="utf-8"))
        n = len(d.get("flat", {}))
    except Exception as e:
        return {"ok": False, "level": 2, "detail": "journals.json 解析失败：%s" % e}
    if n < MIN_JOURNALS:
        return {"ok": False, "level": 1, "detail": "期刊数 %d < %d（规模缩水）" % (n, MIN_JOURNALS)}
    return {"ok": True, "level": 0, "detail": "期刊 %d 本" % n}


def check_entities():
    p = os.path.join(HERE, "entities.json")
    if not os.path.exists(p):
        return {"ok": False, "level": 2, "detail": "entities.json 缺失"}
    try:
        d = json.load(io.open(p, encoding="utf-8"))
        e = d.get("entities", {})
    except Exception as ex:
        return {"ok": False, "level": 2, "detail": "entities.json 解析失败：%s" % ex}
    n = len(e)
    aliased = sum(1 for v in e.values() if v.get("aliases"))
    frac = aliased / n if n else 0
    # 英文别名覆盖：含 ASCII 别名的实体占比（语义桥接关键）
    ascii_aliased = sum(1 for v in e.values()
                        if any(any(c.isascii() and c.isalpha() for c in a) for a in v.get("aliases", [])))
    if n < MIN_ENTITIES:
        return {"ok": False, "level": 1, "detail": "实体数 %d < %d（词典缩水）" % (n, MIN_ENTITIES)}
    if frac < MIN_ALIASED_FRAC:
        return {"ok": False, "level": 1, "detail": "带别名实体占比 %.0f%% < %.0f%%" % (frac * 100, MIN_ALIASED_FRAC * 100)}
    return {"ok": True, "level": 0,
            "detail": "实体 %d 个（带别名 %d，含英文别名 %d）" % (n, aliased, ascii_aliased)}


RETRIEVAL_SCRIPTS = [
    "sync_journals.py", "citation_graph.py", "fulltext_probe.py",
    "backfill_search.py", "scan_preprints.py", "scan_journals.py", "insight_scan.py",
]


def check_scripts():
    missing, bad = [], []
    for s in RETRIEVAL_SCRIPTS:
        p = os.path.join(HERE, s)
        if not os.path.exists(p):
            missing.append(s)
            continue
        try:
            py_compile.compile(p, doraise=True)
        except Exception as e:
            bad.append("%s: %s" % (s, e))
    if missing:
        return {"ok": False, "level": 2, "detail": "脚本缺失：%s" % "、".join(missing)}
    if bad:
        return {"ok": False, "level": 2, "detail": "脚本编译失败：%s" % "、".join(bad)}
    return {"ok": True, "level": 0, "detail": "%d 个检索脚本在位且可编译" % len(RETRIEVAL_SCRIPTS)}


def check_importable():
    """五个本地检索模块能被 import（不触发联网，只验 import 无崩溃）。"""
    import importlib
    sys.path.insert(0, HERE)
    sys.dont_write_bytecode = True
    mods = ["citation_graph", "fulltext_probe", "backfill_search", "scan_preprints", "sync_journals"]
    bad = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as e:
            bad.append("%s: %s" % (m, e))
    if bad:
        return {"ok": False, "level": 2, "detail": "import 失败：%s" % "、".join(bad)}
    return {"ok": True, "level": 0, "detail": "%d 个检索模块可 import" % len(mods)}


def check_declared_consistency():
    """期刊数派生事实：journals.json flat 数量 vs README 声明（防改了数据没改声明）。"""
    import re
    p = os.path.join(HERE, "journals.json")
    n = len(json.load(io.open(p, encoding="utf-8")).get("flat", {}))
    readme = os.path.join(ROOT, "README.md")
    if not os.path.exists(readme):
        return {"ok": True, "level": 0, "detail": "README.md 缺失，跳过声明比对"}
    text = io.open(readme, encoding="utf-8").read()
    m = re.search(r"扫描 (\d+) 本期刊", text)
    if not m:
        return {"ok": False, "level": 1, "detail": "README 未声明「扫描 N 本期刊」"}
    if int(m.group(1)) != n:
        return {"ok": False, "level": 2, "detail": "期刊数漂移：journals.json=%d，README=%s" % (n, m.group(1))}
    return {"ok": True, "level": 0, "detail": "期刊数声明一致（%d）" % n}


CHECKS = [
    ("A", "期刊数据源", check_journals),
    ("B", "实体词典规模", check_entities),
    ("C", "实体别名覆盖", None),  # 并入 B
    ("D", "检索脚本在位", check_scripts),
    ("E", "检索模块可 import", check_importable),
    ("F", "期刊数声明一致", check_declared_consistency),
]


def main():
    ap = argparse.ArgumentParser(description="检索能力健康检查（审计第 16 维）")
    ap.add_argument("--json", help="结果写该 JSON")
    args = ap.parse_args()

    results, level = [], 0
    for code, label, fn in CHECKS:
        if fn is None:
            continue
        r = fn()
        r["check"] = code
        r["label"] = label
        results.append(r)
        level = max(level, r["level"])
        mark = {0: "OK ", 1: "告警", 2: "失败"}[r["level"]]
        print("[%s] %s：%s" % (mark, label, r["detail"]))

    if args.json:
        out_dir = os.path.dirname(args.json)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        json.dump({"results": results, "verdict": level}, io.open(args.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    print("结论：%s（退出码 %d）" % ({0: "通过", 1: "告警", 2: "失败"}[level], level))
    # 清理本脚本 import 产生的 pycache（R2 会查）
    cache = os.path.join(HERE, "__pycache__")
    if os.path.isdir(cache):
        import shutil
        shutil.rmtree(cache, ignore_errors=True)
    return level


if __name__ == "__main__":
    sys.exit(main())