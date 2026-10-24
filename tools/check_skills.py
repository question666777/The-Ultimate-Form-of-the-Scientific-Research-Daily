#!/usr/bin/env python3
"""技能库体检（skills health check）

固化清单：
L040 技能库必须按职能分类、并按依赖就绪度与激活率定期体检，否则储备层会退化成没人用的死库。
L041 技能按职能分类之后必须再给「场景 → 调用顺序」的作业线配方，否则分类只是标签、用的时候还是凭感觉抓。

职责：
  1. 分类覆盖——每个技能目录在 tools\\skills_taxonomy.json 里都要有职能角色
  2. 核心层完整性——AGENTS.md 技能协同矩阵里登记的核心层技能必须全部在位且非空
  3. 依赖就绪度——扫各技能 scripts\\ 的 import，判定 开箱即用 / 纯文档 / 缺依赖
  4. 激活近似——扫 learning\\runs\\*.md 统计技能被提及次数，供 self-audit 第 4 维取证
  5. 作业线配方——校验 tools\\skill_playbooks.json 引用的技能都存在，并解析出实际可用者

用法：
  python tools\\check_skills.py [--json tmp\\audit_skills.json] [--quiet] [--playbooks]

退出码：0 通过 / 1 告警 / 2 失败
"""

import argparse
import ast
import json
import os
import re
import sys
from importlib.util import find_spec

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(ROOT, ".agents", "skills")
TAXONOMY = os.path.join(ROOT, "tools", "skills_taxonomy.json")
PLAYBOOKS = os.path.join(ROOT, "tools", "skill_playbooks.json")
AGENTS = os.path.join(ROOT, "AGENTS.md")
RUNS_DIR = os.path.join(ROOT, "learning", "runs")

# 核心层里像 diagram-design 这类技能挂在全局技能目录，不在项目 .agents\skills\ 下，
# 缺位判定必须把外部技能源算进来，否则每天误报失败
EXTERNAL_ROOTS = [
    os.path.join(os.path.expanduser("~"), ".trae-cn", "skills"),
    os.path.join(ROOT, ".trae", "skills"),
]

STDLIB = set(sys.stdlib_module_names)


def load_taxonomy():
    with open(TAXONOMY, encoding="utf-8") as fh:
        return json.load(fh)["skills"]


def load_playbooks():
    if not os.path.isfile(PLAYBOOKS):
        return {}
    with open(PLAYBOOKS, encoding="utf-8") as fh:
        return json.load(fh).get("playbooks", {})


def external_skills():
    out = set()
    for r in EXTERNAL_ROOTS:
        if os.path.isdir(r):
            out |= {d for d in os.listdir(r) if os.path.isdir(os.path.join(r, d))}
    return out


def playbook_steps(pb):
    """把一条作业线展开成 [(stage, role, [候选技能], need)]，branch 逐条展开。"""
    out = []
    for st in pb.get("steps", []):
        if "branch" in st:
            for label, names in st["branch"].items():
                out.append((f"{st['stage']}（{label}）", st.get("role", ""), names, st.get("need", "")))
        else:
            out.append((st.get("stage", ""), st.get("role", ""), st.get("prefer", []), st.get("need", "")))
    return out


def core_skills():
    """从 AGENTS.md 技能协同矩阵表里解析核心层技能（L038：从数据派生，不手填）。"""
    core = set()
    with open(AGENTS, encoding="utf-8") as fh:
        for line in fh:
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.split("|")]
            if len(cells) < 4 or not cells[1].endswith("层"):
                continue
            for name in re.split(r"[、,]", cells[2]):
                name = re.sub(r"[※*†‡\s]+$", "", name.strip())  # 剥离矩阵脚注标记（※ 等），否则核心层名带尾标误判缺位
                if name and name != "技能":
                    core.add(name)
    return core


def local_modules(sdir):
    names = set()
    for dp, dn, fn in os.walk(sdir):
        for f in fn:
            if f.endswith(".py") and f != "__init__.py":
                names.add(f[:-3])
        names.update(dn)
    return names


def imports_of(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            tree = ast.parse(fh.read())
    except Exception:
        return set()
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


def scan_skill(sdir):
    local = local_modules(sdir)
    mods, n_py = set(), 0
    for dp, _dn, fn in os.walk(sdir):
        for f in fn:
            if f.endswith(".py"):
                n_py += 1
                mods |= imports_of(os.path.join(dp, f))
    third = sorted(m for m in mods
                   if m not in STDLIB and m not in local and not m.startswith("_"))
    missing = []
    for m in third:
        try:
            if find_spec(m) is None:
                missing.append(m)
        except (ImportError, ValueError):
            missing.append(m)
    status = "doc-only" if n_py == 0 else ("need-deps" if missing else "ready")
    return status, n_py, missing


def activation(names):
    """扫运行日志，统计技能被提及的次数（近似激活度）。"""
    hits = {n: 0 for n in names}
    if not os.path.isdir(RUNS_DIR):
        return hits, 0
    logs = [f for f in sorted(os.listdir(RUNS_DIR)) if f.endswith(".md")]
    for f in logs:
        with open(os.path.join(RUNS_DIR, f), encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
        for n in names:
            hits[n] += len(re.findall(r"\b" + re.escape(n) + r"\b", text))
    return hits, len(logs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--playbooks", action="store_true",
                    help="打印作业线配方（场景 → 调用顺序 → 实际解析出的技能）")
    args = ap.parse_args()

    taxonomy = load_taxonomy()
    core = core_skills()
    skills = sorted(d for d in os.listdir(SKILLS_DIR)
                    if os.path.isdir(os.path.join(SKILLS_DIR, d)))

    fails, warns, report = [], [], {}
    buckets = {"ready": [], "doc-only": [], "need-deps": []}

    for s in skills:
        sdir = os.path.join(SKILLS_DIR, s)
        has_skill_md = os.path.isfile(os.path.join(sdir, "SKILL.md"))
        n_files = sum(len(f) for _, _, f in os.walk(sdir))
        status, n_py, missing = scan_skill(sdir)
        role = taxonomy.get(s, {}).get("role", "未归类")

        if not has_skill_md:
            fails.append(f"{s}: 缺 SKILL.md")
        if n_files == 0:
            fails.append(f"{s}: 目录为空")
        if role in ("未归类", "未收录"):
            fails.append(f"{s}: 未归入任何职能角色")
        buckets[status].append(s)
        report[s] = {
            "role": role,
            "category": taxonomy.get(s, {}).get("category", ""),
            "status": status,
            "n_py": n_py,
            "missing": missing,
            "core": s in core,
        }

    external = external_skills()
    missing_core = sorted(core - set(skills) - external)
    for c in missing_core:
        fails.append(f"核心层技能 {c} 既不在项目技能目录也不在外部技能源中")

    available = set(skills) | external
    playbooks = load_playbooks()
    pb_report = {}
    for pb_name, pb in playbooks.items():
        chain = []
        for stage, role, names, need in playbook_steps(pb):
            pick = next((n for n in names if n in available), None)
            manual = not names  # 空 prefer ＝ 该环节无对应技能，由人工/脚本承担（不算失效）
            if pick is None and not manual:
                fails.append(f"作业线「{pb_name}」{stage}：候选技能全部不存在（{ '、'.join(names) }）")
            chain.append({"stage": stage, "role": role, "pick": pick,
                          "manual": manual, "candidates": names, "need": need})
        pb_report[pb_name] = {"when": pb.get("when", ""), "rule": pb.get("rule", ""), "chain": chain}

    hits, n_logs = activation(skills)
    idle_core = sorted(c for c in core if c in hits and hits[c] == 0)

    roles = {}
    for s, v in report.items():
        roles.setdefault(v["role"], []).append(s)

    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"skills": report, "roles": roles,
                       "playbooks": pb_report,
                       "fails": fails, "warns": warns,
                       "activation": hits, "run_logs": n_logs},
                      fh, ensure_ascii=False, indent=1)

    if not args.quiet:
        print(f"技能库体检：{len(skills)} 个技能，{len(roles)} 类职能")
        print(f"  分类覆盖：{len(skills) - sum(1 for v in report.values() if v['role'] in ('未归类', '未收录'))}/{len(skills)} 已归类")
        ext_core = sorted((core & external) - set(skills))
        in_project = len(core) - len(missing_core) - len(ext_core)
        print(f"  核心层  ：{len(core)} 个＝项目内 {in_project} / 外部技能源 {len(ext_core)}"
              + (f"（{ '、'.join(ext_core) }）" if ext_core else "")
              + f"，缺位 {len(missing_core)} 个")
        print(f"  就绪状态：开箱即用 {len(buckets['ready'])} / 纯文档 {len(buckets['doc-only'])} / 缺依赖 {len(buckets['need-deps'])}")
        print(f"  激活近似：基于 {n_logs} 份运行日志，核心层零提及 {len(idle_core)} 个")
        bad_pb = sum(1 for v in pb_report.values()
                     for c in v["chain"] if c["pick"] is None and not c.get("manual"))
        manual_pb = sum(1 for v in pb_report.values()
                        for c in v["chain"] if c.get("manual"))
        print(f"  作业线  ：{len(pb_report)} 条配方，引用失效 {bad_pb} 处，人工环节 {manual_pb} 处")
        print()
        for r, ss in sorted(roles.items(), key=lambda x: -len(x[1])):
            print(f"  [{r}] {len(ss)} 个")
        if args.playbooks:
            for name, v in pb_report.items():
                print()
                print(f"■ {name}")
                if v["when"]:
                    print(f"  适用：{v['when']}")
                if v["rule"]:
                    print(f"  铁律：{v['rule']}")
                for c in v["chain"]:
                    if c["pick"]:
                        mark = c["pick"]
                    elif c.get("manual"):
                        mark = "（人工/脚本，无技能）"
                    else:
                        mark = "【缺失】" + "、".join(c["candidates"])
                    print(f"  {c['stage']:<22} [{c['role']}] {mark}")
                    if c["need"]:
                        print(f"  {'':<22} └ {c['need']}")
        if fails:
            print()
            print("失败项：")
            for f in fails:
                print("  -", f)
        if idle_core:
            print()
            print("告警：核心层技能在运行日志中零提及（可能被绕过）：")
            print("  -", "、".join(idle_core))

    code = 2 if fails else (1 if idle_core else 0)
    return code


if __name__ == "__main__":
    sys.exit(main())