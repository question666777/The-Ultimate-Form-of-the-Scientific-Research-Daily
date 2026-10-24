#!/usr/bin/env python3
"""记忆库工具（learning/memory/findings.md）

固化清单：
L042 记忆库必须每期写入、并在选题与深挖前检索，否则回填一次就成死档。

职责：
  check     格式与质量合规 + 概览统计（第 7 步审计调用，退出码 0/1/2）
  search    按主题 / 标签 / 关键词 / 日期 / 编号检索（选题、深挖、写联系时调用）
  ingest    从 report_data.json 批量入库本期结论（第 4 步生成后自动调用）
  add       追加单条条目（finding / link / question），ID 自动分配，同 DOI 查重
  renumber  按文件顺序重排 F / C / Q 编号并同步引用（ID 派生，禁止手填）

用法：
  python tools\\memory.py check [--json tmp\\memory.json]
  python tools\\memory.py search --topic 免疫学 --tag 机制 --since 2026-09-25
  python tools\\memory.py search --id F034 F084
  python tools\\memory.py search --kw STING --limit 10
  python tools\\memory.py ingest --data tmp\\report_data.json [--date 2026-10-01] [--dry-run]
  python tools\\memory.py add --topic 代谢与营养生物学 --conf 中 --tags 机制,干预 \\
      --text "一句话结论" --doi 10.1038/s41591-026-04651-9 [--date 2026-10-01] [--again 09-30]
  python tools\\memory.py add --type question --text "什么把 A 与 B 连接起来？" \\
      --refs F051,F108 --cand "C004（心磷脂桥）" [--status open]
  python tools\\memory.py renumber [--write]

ingest 读取 report_data.json 的 chapters[*].papers[*]，逐篇取 one_liner / doi / conf / tags；
paper 缺 conf 默认「中」、缺 tags 留空并列出待人工补，同 DOI 已在库则追加「（再报 日期）」。

问题条目（Q）：给「未知」一等公民，由 R1 桥接 / R2 缺口 / R3 张力 / R4 冗余触发，
状态 open / explained / falsified，必须挂 ≥1 条触发 F；格式见 reason_engine.md 第四节。

退出码：0 通过 / 1 告警 / 2 失败
"""

import argparse
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FINDINGS = os.path.join(ROOT, "learning", "memory", "findings.md")

TAGS = ["机制", "干预", "诊断", "标志物", "工具方法", "临床证据", "跨物种", "安全信号"]
CONF = ["高", "中", "低"]
MAX_TEXT = 60

F_RE = re.compile(r"^- \*\*\[(F\d{3})?\]\*\*（(\d{4}-\d{2}-\d{2})｜([^｜]+)｜(高|中|低)｜([^）]*)）(.+)$")
C_RE = re.compile(r"^- \*\*\[(C\d{3})?\]\*\*（([^）]+)）(.+)$")
Q_RE = re.compile(r"^- \*\*\[(Q\d{3})?\]\*\*（(\d{4}-\d{2}-\d{2})｜(open|explained|falsified)｜触发：([^）]+)）(.+)$")
CAND_RE = re.compile(r"^\s+- 候选解释：(.*)$")
QSTATUS = ("open", "explained", "falsified")
ANY_RE = re.compile(r"^- \*\*\[(?:[FCQ]\d{3})?\]\*\*")


def read():
    if not os.path.isfile(FINDINGS):
        return ""
    return io.open(FINDINGS, encoding="utf-8").read()


def parse(text):
    findings, links, questions, bad = [], [], [], []
    lines = text.split("\n")
    for ln, line in enumerate(lines, 1):
        line = line.rstrip()
        if not ANY_RE.match(line):
            continue
        m = F_RE.match(line)
        if m:
            fid, date, topic, conf, tags, body = m.groups()
            body = re.sub(r"\s*→\s*", " → ", body)
            if " → 证据：" not in body:
                bad.append((ln, "缺「→ 证据：」分隔符", line))
                continue
            concl, ev = body.split(" → 证据：", 1)
            again = ""
            am = re.search(r"（再报 ([^）]+)）\s*$", ev)
            if am:
                again = am.group(1).strip()
                ev = ev[: am.start()].strip()
            tag_list = re.findall(r"#[^\s#]+", tags)
            findings.append({"id": fid or "", "date": date, "topic": topic.strip(),
                             "conf": conf, "tags": tag_list,
                             "text": concl.strip(), "doi": ev.strip(),
                             "again": again, "line": ln})
            continue
        m = Q_RE.match(line)
        if m:
            qid, date, status, trig, question = m.groups()
            cand = ""
            if ln < len(lines):
                cm = CAND_RE.match(lines[ln].rstrip())
                if cm:
                    cand = cm.group(1).strip()
            questions.append({"id": qid or "", "date": date, "status": status,
                              "triggers": re.findall(r"F\d{3}", trig),
                              "q": question.strip(), "cand": cand, "line": ln})
            continue
        m = C_RE.match(line)
        if m:
            cid, head, body = m.groups()
            body = re.sub(r"\s*→\s*", " → ", body)
            if " → 依据：" not in body or " → 可证伪预测：" not in body:
                bad.append((ln, "联系条目缺「依据」或「可证伪预测」", line))
                continue
            text_part, rest = body.split(" → 依据：", 1)
            refs_s, pred = rest.split(" → 可证伪预测：", 1)
            links.append({"id": cid or "", "head": head.strip(), "text": text_part.strip(),
                          "refs": re.findall(r"F\d{3}", refs_s),
                          "pred": pred.strip(), "line": ln})
            continue
        bad.append((ln, "条目格式无法解析", line))
    return findings, links, questions, bad


def do_check(args):
    text = read()
    if not text:
        print(f"[F] 读不到记忆库：{FINDINGS}")
        return 2
    findings, links, questions, bad = parse(text)
    fails, warns = [], []

    for ln, why, line in bad:
        fails.append(f"第 {ln} 行 {why}：{line[:70]}")

    fids = [f["id"] for f in findings]
    cids = [c["id"] for c in links]
    qids = [q["id"] for q in questions]
    if any(not i for i in fids + cids + qids):
        fails.append("有条目缺编号（跑 renumber 修复）")
    else:
        if len(set(fids)) != len(fids):
            fails.append("结论编号有重复")
        if fids and fids != [f"F{i:03d}" for i in range(1, len(fids) + 1)]:
            fails.append("结论编号不连续（跑 renumber 修复）")
        if cids and cids != [f"C{i:03d}" for i in range(1, len(cids) + 1)]:
            fails.append("联系编号不连续（跑 renumber 修复）")
        if qids and qids != [f"Q{i:03d}" for i in range(1, len(qids) + 1)]:
            fails.append("问题编号不连续（跑 renumber 修复）")

    doi_seen = {}
    for f in findings:
        if not f["doi"]:
            fails.append(f"{f['id']} 没有证据 DOI")
        if f["doi"] in doi_seen:
            fails.append(f"DOI 重复：{f['id']} 与 {doi_seen[f['doi']]} 同为 {f['doi']}（应合并为「再报」）")
        else:
            doi_seen[f["doi"]] = f["id"]
        for t in f["tags"]:
            if t.lstrip("#") not in TAGS:
                fails.append(f"{f['id']} 标签越界：{t}（闭集见 README）")
        if len(f["text"]) > MAX_TEXT:
            warns.append(f"{f['id']} 结论 {len(f['text'])} 字，超过 {MAX_TEXT} 字上限，需再压缩")
        if not f["tags"]:
            warns.append(f"{f['id']} 没有标签")

    known = set(fids)
    for c in links:
        if len(c["refs"]) < 2:
            fails.append(f"{c['id']} 依据不足 2 条结论（{len(c['refs'])} 条）")
        for r in c["refs"]:
            if r not in known:
                fails.append(f"{c['id']} 引用了不存在的 {r}")
        if len(c["pred"]) < 10:
            warns.append(f"{c['id']} 可证伪预测过短，可能只是感想")

    for q in questions:
        if q["status"] not in QSTATUS:
            fails.append(f"{q['id']} 状态越界：{q['status']}（闭集 open/explained/falsified）")
        if not q["triggers"]:
            fails.append(f"{q['id']} 没有触发 F 编号")
        for r in q["triggers"]:
            if r not in known:
                fails.append(f"{q['id']} 触发了不存在的 {r}")
        if not (q["q"].endswith("？") or q["q"].endswith("?")):
            warns.append(f"{q['id']} 问题句未以问号结尾")
        if q["status"] == "explained" and not q["cand"]:
            fails.append(f"{q['id']} 标为 explained 但缺候选解释（指向 C）")

    by_topic, by_conf, by_tag = {}, {}, {}
    for f in findings:
        by_topic[f["topic"]] = by_topic.get(f["topic"], 0) + 1
        by_conf[f["conf"]] = by_conf.get(f["conf"], 0) + 1
        for t in f["tags"]:
            by_tag[t] = by_tag.get(t, 0) + 1
    dates = sorted(f["date"] for f in findings)

    print("记忆库体检")
    print(f"  结论 {len(findings)} 条｜联系 {len(links)} 条｜问题 {len(questions)} 条｜跨 {dates[0]} ~ {dates[-1]}" if dates else "  库是空的")
    print(f"  置信度：高 {by_conf.get('高', 0)}｜中 {by_conf.get('中', 0)}｜低 {by_conf.get('低', 0)}")
    print(f"  再报合并 {sum(1 for f in findings if f['again'])} 条｜主题 {len(by_topic)} 个")
    if questions:
        qstat = {s: sum(1 for q in questions if q["status"] == s) for s in QSTATUS}
        print(f"  问题状态：open {qstat['open']}｜explained {qstat['explained']}｜falsified {qstat['falsified']}")
    print("  主题分布：" + "｜".join(f"{k} {v}" for k, v in sorted(by_topic.items(), key=lambda x: -x[1])))
    print("  标签分布：" + "｜".join(f"{k} {v}" for k, v in sorted(by_tag.items(), key=lambda x: -x[1])))
    for w in warns:
        print(f"  [W] {w}")
    for f in fails:
        print(f"  [F] {f}")
    print(f"  结论：{'通过' if not fails and not warns else ('告警' if not fails else '失败')}")

    if args.json_out:
        d = os.path.dirname(args.json_out)
        if d:
            os.makedirs(d, exist_ok=True)
        json.dump({"findings": len(findings), "links": len(links), "questions": len(questions),
                   "by_topic": by_topic, "by_conf": by_conf, "by_tag": by_tag,
                   "warns": warns, "fails": fails},
                  io.open(args.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 2 if fails else (1 if warns else 0)


def do_search(args):
    text = read()
    findings, links, questions, _ = parse(text)
    items = findings + links + questions
    if args.id:
        want = {i.upper() for i in args.id}
        items = [x for x in items if x["id"].upper() in want]
    if args.topic:
        items = [x for x in items if args.topic in x.get("topic", "") or args.topic in x.get("head", "")]
    if args.tag:
        want = {t if t.startswith("#") else "#" + t for t in args.tag}
        items = [x for x in items if want & set(x.get("tags", []))]
    if args.since:
        items = [x for x in items if x.get("date", x.get("head", "")) >= args.since]
    if args.kw:
        items = [x for x in items if args.kw in x.get("text", "") or args.kw in x.get("doi", "")
                 or args.kw in x.get("pred", "") or args.kw in x.get("q", "")
                 or args.kw in x.get("cand", "")]
    if not items:
        print("没有匹配条目")
        return 1
    for x in items[: args.limit]:
        if "doi" in x:
            again = f"（再报 {x['again']}）" if x["again"] else ""
            print(f"[{x['id']}] {x['date']}｜{x['topic']}｜{x['conf']}｜{' '.join(x['tags'])}")
            print(f"    {x['text']} → {x['doi']}{again}")
        elif "pred" in x:
            print(f"[{x['id']}] {x['head']}｜依据 {'、'.join(x['refs'])}")
            print(f"    {x['text']}")
            print(f"    可证伪预测：{x['pred']}")
        else:
            print(f"[{x['id']}] {x['date']}｜{x['status']}｜触发 {'、'.join(x['triggers'])}")
            print(f"    {x['q']}")
            if x["cand"]:
                print(f"    候选解释：{x['cand']}")
    if len(items) > args.limit:
        print(f"…… 共 {len(items)} 条，已显示前 {args.limit} 条")
    return 0


def next_id(items, prefix):
    nums = [int(x["id"][1:]) for x in items if x["id"].startswith(prefix)]
    return f"{prefix}{(max(nums) + 1) if nums else 1:03d}"


def _norm_tags(raw):
    if isinstance(raw, list):
        parts = raw
    else:
        parts = (raw or "").replace("，", ",").split(",")
    return [t if t.startswith("#") else "#" + t for t in (p.strip() for p in parts) if t]


def insert_findings(text, date, lines):
    """把若干结论行插入到 date 小节下；无该小节则在「一、结论」区末尾新建。

    注意：文件头之后也有一条 `---`，所以不能按第一个分隔符切，否则会插进标题区。
    正确做法是定位「二、联系」标题，取其前最后一条 `---` 作为结论区右边界。
    """
    block = "\n".join(lines)
    m2 = re.search(r"^##\s*二[、·]\s*联系", text, re.M)
    sec2 = m2.start() if m2 else len(text)
    seps = list(re.finditer(r"^---\s*$", text[:sec2], re.M))
    end = seps[-1].start() if seps else sec2
    head = text[:end]
    ds = list(re.finditer(r"^### (\d{4}-\d{2}-\d{2})\s*$", head, re.M))
    if ds and ds[-1].group(1) == date:
        new_head = head.rstrip("\n") + "\n" + block + "\n"
    else:
        new_head = head.rstrip("\n") + f"\n\n### {date}\n\n{block}\n"
    return new_head + "\n" + text[end:]


def insert_questions(text, date, lines):
    """把问题行追加到「三、问题」区末尾；无该区则在文末新建。"""
    block = "\n".join(lines)
    m3 = re.search(r"^##\s*三[、·]\s*问题.*$", text, re.M)
    if not m3:
        return (text.rstrip("\n") + "\n\n---\n\n## 三、问题（触发式生长）\n\n"
                "> 给「未知」一等公民：由 R1 桥接 / R2 缺口 / R3 张力 / R4 冗余触发。\n"
                "> 每条挂 ≥1 条触发 F，状态 open / explained / falsified；候选解释指向 C。\n\n"
                + block + "\n")
    nxt = re.search(r"^##\s", text[m3.end():], re.M)
    end = m3.end() + nxt.start() if nxt else len(text)
    head = text[:end].rstrip("\n")
    tail = text[end:].strip("\n")
    out = head + "\n" + block + "\n"
    if tail:
        out += "\n" + tail + "\n"
    return out


def do_add(args):
    text = read()
    findings, links, questions, _ = parse(text)
    date = args.date or __import__("datetime").date.today().isoformat()

    if args.type == "finding":
        if not (args.topic and args.text and args.doi):
            print("[F] finding 需要 --topic / --text / --doi")
            return 2
        for f in findings:
            if f["doi"] == args.doi:
                print(f"[!] 该 DOI 已在库（{f['id']}，{f['date']}）。按契约不重复入库，"
                      f"如需标注重复报道请手动追加「（再报 {date[5:]}）」")
                return 1
        tags = _norm_tags(args.tags)
        for t in tags:
            if t.lstrip("#") not in TAGS:
                print(f"[F] 标签越界：{t}")
                return 2
        if args.conf not in CONF:
            print(f"[F] 置信度必须是 {'/'.join(CONF)}")
            return 2
        again = f"（再报 {args.again}）" if args.again else ""
        line = (f"- **[]**（{date}｜{args.topic}｜{args.conf}｜{' '.join(tags)}）"
                f"{args.text} → 证据：{args.doi}{again}")
        new = insert_findings(text, date, [line])
    elif args.type == "question":
        if not (args.text and args.refs):
            print("[F] question 需要 --text / --refs")
            return 2
        status = args.status or "open"
        if status not in QSTATUS:
            print(f"[F] 状态必须是 {'/'.join(QSTATUS)}")
            return 2
        trig = re.findall(r"F\d{3}", args.refs)
        if not trig:
            print("[F] question 必须挂 ≥1 条触发 F 编号")
            return 2
        known = {f["id"] for f in findings}
        for r in trig:
            if r not in known:
                print(f"[F] 触发了不存在的 {r}")
                return 2
        q = f"- **[]**（{date}｜{status}｜触发：{'、'.join(trig)}）{args.text}"
        if args.cand:
            q += f"\n  - 候选解释：{args.cand}"
        new = insert_questions(text, date, [q])
    else:
        if not (args.text and args.refs and args.pred):
            print("[F] link 需要 --text / --refs / --pred")
            return 2
        refs = re.findall(r"F\d{3}", args.refs)
        if len(refs) < 2:
            print("[F] 联系条目必须挂 ≥2 条结论编号")
            return 2
        known = {f["id"] for f in findings}
        for r in refs:
            if r not in known:
                print(f"[F] 引用了不存在的 {r}")
                return 2
        new = text.rstrip("\n") + (f"\n- **[]**（{date}｜{args.span or '跨期'}）{args.text}"
                                  f" → 依据：{'、'.join(refs)} → 可证伪预测：{args.pred}\n")

    io.open(FINDINGS, "w", encoding="utf-8").write(new)
    print("已追加，正在重排编号…")
    return do_renumber(argparse.Namespace(write=True))


def do_ingest(args):
    text = read()
    findings, _, _, _ = parse(text)
    if not os.path.isfile(args.data):
        print(f"[F] 读不到 {args.data}")
        return 2
    data = json.load(io.open(args.data, encoding="utf-8"))
    date = args.date or data.get("date")
    if not date:
        print("[F] 既没有 --date，report_data.json 里也没有 date")
        return 2

    by_doi = {f["doi"]: f for f in findings}
    lines, seen, no_conf, no_tags, dup = [], set(), [], [], []
    for c in sorted(data.get("chapters", {}).values(), key=lambda x: x.get("order", 0)):
        topic = (c.get("title") or "").strip()
        for p in c.get("papers", []):
            doi = (p.get("doi") or "").strip()
            body = (p.get("one_liner") or "").strip()
            if not doi or not body:
                print(f"[!] 跳过缺 DOI 或 one_liner 的论文：{(p.get('title') or '')[:40]}")
                continue
            if doi in by_doi:
                dup.append((by_doi[doi]["id"], doi, date))
                continue
            if doi in seen:
                continue
            seen.add(doi)
            conf = (p.get("conf") or "").strip()
            if conf not in CONF:
                if conf:
                    print(f"[!] conf 非法（{conf}），{doi} 回退为「中」")
                conf = "中"
                no_conf.append(doi)
            tags = _norm_tags(p.get("tags"))
            bad = [t for t in tags if t.lstrip("#") not in TAGS]
            if bad:
                print(f"[!] {doi} 标签越界，已丢弃：{' '.join(bad)}")
                tags = [t for t in tags if t.lstrip("#") not in bad]
            if not tags:
                no_tags.append(doi)
            lines.append(f"- **[]**（{date}｜{topic}｜{conf}｜{' '.join(tags)}）"
                         f"{body} → 证据：{doi}")

    if not lines and not dup:
        print("本期没有可入库的新结论（可能都已入库）")
        return 0

    print(f"本期待入库 {len(lines)} 条｜已在库(转再报) {len(dup)} 条")
    for fid, doi, d in dup:
        print(f"  [再报] {doi} → 已在 {fid}，请手动在该条末尾追加「（再报 {d[5:]}）」")
    if no_conf:
        print(f"  [待补] 缺 conf 默认「中」{len(no_conf)} 条：{' '.join(no_conf)}")
    if no_tags:
        print(f"  [待补] 缺 tags {len(no_tags)} 条：{' '.join(no_tags)}")

    if args.dry_run:
        print("  --dry-run，未落盘。预览：")
        for l in lines:
            print("   " + l)
        return 0
    if not lines:
        return 0

    io.open(FINDINGS, "w", encoding="utf-8").write(insert_findings(text, date, lines))
    print("已写入，正在重排编号…")
    return do_renumber(argparse.Namespace(write=True))


def do_renumber(args):
    text = read()
    findings, links, questions, bad = parse(text)
    if bad:
        print(f"[F] 有 {len(bad)} 条格式无法解析，先修格式再重排")
        return 2
    # 第一遍：按出现顺序定号，记录旧号→新号（空号不计入 map）
    fmap, cmap, qmap = {}, {}, {}
    fi = ci = qi = 0
    for line in text.split("\n"):
        s = line.rstrip()
        mf = F_RE.match(s)
        mq = None if mf else Q_RE.match(s)
        mc = None if (mf or mq) else C_RE.match(s)
        if mf:
            fi += 1
            if mf.group(1):
                fmap[mf.group(1)] = f"F{fi:03d}"
        elif mq:
            qi += 1
            if mq.group(1):
                qmap[mq.group(1)] = f"Q{qi:03d}"
        elif mc:
            ci += 1
            if mc.group(1):
                cmap[mc.group(1)] = f"C{ci:03d}"

    # 第二遍：改写编号（空号 `**[]**` 按位置补号），并同步引用
    #   C 条目 → 同步 F 引用；Q 条目 → 同步 F 触发；候选解释行 → 同步 C 引用
    out, changed = [], 0
    fi = ci = qi = 0
    for line in text.split("\n"):
        orig = line
        s = line.rstrip()
        mf = F_RE.match(s)
        mq = None if mf else Q_RE.match(s)
        mc = None if (mf or mq) else C_RE.match(s)
        if mf:
            fi += 1
            old = mf.group(1)
            line = line.replace(f"**[{old}]**" if old else "**[]**", f"**[F{fi:03d}]**", 1)
        elif mq:
            qi += 1
            old = mq.group(1)
            line = line.replace(f"**[{old}]**" if old else "**[]**", f"**[Q{qi:03d}]**", 1)
            for a, b in fmap.items():
                line = re.sub(rf"\b{a}\b", b, line)
        elif mc:
            ci += 1
            old = mc.group(1)
            line = line.replace(f"**[{old}]**" if old else "**[]**", f"**[C{ci:03d}]**", 1)
            for a, b in fmap.items():
                line = re.sub(rf"\b{a}\b", b, line)
        elif CAND_RE.match(s):
            for a, b in cmap.items():
                line = re.sub(rf"\b{a}\b", b, line)
        if line != orig:
            changed += 1
        out.append(line)
    new = "\n".join(out)
    if changed and args.write:
        io.open(FINDINGS, "w", encoding="utf-8").write(new)
        print(f"已重排 {changed} 行（结论 {len(findings)} 条，联系 {len(links)} 条，问题 {len(questions)} 条）")
    elif changed:
        print(f"有 {changed} 行编号需要重排，加 --write 落盘")
        return 1
    else:
        print("编号已是连续的，无需改动")
    return 0


ap = argparse.ArgumentParser(description="记忆库工具")
sub = ap.add_subparsers(dest="cmd")
p = sub.add_parser("check")
p.add_argument("--json", dest="json_out", default=None)
p = sub.add_parser("search")
p.add_argument("--id", nargs="*")
p.add_argument("--topic")
p.add_argument("--tag", nargs="*")
p.add_argument("--kw")
p.add_argument("--since")
p.add_argument("--limit", type=int, default=20)
p = sub.add_parser("add")
p.add_argument("--type", choices=["finding", "link", "question"], default="finding")
p.add_argument("--date")
p.add_argument("--topic")
p.add_argument("--conf", default="中")
p.add_argument("--tags", default="")
p.add_argument("--text")
p.add_argument("--doi")
p.add_argument("--again")
p.add_argument("--refs")
p.add_argument("--pred")
p.add_argument("--span")
p.add_argument("--cand")
p.add_argument("--status", default=None)
p = sub.add_parser("ingest")
p.add_argument("--data", required=True)
p.add_argument("--date")
p.add_argument("--dry-run", action="store_true")
p = sub.add_parser("renumber")
p.add_argument("--write", action="store_true")

args = ap.parse_args()
if args.cmd == "check":
    sys.exit(do_check(args))
if args.cmd == "search":
    sys.exit(do_search(args))
if args.cmd == "ingest":
    sys.exit(do_ingest(args))
if args.cmd == "add":
    sys.exit(do_add(args))
if args.cmd == "renumber":
    sys.exit(do_renumber(args))
ap.print_help()