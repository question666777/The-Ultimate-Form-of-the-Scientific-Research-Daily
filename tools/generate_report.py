# -*- coding: utf-8 -*-
"""前沿生物追踪 · 报告渲染器（常驻脚本）。

读取 report_data.json，渲染为自包含 HTML（内嵌 SVG 图表；DOI 与目录导航均为可点击链接）。
用法：
    python generate_report.py --data tmp/report_data.json --output tmp/report_full.html

固化清单：
L031 footer/h1/chapter-skills 去 AI 自动化痕迹（footer 不写"自动日报"/"流水线生成"，
    chapter-skills 从可见 div 改为 HTML 注释，h1 去掉"每日日报"）
L032 渲染后自动调用 humanize_text.fix_all 去AI化修复（词汇替换+空格修复+连接词删除）
L039 报告为通用 HTML（2026-10-01 起不再出公众号版本，L036 外链禁令/L037 SVG元素过滤作废）：
    DOI 与目录导航均渲染为可点击链接，外链不再剥离

视觉形式（2026-09-29 新增，每篇报告各一次、一个主题一种）：
  - 「细胞朋友圈」build_moments_svg：主题一章节开头，拟人发帖+评论区（chapter.visual.type=="moments"）
  - 「反派档案卡」build_villain_svg：主题二机制最硬的论文卡片内，通缉令风格
    （chapter.visual.type=="villain"，visual.data.ref_no 指定嵌在哪篇论文）
  - 两图头像是 <rect>/<line>/<polyline> 拼的卡通微生物（AVATAR_MOMENTS 绿杆状细菌 / AVATAR_VILLAIN 红刺突病毒）
  - 「细胞朋友圈」为绿底白字（#4a8a5f），评论框浅绿（#5d9a72），全卡白字
L038 反派档案卡「档案编号」由报告日期自动派生（FBT-YYYYMMDD），忽略 report_data 里的手填 no
    —— 手填字段每期必忘改，编号会一直停在同一天
"""
import argparse
import html
import json
import re


def _wrap_text(text, max_chars=26):
    """中文按字符数断行（SVG text 不自动换行）。

    标点不开头；JAK-STAT/TRIM21 这类 ASCII 词不在中间拆断。
    """
    def ascii_al(c):
        return c.isascii() and c.isalnum()

    def bonded(c1, c2):
        return (ascii_al(c1) and (ascii_al(c2) or c2 == "-")) or (c1 == "-" and ascii_al(c2))

    lines = []
    rest = text
    while len(rest) > max_chars:
        cut = max_chars
        while cut > 1 and (rest[cut] in "，。：；！？、）」』】》…—" or bonded(rest[cut - 1], rest[cut])):
            cut -= 1
        if cut <= 1:
            cut = max_chars
        lines.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        lines.append(rest)
    return lines


# 卡通微生物头像：用 <rect>/<line>/<polyline> 拼出可辨认的微生物卡通，
# 全部内嵌无外链，坐标锁死在这两块头像框内。
# AVATAR_MOMENTS：朋友圈头像＝绿色杆状细菌（圆角菌体 + 两端鞭毛 + 背部菌毛 + 眼睛笑脸），
#   画在 56×56 白底圆（rect rx=28）上，坐标范围 x32~88 / y38~94。
# AVATAR_VILLAIN：通缉令头像＝红色刺突病毒（圆角衣壳 + 八根带帽刺突 + 怒眉 + 锯齿嘴），
#   直接画在 108×108 暗色通缉框里，坐标范围 x98~178 / y138~216。
AVATAR_MOMENTS = (
    '<rect x="32" y="38" width="56" height="56" rx="28" fill="#ffffff"/>'
    '<rect x="35.5" y="41.5" width="49" height="49" rx="24.5" fill="#eaf7ef"/>'
    '<polyline points="78,66 82,61 84,67 87,63" fill="none" stroke="#2f7d4f" stroke-width="1.6" stroke-linecap="round"/>'
    '<polyline points="42,66 38,61 36,67 33,63" fill="none" stroke="#2f7d4f" stroke-width="1.6" stroke-linecap="round"/>'
    '<line x1="52" y1="55" x2="52" y2="49" stroke="#2f7d4f" stroke-width="1.4" stroke-linecap="round"/>'
    '<line x1="60" y1="54" x2="60" y2="48" stroke="#2f7d4f" stroke-width="1.4" stroke-linecap="round"/>'
    '<line x1="68" y1="55" x2="68" y2="49" stroke="#2f7d4f" stroke-width="1.4" stroke-linecap="round"/>'
    '<rect x="42" y="55" width="36" height="24" rx="12" fill="#4a8a5f" stroke="#2f7d4f" stroke-width="1.6"/>'
    '<rect x="51" y="61" width="5" height="7" rx="2.5" fill="#ffffff"/>'
    '<rect x="65" y="61" width="5" height="7" rx="2.5" fill="#ffffff"/>'
    '<rect x="52.5" y="63" width="2" height="3" rx="1" fill="#1f5c3a"/>'
    '<rect x="66.5" y="63" width="2" height="3" rx="1" fill="#1f5c3a"/>'
    '<polyline points="55,72 60,75 65,72" fill="none" stroke="#ffffff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'
)
AVATAR_VILLAIN = (
    '<line x1="138" y1="158" x2="138" y2="144" stroke="#e0576a" stroke-width="2" stroke-linecap="round"/>'
    '<line x1="138" y1="198" x2="138" y2="212" stroke="#e0576a" stroke-width="2" stroke-linecap="round"/>'
    '<line x1="118" y1="178" x2="104" y2="178" stroke="#e0576a" stroke-width="2" stroke-linecap="round"/>'
    '<line x1="158" y1="178" x2="172" y2="178" stroke="#e0576a" stroke-width="2" stroke-linecap="round"/>'
    '<line x1="124" y1="164" x2="112" y2="152" stroke="#e0576a" stroke-width="2" stroke-linecap="round"/>'
    '<line x1="152" y1="164" x2="164" y2="152" stroke="#e0576a" stroke-width="2" stroke-linecap="round"/>'
    '<line x1="124" y1="192" x2="112" y2="204" stroke="#e0576a" stroke-width="2" stroke-linecap="round"/>'
    '<line x1="152" y1="192" x2="164" y2="204" stroke="#e0576a" stroke-width="2" stroke-linecap="round"/>'
    '<rect x="135" y="138.5" width="6" height="6" rx="3" fill="#e0576a"/>'
    '<rect x="135" y="211.5" width="6" height="6" rx="3" fill="#e0576a"/>'
    '<rect x="98.5" y="175" width="6" height="6" rx="3" fill="#e0576a"/>'
    '<rect x="171.5" y="175" width="6" height="6" rx="3" fill="#e0576a"/>'
    '<rect x="107" y="147" width="6" height="6" rx="3" fill="#e0576a"/>'
    '<rect x="163" y="147" width="6" height="6" rx="3" fill="#e0576a"/>'
    '<rect x="107" y="203" width="6" height="6" rx="3" fill="#e0576a"/>'
    '<rect x="163" y="203" width="6" height="6" rx="3" fill="#e0576a"/>'
    '<rect x="118" y="158" width="40" height="40" rx="12" fill="#d1495b" stroke="#ffd9de" stroke-width="1.5"/>'
    '<rect x="127" y="170" width="9" height="9" rx="4.5" fill="#ffffff"/>'
    '<rect x="140" y="170" width="9" height="9" rx="4.5" fill="#ffffff"/>'
    '<rect x="129.5" y="172.5" width="4" height="5" rx="2" fill="#8e1b1b"/>'
    '<rect x="142.5" y="172.5" width="4" height="5" rx="2" fill="#8e1b1b"/>'
    '<polyline points="125,164 135,169" fill="none" stroke="#3a2f2a" stroke-width="2" stroke-linecap="round"/>'
    '<polyline points="151,164 141,169" fill="none" stroke="#3a2f2a" stroke-width="2" stroke-linecap="round"/>'
    '<polyline points="128,186 133,191 138,186 143,191 148,186" fill="none" stroke="#ffffff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
)


def build_moments_svg(v):
    """细胞朋友圈（主题一）：拟人发帖 + 评论区互动（绿底白字）。"""
    W = 640
    d = v["data"]
    esc = html.escape
    post_lines = _wrap_text(d["post"], 26)
    comments = d.get("comments", [])
    cmt_lines = []
    for c in comments:
        cmt_lines.append((c["name"], _wrap_text(c["text"], 24)))
    n_cmt_rows = sum(len(x[1]) for x in cmt_lines)
    H = 132 + 30 * len(post_lines) + 16 + (34 * n_cmt_rows + 28 if cmt_lines else 0) + 24
    parts = []
    # 绿底卡片（养眼绿）+ 白字
    parts.append(f'<rect x="10" y="10" width="{W-20}" height="{H-20}" rx="14" fill="#4a8a5f"/>')
    # 右上角标签
    parts.append(f'<text x="{W-36}" y="42" text-anchor="end" font-size="12" fill="#cfe6d8" letter-spacing="2">细胞朋友圈</text>')
    # 头像（绿底白字圆）+ 名字 + 时间
    parts.append(AVATAR_MOMENTS)
    parts.append(f'<text x="102" y="62" font-size="17" font-weight="700" fill="#ffffff">{esc(d["poster"])}</text>')
    parts.append(f'<text x="102" y="84" font-size="12" fill="#cfe6d8">{esc(d.get("time", "刚刚"))}</text>')
    # 帖子正文
    y = 122
    for ln in post_lines:
        parts.append(f'<text x="42" y="{y}" font-size="15" fill="#ffffff">{esc(ln)}</text>')
        y += 28
    # 评论区（浅绿底 + 白字）
    if cmt_lines:
        y += 6
        box_h = 34 * n_cmt_rows + 26
        parts.append(f'<rect x="42" y="{y}" width="{W-84}" height="{box_h}" rx="8" fill="#5d9a72"/>')
        cy = y + 26
        for name, lines in cmt_lines:
            for i, ln in enumerate(lines):
                prefix = f'{esc(name)}：' if i == 0 else ""
                parts.append(f'<text x="56" y="{cy}" font-size="13"><tspan fill="#ffffff" font-weight="600">{prefix}</tspan>'
                             f'<tspan fill="#eaf5ee">{esc(ln)}</tspan></text>')
                cy += 30
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="细胞朋友圈">'
            + "".join(parts) + "</svg>")


def _archive_no(report_date):
    """档案编号 = 报告日期（YYYYMMDD），每期自动不同；手填字段一律忽略（L038）。"""
    digits = re.sub(r"\D", "", report_date or "")
    return digits if len(digits) == 8 else "0000"


def build_villain_svg(v, report_date=""):
    """反派档案卡（主题二）：通缉令风格，嵌在机制最硬的论文卡片内。

    布局按内容动态量高：先算出右侧「通缉犯/罪名」区块的底边，再定分隔线与下半区起点，
    罪名行数多时不会压到分隔线和【作案手法】（2026-09-30 修复）。
    """
    W = 640
    d = v["data"]
    esc = html.escape
    name_lines = _wrap_text(d["villain"], 18)
    crime_lines = _wrap_text(d["crime"], 18)
    method_lines = _wrap_text(d["method"], 30)
    weak_lines = _wrap_text(d["weakness"], 30)
    # 右侧区块：标签 + 正文行
    name_label_y = 148
    name_top = name_label_y + 24
    crime_label_y = name_top + 28 * (len(name_lines) - 1) + 22
    crime_top = crime_label_y + 24
    right_bottom = crime_top + 28 * (len(crime_lines) - 1)
    # 头像框底边在 232；分隔线取二者较大值再留 26 的呼吸位
    sep_y = max(232, right_bottom) + 26
    method_label_y = sep_y + 32
    method_top = sep_y + 56
    method_bottom = method_top + 24 * (len(method_lines) - 1)
    weak_label_y = method_bottom + 34
    weak_top = weak_label_y + 26
    weak_bottom = weak_top + 24 * (len(weak_lines) - 1)
    H = weak_bottom + 72
    parts = []
    parts.append(f'<rect x="8" y="8" width="{W-16}" height="{H-16}" rx="6" fill="#3a2f2a"/>')
    parts.append(f'<rect x="20" y="20" width="{W-40}" height="{H-40}" fill="none" stroke="#c9a86a" stroke-width="2.5"/>')
    parts.append(f'<rect x="27" y="27" width="{W-54}" height="{H-54}" fill="none" stroke="#c9a86a" stroke-width="1"/>')
    parts.append(f'<text x="{W//2}" y="70" text-anchor="middle" font-size="30" font-weight="800" fill="#e8c987" letter-spacing="12">通缉令</text>')
    parts.append(f'<text x="{W//2}" y="94" text-anchor="middle" font-size="12" fill="#b9a582" letter-spacing="4">WANTED · 本期生物反派档案</text>')
    # 左：头像（红底白字圆，用 rect rx 画圆）
    parts.append(f'<rect x="84" y="124" width="108" height="108" rx="54" fill="#4a3b33" stroke="#d1495b" stroke-width="3" stroke-dasharray="7,5"/>')
    parts.append(AVATAR_VILLAIN)
    # 右：通缉犯 + 罪名
    parts.append(f'<text x="216" y="{name_label_y}" font-size="12" fill="#b9a582" letter-spacing="2">通缉犯</text>')
    yy = name_top
    for ln in name_lines:
        parts.append(f'<text x="216" y="{yy}" font-size="17" font-weight="700" fill="#f2e8d5">{esc(ln)}</text>')
        yy += 28
    parts.append(f'<text x="216" y="{crime_label_y}" font-size="12" fill="#b9a582" letter-spacing="2">罪名</text>')
    yy = crime_top
    for ln in crime_lines:
        parts.append(f'<text x="216" y="{yy}" font-size="17" font-weight="700" fill="#f2e8d5">{esc(ln)}</text>')
        yy += 28
    parts.append(f'<line x1="56" y1="{sep_y}" x2="{W-56}" y2="{sep_y}" stroke="#c9a86a" stroke-width="1" opacity="0.5"/>')
    # 作案手法
    parts.append(f'<text x="56" y="{method_label_y}" font-size="14" font-weight="700" fill="#d1495b">【作案手法】</text>')
    yy = method_top
    for ln in method_lines:
        parts.append(f'<text x="56" y="{yy}" font-size="14" fill="#e8ded0">{esc(ln)}</text>')
        yy += 24
    # 弱点
    parts.append(f'<text x="56" y="{weak_label_y}" font-size="14" font-weight="700" fill="#7fb069">【弱点】</text>')
    yy = weak_top
    for ln in weak_lines:
        parts.append(f'<text x="56" y="{yy}" font-size="14" fill="#e8ded0">{esc(ln)}</text>')
        yy += 24
    parts.append(f'<text x="{W-56}" y="{H-44}" text-anchor="end" font-size="11" fill="#8a7a66">档案编号 FBT-{_archive_no(report_date)}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="反派档案卡">'
            + "".join(parts) + "</svg>")


def render(data):
    chapters = list(data["chapters"].values())
    chapters.sort(key=lambda c: c["order"])
    d = data
    H = html
    # 目录
    toc_items = "".join(f'<li><a href="#{c["id"]}">{H.escape(c["title"])}</a></li>' for c in chapters)
    # 生活启示
    life_cards = "".join(
        f'<div class="life-card"><h4>{i+1}. {H.escape(l["title"])}</h4><p>{H.escape(l["body"])}</p>'
        f'<div class="evidence">{H.escape(l["evidence"])}</div></div>'
        for i, l in enumerate(d["life"]))
    # 参考文献
    refs = "".join(
        f'<div class="ref-item"><span class="ref-num">[{r["no"]}]</span> {H.escape(r["authors"])} {H.escape(r["title"])}. '
        f'<em>{H.escape(r["journal"])}</em>. {H.escape(r["date"])}. '
        f'<a href="https://doi.org/{H.escape(r["doi"])}">DOI: {H.escape(r["doi"])}</a></div>'
        for r in d["references"])
    # 章节
    chapter_html = ""
    for c in chapters:
        color = c["color"]
        vis = c.get("visual")
        visual_html = ""
        if vis and vis.get("type") == "moments":
            visual_html = f'<div class="chapter-svg">{build_moments_svg(vis)}</div>'
        papers = ""
        for p in c["papers"]:
            badge = ""
            if p["badge"] == "top":
                badge = '<span class="badge-top" style="background:#2e86ab">顶刊</span>'
            elif p["badge"] == "pre":
                badge = '<span class="badge-pre">预印本</span>'
            doi_link = f'<a href="https://doi.org/{H.escape(p["doi"])}">{H.escape(p["doi"])}</a>'
            pmid_txt = f'<span>PMID: {H.escape(p["pmid"])}</span>' if p.get("pmid") else ""
            papers += (
                f'<div class="paper-card" id="{c["id"]}-p{p["ref_no"]}">'
                f'<div class="paper-header"><span class="paper-num">#{p["ref_no"]}</span>'
                f'<span class="time-label" style="border-color:{color};color:{color}">{H.escape(p["time_label"])}</span>'
                f'{badge}<span class="paper-journal">{H.escape(p["journal"])}</span>'
                f'<span class="paper-date">{H.escape(p["date"])}</span></div>'
                f'<div class="one-liner" style="border-left:4px solid {color}"><strong>一句话看懂：</strong>{H.escape(p["one_liner"])}</div>'
                f'<h4 class="paper-title">{H.escape(p["title"])}</h4>'
                f'<p class="paper-authors">{H.escape(p["authors"])}</p>'
                f'<div class="paper-abstract"><span class="abstract-label">深入解读</span><p>{H.escape(p["abstract_zh"])}</p></div>'
                + (f'<div class="chapter-svg">{build_villain_svg(vis, d["date"])}</div>'
                   if vis and vis.get("type") == "villain" and vis["data"].get("ref_no") == p["ref_no"] else "")
                + f'<div class="paper-meta"><span>DOI: {doi_link}</span>{pmid_txt}</div></div>')
        chapter_html += (
            f'<section class="chapter" id="{c["id"]}">'
            f'<div class="chapter-header" style="background:linear-gradient(135deg,{color}15,{color}05);border-top:4px solid {color}">'
            f'<div class="chapter-icon" style="background:{color}">{H.escape(c["icon"])}</div>'
            f'<div><h2>第{c["order"]}章 · {H.escape(c["title"])}</h2>'
            f'<p class="chapter-subtitle">{H.escape(c["subtitle"])}</p></div></div>'
            f'<div class="chapter-intro" style="background:{color}08;border-left:4px solid {color}">'
            f'<span class="intro-label" style="color:{color}">本章看点</span>'
            f'<p>{H.escape(c["intro"])}</p></div>'
            + visual_html
            + papers +
            (f'<div class="chapter-svg">{c["svg"]}</div>' if c.get("svg") else "") +
            (f'<!-- chapter-skills: {H.escape(c.get("skills",""))} -->' if c.get("skills") else "") +
            f'</section>')
        if c["order"] == 1 and len(chapters) >= 2:
            chapter_html += '__FLIP_PLACEHOLDER__'
    hl_labels = {"report": "本期解读", "top": "顶刊论文"}
    highlights = "".join(
        f'<div class="highlight">{hl_labels.get(k, k)} <strong>{H.escape(v)}</strong></div>'
        for k, v in d["highlights"].items() if k in ("report", "top"))
    # 翻转卡（报告中间，趣味花絮）
    ff = d.get("fun_fact", {})
    flip_html = ""
    if ff:
        flip_html = (
            f'<div class="fun-fact-box">'
            f'<div class="ff-label">📡 今日趣闻</div>'
            f'<div class="ff-question">{H.escape(ff.get("question", ""))}</div>'
            f'<div class="ff-answer">{H.escape(ff.get("answer", ""))}</div>'
            f'</div>'
        )
    # 互动投票（报告末尾）
    vt = d.get("vote", {})
    vote_html = ""
    if vt:
        opts = "".join(f'<div class="vote-option">{H.escape(o)}</div>' for o in vt.get("options", []))
        vote_html = (
            f'<section class="vote-section" id="vote">'
            f'<h2>今日互动</h2>'
            f'<div class="vote-sub">没有标准答案，只想听你说</div>'
            f'<div class="vote-q">{H.escape(vt.get("question", ""))}</div>'
            f'<div class="vote-options">{opts}</div>'
            f'</section>'
        )

    chapter_html = chapter_html.replace('__FLIP_PLACEHOLDER__', flip_html)

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>【前沿生物追踪】{d['date']}｜{' · '.join(c['title'] for c in chapters)}</title>
<style>
:root {{--paper:#f5f5f5;--paper-2:#ececec;--ink:#2d3142;--muted:#4f5d75;--soft:#7a8399;--rule:rgba(45,49,66,0.12);--accent:#eb6c36;--accent-tint:rgba(235,108,54,0.08);--link:#2e5aa8;}}
* {{box-sizing:border-box;margin:0;padding:0;}}
body {{font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:var(--paper);color:var(--ink);line-height:1.7;max-width:900px;margin:0 auto;padding:20px;}}
.report-header {{background:linear-gradient(135deg,#2d3142,#4f5d75);color:white;padding:30px;border-radius:12px;margin-bottom:24px;}}
.report-header h1 {{font-size:24px;margin-bottom:8px;}}
.report-header .meta {{font-size:13px;opacity:0.85;}}
.report-header .meta div {{margin:4px 0;}}
.summary-card {{background:white;border:2px solid var(--accent);border-radius:12px;padding:24px;margin-bottom:24px;}}
.summary-card .stars {{color:var(--accent);font-size:22px;letter-spacing:2px;margin-bottom:8px;}}
.summary-card .core-conclusion {{font-size:18px;line-height:1.6;margin:12px 0;}}
.summary-card .highlights {{display:flex;gap:16px;flex-wrap:wrap;margin-top:16px;}}
.summary-card .highlight {{background:var(--accent-tint);padding:8px 16px;border-radius:8px;font-size:13px;}}
.summary-card .highlight strong {{color:var(--accent);}}
.toc {{background:var(--paper-2);border-radius:12px;padding:20px 24px;margin-bottom:24px;}}
.toc h3 {{font-size:16px;margin-bottom:12px;color:var(--muted);}}
.toc ol {{list-style:none;counter-reset:toc;}}
.toc li {{counter-increment:toc;margin:6px 0;}}
.toc li::before {{content:counter(toc);display:inline-block;width:24px;height:24px;background:var(--ink);color:white;border-radius:50%;text-align:center;line-height:24px;font-size:12px;margin-right:8px;}}
.toc a {{color:var(--ink);text-decoration:none;}}
.toc a:hover {{color:var(--accent);}}
.chapter {{margin-bottom:32px;}}
.chapter-header {{padding:20px 24px;border-radius:12px;display:flex;align-items:center;gap:16px;}}
.chapter-icon {{min-width:48px;height:48px;border-radius:12px;display:flex;align-items:center;justify-content:center;color:white;font-size:18px;font-weight:700;flex-shrink:0;padding:0 8px;}}
.chapter-header h2 {{font-size:20px;}}
.chapter-subtitle {{font-size:14px;color:var(--muted);margin-top:2px;}}
.chapter-intro {{padding:16px 20px;border-radius:8px;margin:12px 0 20px;}}
.intro-label {{display:inline-block;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px;}}
.chapter-intro p {{font-size:15px;line-height:1.6;}}
.intro-stats {{display:none;}}
.chapter-svg {{margin:16px 0;padding:12px;background:var(--paper-2);border-radius:8px;overflow:hidden;}}
.fun-fact-box {{background:linear-gradient(135deg,#2d3142,#4f5d75);color:white;border-radius:12px;padding:24px;margin:32px auto;max-width:600px;}}
.fun-fact-box .ff-label {{font-size:13px;opacity:0.7;letter-spacing:0.05em;margin-bottom:10px;}}
.fun-fact-box .ff-question {{font-size:18px;font-weight:600;line-height:1.5;margin-bottom:12px;}}
.fun-fact-box .ff-answer {{font-size:15px;line-height:1.6;opacity:0.9;border-top:1px solid rgba(255,255,255,0.2);padding-top:12px;}}
.vote-section {{background:linear-gradient(135deg,#2d3142,#4f5d75);color:white;border-radius:16px;padding:32px;margin:40px 0 32px;text-align:center;}}
.vote-section h2 {{font-size:22px;margin-bottom:6px;color:white;}}
.vote-section .vote-sub {{font-size:13px;opacity:0.6;margin-bottom:16px;}}
.vote-section .vote-q {{font-size:19px;line-height:1.6;margin-bottom:24px;color:#ffd580;font-weight:600;}}
.vote-options {{display:flex;flex-wrap:wrap;gap:12px;justify-content:center;}}
.vote-option {{background:rgba(255,255,255,0.12);border:2px solid rgba(255,255,255,0.25);border-radius:10px;padding:14px 24px;font-size:15px;transition:all 0.3s;color:white;flex:1;min-width:140px;max-width:200px;}}
.vote-option:hover {{background:rgba(255,255,255,0.22);border-color:var(--accent);transform:translateY(-2px);}}
.paper-card {{background:white;border:1px solid var(--rule);border-radius:10px;padding:20px;margin-bottom:16px;}}
.paper-header {{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;}}
.paper-num {{font-size:14px;font-weight:700;color:var(--soft);}}
.time-label {{font-size:11px;padding:2px 8px;border:1px solid;border-radius:4px;}}
.badge-top,.badge-pre {{font-size:11px;padding:2px 8px;border-radius:4px;color:white;}}
.badge-pre {{background:var(--soft);}}
.paper-journal {{font-size:13px;color:var(--muted);font-weight:500;}}
.paper-date {{font-size:12px;color:var(--soft);}}
.one-liner {{background:var(--accent-tint);padding:10px 14px;border-radius:6px;margin:10px 0;font-size:15px;line-height:1.5;}}
.one-liner strong {{color:var(--accent);}}
.paper-title {{font-size:16px;margin:10px 0 4px;line-height:1.4;}}
.paper-authors {{font-size:12px;color:var(--soft);margin-bottom:10px;}}
.paper-abstract {{background:var(--paper);border-radius:6px;padding:12px 16px;}}
.abstract-label {{font-size:11px;color:var(--soft);text-transform:uppercase;letter-spacing:0.05em;display:block;margin-bottom:6px;}}
.paper-abstract p {{font-size:13px;line-height:1.6;}}
.paper-meta {{display:flex;gap:16px;margin-top:8px;font-size:12px;color:var(--soft);}}
.paper-meta a {{color:var(--link);text-decoration:none;}}
.life-section {{background:linear-gradient(135deg,rgba(235,108,54,0.04),rgba(45,49,66,0.04));border:2px solid var(--accent);border-radius:12px;padding:24px;margin:32px 0;}}
.life-section h2 {{font-size:20px;color:var(--accent);margin-bottom:12px;}}
.life-card {{background:white;border-radius:8px;padding:16px 20px;margin-bottom:12px;}}
.life-card h4 {{font-size:15px;margin-bottom:6px;}}
.life-card p {{font-size:14px;line-height:1.6;}}
.life-card .evidence {{font-size:12px;color:var(--soft);margin-top:8px;padding-top:8px;border-top:1px solid var(--rule);}}
.references {{margin-top:32px;}}
.references h2 {{font-size:18px;margin-bottom:16px;}}
.ref-item {{font-size:12px;line-height:1.5;margin-bottom:8px;padding-left:28px;position:relative;}}
.ref-num {{position:absolute;left:0;color:var(--soft);font-weight:600;}}
.ref-item a {{color:var(--link);text-decoration:none;}}
.ref-item em {{color:var(--muted);}}
footer {{text-align:center;font-size:12px;color:var(--soft);margin-top:40px;padding-top:20px;border-top:1px solid var(--rule);}}
@media (max-width:600px) {{body {{padding:12px;}}.chapter-header {{flex-direction:column;align-items:flex-start;}}}}
</style></head>
<body>
<div class="report-header">
<h1>前沿生物追踪</h1>
<div class="meta">
<div>日期：{d['date']}</div>
<div>今日主题：{' · '.join(c['title'] for c in chapters)}</div>
</div></div>
<div class="summary-card">
<div class="stars">{d['stars']}</div>
<div class="core-conclusion"><strong>一句话核心结论：</strong>{H.escape(d['core_conclusion'])}</div>
<div class="highlights">{highlights}</div>
</div>
<div class="toc"><h3>目录导航</h3><ol>{toc_items}</ol></div>
{chapter_html}
<section class="life-section" id="life"><h2>生活启示</h2>{life_cards}</section>
{vote_html}
<section class="references" id="refs"><h2>参考文献</h2>{refs}</section>
<footer>前沿生物追踪 · {d['date']} · 仅供科普参考，不构成医疗建议</footer>
</body></html>"""
    return html_doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--no-humanize", action="store_true", help="跳过去AI化步骤")
    args = ap.parse_args()
    with open(args.data, encoding="utf-8") as f:
        data = json.load(f)
    doc = render(data)
    # 去AI化修复（L032）
    if not args.no_humanize:
        try:
            from humanize_text import fix_all, detect_all, strip_html
            fixed, results = fix_all(doc)
            pre_score = detect_all(strip_html(doc))["ai_score"]
            post_score = detect_all(strip_html(fixed))["ai_score"]
            doc = fixed
            print(f"[humanize] 去AI化修复: {results['total_fixes']}处 (词汇{results['vocab_fixed']}/空格{results['spacing_fixed']}/连接词{results['connectors_fixed']})")
            print(f"[humanize] AI痕迹指数: {pre_score} → {post_score}")
        except Exception as e:
            print(f"[humanize] 跳过（{e}）")
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"[done] 报告已渲染 -> {args.output} ({len(doc)} 字符)")


if __name__ == "__main__":
    main()