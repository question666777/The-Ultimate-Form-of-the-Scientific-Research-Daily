# -*- coding: utf-8 -*-
"""前沿生物追踪 · 中文写作质量提升（常驻脚本）。

检测中文文本的 AI 腔（高频词堆砌、句长单一、排比结构），给出修改建议并支持自动修复。
定位是**写得更好**，不是规避检测（2026-10-13 修订，见 learning/proposals/2026-10-13.md 提案 1）。
规则来源：
  - stephenlzc/humanize-mba-text-skill（中文AI词汇+句长CV+排比检测）
  - harshaneel/humanize（9杠杆：perplexity injection, burstiness enforcement, hedge surgery等）
  - brandonwise/humanizer（28种AI写作模式）

固化清单：
L031 去AI自动化痕迹：footer/所用技能/每日日报不暴露自动化生成
L032 中文AI检测规则固化：词汇层+句式层+结构层+统计指标
L035 词汇替换不碰HTML结构性标签：abstract-label/intro-label等，避免一致性检查失败

用法：
    python humanize_text.py --html HTML/2026-09-24.html          # 检测AI痕迹
    python humanize_text.py --html HTML/2026-09-24.html --fix     # 检测+自动修复
    python humanize_text.py --html HTML/2026-09-24.html --json tmp/humanize.json
    python humanize_text.py --text "一段中文文本"                   # 检测纯文本
"""
import argparse
import io
import json
import math
import re
import sys

# ============================================================
# 规则1：AI高频词汇（中文）
# 来源：humanize-mba-text-skill/language.toml
# ============================================================
AI_VOCAB = {
    # 互联网黑话型
    "赋能": "支持/帮助",
    "抓手": "切入点/方法",
    "闭环": "完整流程",
    "痛点": "难题/问题",
    "赛道": "领域/方向",
    "打通": "连通",
    "心智": "认知",
    "颗粒度": "精细程度",
    "护城河": "优势",
    "组合拳": "一整套方法",
    "生态系统": "体系",
    "生态圈": "圈子",
    "生态链": "产业链",
    "对齐": "对齐/统一",
    "沉淀": "积累",
    "拉齐": "统一",
    "底层逻辑": "基本原理",
    "顶层设计": "整体规划",
    "方法论": "方法",
    # AI行话型
    "值得注意的是": "",
    "综上所述": "",
    "基于以上": "",
    "不难发现": "",
    "由此可见": "",
    "总的来说": "",
    "总而言之": "",
    "众所周知": "",
    "毋庸置疑": "",
    "不言而喻": "",
    "显而易见": "",
    "需要指出的是": "",
    "与此同时": "同时",
    "在此基础上": "接着",
    "从某种意义上说": "",
    "从另一个角度来看": "",
    "更重要的是": "",
    "更为关键的是": "",
    "尤为值得一提的是": "",
    "除此之外": "另外",
    "进一步来说": "",
    "深入探讨": "讨论",
    "深入分析": "分析",
    # "深入解读": "解读",  # 已移除——是报告结构性标签(abstract-label)，误改会导致一致性检查失败 L035
    "深度剖析": "分析",
    "全面解析": "解析",
    "一文读懂": "",
    "一文了解": "",
    "干货满满": "",
    # 过度学术/程式化
    "本研究": "这项研究",
    "本研究表明": "研究发现",
    "本研究的": "这项研究的",
    "本研究中": "在这项研究中",
    "本研究发现": "研究发现",
    "结果表明": "结果显示",
    "研究表明": "研究发现",
    "研究指出": "研究发现",
    "研究认为": "研究者认为",
    "研究证实": "研究证实",
    "研究发现": "研究发现",  # 保留但统计频率
    "大量研究表明": "多项研究发现",
    "已有研究表明": "之前的研究发现",
    "既往研究": "之前的研究",
    "现有研究": "目前的研究",
    "亟待进一步": "需要",
    "进一步验证": "验证",
    "进一步探索": "探索",
    "进一步研究": "后续研究",
    "未来研究方向": "后续可研究",
    "具有重要的临床意义": "对临床有帮助",
    "具有重要的理论意义": "在理论上有价值",
    "具有广阔的应用前景": "应用前景不错",
    "为后续研究奠定了基础": "为后续研究打了基础",
    "提供了新的视角": "提供了新思路",
    "提供了新的思路": "提供了新思路",
    "开辟了新的途径": "开辟了新路",
    "为...提供了重要参考": "可供参考",
    "具有里程碑意义": "是个重要进展",
    "有望成为": "可能成为",
    "或将成为": "可能成为",
    "被认为是": "是",
    "被广泛认为是": "是",
    "被公认为": "是",
    "被证实": "已证实",
    "被证明": "已证明",
}

# 高频但不替换的词（只统计频率，不替换）
FREQ_ONLY = {"研究发现", "研究证实"}

# ============================================================
# 规则2：三段式排比检测
# ============================================================
THREE_PART_PATTERNS = [
    r"一是[，,].*?二是[，,].*?三是",
    r"首先[，,].*?其次[，,].*?(?:最后|再次|最终)",
    r"第一[，,].*?第二[，,].*?第三",
    r"一方面[，,].*?另一方面",
    r"其一[，,].*?其二[，,].*?其三",
    r"第[一二三][，,]",
]

# ============================================================
# 规则3：否定式排比
# ============================================================
NEGATIVE_PARALLELISM = [
    r"不是[^，。；]*?[，,]?\s*而是",
    r"不仅[^。]*?[，,]?\s*更[是是]",
    r"既[^。]*?[，,]?\s*又",
    r"不仅不[^。]*?[，,]?\s*反而",
]

# ============================================================
# 规则4：过度连接词
# ============================================================
EXCESSIVE_CONNECTORS = [
    "此外", "另外", "同时", "与此同时", "另一方面",
    "不仅如此", "更为重要的是", "更关键的是", "更值得注意的是",
    "在此基础上", "基于此", "由此", "因此", "所以",
    "然而", "不过", "但是", "尽管如此",
    "换言之", "简而言之", "归根结底", "追根溯源",
    "归根到底", "说到底",
]

# 同一段落中出现3个以上连接词 = 过度使用
CONNECTOR_THRESHOLD = 3

# ============================================================
# 规则5：AI句式模板
# ============================================================
AI_SENTENCE_TEMPLATES = [
    (r"在.{1,10}方面", "「在X方面」过于程式化"),
    (r"随着.{1,15}的发展", "「随着X的发展」AI开场白"),
    (r"在当今.{1,10}时代", "「在当今X时代」AI开场白"),
    (r"在.{1,10}背景下", "「在X背景下」程式化"),
    (r"作为.{1,15}的重要(组成|部分)", "「作为X的重要组成部分」程式化"),
    (r"扮演着.{1,10}角色", "「扮演着X角色」程式化"),
    (r"发挥(着|了).{0,10}作用", "「发挥着X作用」程式化"),
    (r"为.{1,15}提供了.{1,10}参考", "「为X提供了参考」程式化"),
    (r"具有.{1,10}意义", "「具有X意义」程式化"),
    (r"对.{1,10}具有.{1,10}意义", "「对X具有意义」程式化"),
    (r"有望(成为|推动|促进|改善|实现)", "「有望X」AI预测口吻"),
    (r"或将成为", "AI预测口吻"),
    (r"具有重要(的)?(理论|实践|临床|现实)意义", "AI套话"),
    (r"为.{1,10}奠定了.{1,10}基础", "AI套话"),
]


def split_sentences(text):
    """中文分句（按。！？；分，保留分隔符）。"""
    parts = re.split(r'([。！？；\n])', text)
    sentences = []
    for i in range(0, len(parts) - 1, 2):
        s = parts[i] + parts[i + 1]
        s = s.strip()
        if s:
            sentences.append(s)
    if len(parts) % 2 == 1 and parts[-1].strip():
        sentences.append(parts[-1].strip())
    return [s for s in sentences if s]


def count_cjk(text):
    """统计中文字符数（CJK Unified Ideographs）。"""
    return sum(1 for c in text if '\u4e00' <= c <= '\u9fff')


def strip_html(raw):
    """剥离HTML标签，保留纯文本。"""
    t = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.S | re.I)
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t]+", " ", t.strip())


def detect_ai_vocabulary(text):
    """检测AI高频词汇。"""
    hits = []
    for word, replacement in AI_VOCAB.items():
        count = len(re.findall(re.escape(word), text))
        if count > 0:
            hits.append({
                "word": word,
                "count": count,
                "replacement": replacement if word not in FREQ_ONLY else "(仅统计频率)",
            })
    return hits


def detect_parallelism(text):
    """检测三段式排比和否定式排比。"""
    hits = []
    for pat in THREE_PART_PATTERNS:
        matches = list(re.finditer(pat, text, re.S))
        for m in matches:
            hits.append({
                "type": "三段式排比",
                "pattern": pat,
                "evidence": m.group(0)[:80],
                "suggestion": "打散为自然语序，去掉编号词",
            })
    for pat in NEGATIVE_PARALLELISM:
        matches = list(re.finditer(pat, text, re.S))
        for m in matches:
            hits.append({
                "type": "否定式排比",
                "pattern": pat,
                "evidence": m.group(0)[:80],
                "suggestion": "直接陈述正面观点",
            })
    return hits


def detect_connectors(text):
    """检测过度连接词使用。"""
    paragraphs = re.split(r'[\n。]', text)
    hits = []
    for i, para in enumerate(paragraphs):
        para = para.strip()
        if len(para) < 10:
            continue
        found = []
        for conn in EXCESSIVE_CONNECTORS:
            count = len(re.findall(re.escape(conn), para))
            if count > 0:
                found.append({"word": conn, "count": count})
        if len(found) >= CONNECTOR_THRESHOLD:
            hits.append({
                "paragraph_index": i,
                "evidence": para[:100],
                "connectors": found,
                "suggestion": f"段落中连接词过多({len(found)}个)，删除部分过渡词",
            })
    return hits


def detect_sentence_templates(text):
    """检测AI句式模板。"""
    hits = []
    for pat, desc in AI_SENTENCE_TEMPLATES:
        matches = list(re.finditer(pat, text))
        if matches:
            hits.append({
                "pattern": pat,
                "description": desc,
                "count": len(matches),
                "evidence": matches[0].group(0)[:80],
            })
    return hits


def calculate_burstiness(sentences):
    """计算burstiness（句子长度突发性）。

    人类写作burstiness高（长短句交替），AI低（均匀）。
    用句长的变异系数CV衡量：
      CV = std / mean
      CV < 0.3 → AI特征明显
      CV > 0.5 → 接近人类
    """
    if len(sentences) < 3:
        return {"cv": 0, "assessment": "样本不足", "flag": False}
    lengths = [count_cjk(s) for s in sentences]
    n = len(lengths)
    mean = sum(lengths) / n
    if mean == 0:
        return {"cv": 0, "assessment": "空文本", "flag": False}
    variance = sum((x - mean) ** 2 for x in lengths) / n
    std = math.sqrt(variance)
    cv = std / mean
    if cv < 0.3:
        assessment = "句长过于均匀（AI特征）"
        flag = True
    elif cv < 0.5:
        assessment = "句长较为均匀（偏AI）"
        flag = True
    else:
        assessment = "句长变化自然"
        flag = False
    return {
        "cv": round(cv, 3),
        "mean_length": round(mean, 1),
        "max_length": max(lengths),
        "min_length": min(lengths),
        "assessment": assessment,
        "flag": flag,
    }


def detect_short_sentence_ratio(sentences):
    """检测短句比例。

    人类写作中每150字至少1个短句（≤6字）。
    AI倾向于每句都差不多长。
    """
    total_chars = sum(count_cjk(s) for s in sentences)
    short_sentences = sum(1 for s in sentences if count_cjk(s) <= 6 and count_cjk(s) > 0)
    if total_chars == 0:
        return {"ratio": 0, "flag": False}
    short_per_150 = short_sentences / (total_chars / 150) if total_chars > 0 else 0
    return {
        "total_chars": total_chars,
        "short_sentences": short_sentences,
        "short_per_150_chars": round(short_per_150, 2),
        "flag": short_per_150 < 1.0 and total_chars > 150,
        "suggestion": "每150字应有至少1个短句(≤6字)来制造节奏感" if short_per_150 < 1.0 else "",
    }


def calculate_ai_score(detections):
    """综合AI评分（0-100，越高越像AI）。"""
    score = 0
    # 词汇层
    vocab_count = sum(d["count"] for d in detections["vocabulary"])
    score += min(25, vocab_count * 3)
    # 句式层
    score += min(20, len(detections["templates"]) * 5)
    # 排比
    score += min(15, len(detections["parallelism"]) * 5)
    # 连接词
    score += min(15, len(detections["connectors"]) * 5)
    # burstiness
    burst = detections["burstiness"]
    if burst["flag"]:
        if burst["cv"] < 0.3:
            score += 15
        elif burst["cv"] < 0.5:
            score += 8
    # 短句比例
    if detections["short_ratio"]["flag"]:
        score += 10
    return min(100, score)


def detect_all(text):
    """执行全部检测。"""
    sentences = split_sentences(text)
    detections = {
        "vocabulary": detect_ai_vocabulary(text),
        "parallelism": detect_parallelism(text),
        "connectors": detect_connectors(text),
        "templates": detect_sentence_templates(text),
        "burstiness": calculate_burstiness(sentences),
        "short_ratio": detect_short_sentence_ratio(sentences),
    }
    detections["ai_score"] = calculate_ai_score(detections)
    detections["total_sentences"] = len(sentences)
    return detections


def fix_vocabulary(text):
    """自动替换AI高频词汇。"""
    fixed = 0
    for word, replacement in AI_VOCAB.items():
        if word in FREQ_ONLY:
            continue
        if replacement:
            count = text.count(word)
            text = text.replace(word, replacement)
            fixed += count
        else:
            # 连同后随标点一起删除：若只把「更重要的是，」换成「，」，
            # 前面已有的句号会留下「。，」这种悬空标点（2026-09-30 修复）
            for p in (word + "，", word + "。", word + "、", word):
                count = text.count(p)
                if count > 0:
                    text = text.replace(p, "")
                    fixed += count
                    break
    # 清理悬空/重复标点
    text = re.sub(r'[，、]{2,}', '，', text)
    text = re.sub(r'([。！？；])[，、]', r'\1', text)
    text = re.sub(r'^[，、]', '', text, flags=re.M)
    return text, fixed


def fix_spacing(text):
    """修复中英文混排空格：AI论文中常见中文和英文/数字之间有空格。"""
    fixes = 0
    # 中文+空格+英文/数字 → 中文+英文/数字
    text, n = re.subn(r'([\u4e00-\u9fff])\s+([A-Za-z0-9])', r'\1\2', text)
    fixes += n
    # 英文/数字+空格+中文 → 英文/数字+中文
    text, n = re.subn(r'([A-Za-z0-9])\s+([\u4e00-\u9fff])', r'\1\2', text)
    fixes += n
    return text, fixes


def fix_connectors(text):
    """删除段首过度连接词。"""
    fixes = 0
    for conn in ["此外，", "另外，", "不仅如此，", "与此同时，"]:
        count = text.count(conn)
        text = text.replace(conn, "")
        fixes += count
    return text, fixes


def fix_all(text):
    """执行全部自动修复。"""
    results = {}
    text, v = fix_vocabulary(text)
    results["vocab_fixed"] = v
    text, s = fix_spacing(text)
    results["spacing_fixed"] = s
    text, c = fix_connectors(text)
    results["connectors_fixed"] = c
    results["total_fixes"] = v + s + c
    return text, results


def main():
    ap = argparse.ArgumentParser(description="中文去AI化检测与修复")
    ap.add_argument("--html", help="HTML文件路径")
    ap.add_argument("--text", help="纯文本输入")
    ap.add_argument("--fix", action="store_true", help="自动修复")
    ap.add_argument("--json", help="输出JSON报告到指定路径")
    args = ap.parse_args()

    # 获取文本
    if args.html:
        with io.open(args.html, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        text = strip_html(raw)
        # 如果要修复，对原始HTML操作
        target = raw
    elif args.text:
        text = args.text
        target = args.text
    else:
        ap.error("请用 --html 或 --text 提供输入")

    # 检测
    detections = detect_all(text)

    # 打印报告
    print("=" * 62)
    print("去AI化检测报告")
    print("=" * 62)
    print(f"\n【综合评分】AI痕迹指数: {detections['ai_score']}/100", end=" ")
    if detections['ai_score'] >= 60:
        print("⚠ 高风险（像AI生成）")
    elif detections['ai_score'] >= 30:
        print("⚠ 中等风险")
    else:
        print("✓ 低风险")

    print(f"\n【句子统计】共 {detections['total_sentences']} 句")
    b = detections["burstiness"]
    print(f"\n【句长突发性】CV={b['cv']}（均值{b.get('mean_length','?')}字，范围{b.get('min_length','?')}-{b.get('max_length','?')}字）")
    print(f"  评估：{b['assessment']}")
    if b["flag"]:
        print(f"  ⚠ 句长过于均匀，需要长短句交替")

    sr = detections["short_ratio"]
    print(f"\n【短句比例】{sr['short_sentences']}个短句/共{sr['total_chars']}字")
    if sr["flag"]:
        print(f"  ⚠ {sr['suggestion']}")

    if detections["vocabulary"]:
        print(f"\n【AI高频词汇】({len(detections['vocabulary'])}种)")
        for v in detections["vocabulary"][:15]:
            repl = f" → 替换为「{v['replacement']}」" if v["replacement"] and v["replacement"] != "(仅统计频率)" else ""
            print(f"  「{v['word']}」出现{v['count']}次{repl}")

    if detections["templates"]:
        print(f"\n【AI句式模板】({len(detections['templates'])}种)")
        for t in detections["templates"]:
            print(f"  {t['description']}（{t['count']}次）：「{t['evidence']}」")

    if detections["parallelism"]:
        print(f"\n【排比结构】({len(detections['parallelism'])}处)")
        for p in detections["parallelism"]:
            print(f"  {p['type']}：{p['evidence']}")
            print(f"    → {p['suggestion']}")

    if detections["connectors"]:
        print(f"\n【过度连接词】({len(detections['connectors'])}处)")
        for c in detections["connectors"]:
            print(f"  段落{c['paragraph_index']}：{len(c['connectors'])}个连接词")
            print(f"    → {c['suggestion']}")

    # 修复
    if args.fix:
        fixed_text, fix_results = fix_all(target if args.html else text)
        print(f"\n{'=' * 62}")
        print(f"自动修复完成：共 {fix_results['total_fixes']} 处")
        print(f"  词汇替换: {fix_results['vocab_fixed']}")
        print(f"  空格修复: {fix_results['spacing_fixed']}")
        print(f"  连接词删除: {fix_results['connectors_fixed']}")
        if args.html:
            with io.open(args.html, "w", encoding="utf-8") as f:
                f.write(fixed_text)
            print(f"  已写回: {args.html}")
        # 修复后重新检测
        fixed_plain = strip_html(fixed_text) if args.html else fixed_text
        post = detect_all(fixed_plain)
        print(f"\n修复后AI痕迹指数: {post['ai_score']}/100")
        print(f"  修复前: {detections['ai_score']} → 修复后: {post['ai_score']}")
        detections["post_fix_score"] = post["ai_score"]
        detections["fixes"] = fix_results

    # JSON输出
    if args.json:
        with io.open(args.json, "w", encoding="utf-8") as f:
            json.dump(detections, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n报告已写入: {args.json}")

    # 退出码
    score = detections.get("post_fix_score", detections["ai_score"])
    return 2 if score >= 60 else (1 if score >= 30 else 0)


if __name__ == "__main__":
    sys.exit(main())
