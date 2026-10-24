#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""novelty_gate.py — 自动新颖性闸门（D0 前置核验的执行层）

背景：引擎（insight_scan 的跨领域远链 / reason_engine 的 R1 桥接、R5 同构）能产出
「跨领域、机制上说得通」的候选，但候选是不是**已经被别人做过**，此前只能靠人工临时
检索。于是 D0 闸门虽写着「动笔前必须做近 3 个月抢先核验」，实际执行不可复现、容易漏。
本脚本把这一步自动化：

  对每条候选，拆成「两侧概念词」，在 Europe PMC + Crossref 检索**近 N 天**（默认 90）
  同时提到两侧概念的文章——
    命中且经本地双向确认 → 抢先（候选已被同期文献覆盖，D0 直接否决）
    有命中但未双向确认   → 疑似（需人工看）
    无命中               → 未抢先（可进 D0 后续评估）

「双向确认」是防误报的关键：检索 API 的布尔查询只负责召回，真正的判据是返回文章的
标题/摘要里**同时**出现左侧词与右侧词，且用词边界匹配（避免 CAR 命中 cardiovascular）。

查询词从哪来：候选两侧的**具体实体**（度 < GENERIC_DEG 的非泛化枢纽），经 EN_TERMS
映射成英文检索词——本库实体规范名多为中文，没有这层映射就检索不了英文文献库。

维护 EN_TERMS：它是本脚本唯一需要人工生长的知识资产。脚本会显式列出「因缺英文词
无法核验」的候选，按实际需要补词，不必预先穷举（同 R3 数字普查：机器列候选、人工勾选）。

候选来源（--source）：
  scan     跨领域远链（insight_scan 第五类候选 E，默认）
  bridge   R1 介质桥接（reason_engine）
  analogy  R5 跨层级同构（reason_engine）
  all      三者合并（按 refs 去重）

用法（在项目根运行）：
  python tools\\novelty_gate.py                          # 核验跨领域远链 top 12
  python tools\\novelty_gate.py --source bridge
  python tools\\novelty_gate.py --source all --top 20 --days 90
  python tools\\novelty_gate.py --no-net                 # 只列查询式、不联网（自检/离线）
  python tools\\novelty_gate.py --write learning\\runs\\novelty_gate.md --json tmp\\novelty.json

退出码：0 全部未抢先 / 1 有疑似·缺词·核验失败 / 2 有抢先（闸门否决）
只用标准库。

固化清单（行首 L### 供 check_closure_health.py 识别）：
L046  候选/选题的新颖性核验靠人工临时检索不可复现 → 本脚本自动核验（Europe PMC + Crossref 近 90 天抢先核验，双专有词分档：两侧均命中专有词=抢先否决 / 仅泛化词=疑似 / 无命中=未抢先）
"""

import argparse
import datetime
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True  # 导入 insight_scan/reason_engine 会产生 __pycache__，而自审 R2 正查它

import insight_scan as isc
import reason_engine as eng

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(ROOT, "tools", "novelty_cache.json")

UA = "frontier-bio-daily/1.0 (mailto:you@example.com)"
TIMEOUT = 30
RETRY = 3

WINDOW_DAYS = 90      # 抢先核验窗口：近 3 个月
GENERIC_DEG = 6       # 度 ≥ 该值的实体是泛化枢纽（病毒/代谢），不进查询词
MAX_TERMS_PER_SIDE = 3
DEFAULT_TOP = 12
PAGE_SIZE = 25

# 泛化检索词：太宽泛、不足以支撑「抢先」判定。判定采用「专有词」双门槛——
# 一条命中必须在**两侧都命中专有词**（如 IRE1α + Dlk1-Dio3、TRF + 染色质重塑）才算
# 真抢先；只命中泛化词（缺氧+糖尿病、限时进食+肝脏、癫痫+耐药 这类常见搭配）判「疑似」。
# 这是精度闸门：对新颖性核验而言，误判「抢先」会误杀好选题，代价高于漏报，
# 故门槛偏保守，宁可把边缘命中留给人工看。
GENERIC_TERMS = {
    # 泛过程 / 泛状态
    "hypoxia", "inflammation", "metabolism", "lipid metabolism", "aging", "senescence",
    "apoptosis", "autophagy", "mitophagy", "fibrosis", "infection", "immunity",
    "growth", "development", "pain", "stress", "immune", "signal", "signaling",
    "pathway", "expression", "activity", "function", "role", "effect",
    # 泛病种 / 泛类别
    "cancer", "tumor", "disease", "virus", "resistance", "drug resistance",
    # 泛器官 / 泛区域
    "liver", "spleen", "brain", "intestine", "muscle", "blood", "cervix",
    "lymph node", "prefrontal cortex", "locus coeruleus", "brain region",
    "root hair", "lateral root",
    # 泛生活/统计词
    "diet", "exercise", "social", "patients", "mice", "cell", "cells",
    "protein", "gene", "dna", "rna", "therapy", "treatment", "study", "analysis",
}

VERDICT_ORDER = {"抢先": 3, "疑似": 2, "未抢先": 1, "缺词": 0, "核验失败": 0}

# ---------------------------------------------------------------------------
# EN_TERMS —— 中文规范名 → 英文检索词（本脚本唯一需人工生长的资产）
#
# 为什么需要：entities.json 的规范名多为中文（心磷脂 / 小胶质细胞 / 骨髓瘤），
# 直接丢给 Europe PMC / Crossref 检索不到东西。ASCII 规范名与其 ASCII 别名脚本会
# 自动采用（见 terms_of），只有「纯中文且无 ASCII 别名」的实体才需在此登记。
#
# 登记纪律：一个实体给 1-3 个最标准的英文名/缩写（用词越泛检索越噪，宁可少给）；
# 泛化词（virus / metabolism / inflammation）可不登记——它们会被 GENERIC_DEG 挡在
# 查询词之外。缺词的候选会由脚本列出，按需补，不预先穷举。
# ---------------------------------------------------------------------------
EN_TERMS = {
    # --- 分子 / 蛋白 / 基因 ---
    "组蛋白": ["histone"], "雄激素受体": ["androgen receptor"],
    "卵泡抑素": ["follistatin"], "聚合酶α": ["polymerase alpha"],
    "α-突触核蛋白": ["alpha-synuclein"], "广谱中和抗体": ["broadly neutralizing antibody", "bNAb"],
    "抗体": ["antibody"], "免疫检查点": ["immune checkpoint"],
    "水杨酸信号": ["salicylic acid"],
    # --- 代谢物 ---
    "胆固醇": ["cholesterol"], "心磷脂": ["cardiolipin"], "色氨酸": ["tryptophan"],
    "吲哚": ["indole"], "硫化氢": ["hydrogen sulfide"], "环八硫": ["cyclooctasulfur"],
    "NAD+": ["NAD"], "BCAA": ["branched-chain amino acid"], "GABA": ["GABA"],
    "丝氨酸": ["serine"], "脂质过氧化": ["lipid peroxidation"],
    # --- 细胞 ---
    "小胶质细胞": ["microglia"], "巨噬细胞": ["macrophage"], "树突状细胞": ["dendritic cell"],
    "T细胞": ["T cell"], "B细胞": ["B cell"], "星形胶质细胞": ["astrocyte"],
    "髓系细胞": ["myeloid cell"], "骨细胞": ["osteocyte"], "血清素神经元": ["serotonergic neuron"],
    "血管内皮": ["endothelial cell"], "血小板": ["platelet"], "iPSC": ["induced pluripotent stem cell"],
    "干细胞": ["stem cell"], "衰老细胞": ["senescent cell", "cellular senescence"],
    "Th17": ["Th17"],
    # --- 细胞器 / 结构 ---
    "线粒体": ["mitochondria"], "溶酶体": ["lysosome"], "核糖体": ["ribosome"],
    "核仁": ["nucleolus"], "内质网": ["endoplasmic reticulum"], "脂滴": ["lipid droplet"],
    "细胞器": ["organelle"],
    # --- 过程 / 表型 ---
    "机械力感知": ["mechanosensing", "mechanotransduction"],
    "线粒体自噬": ["mitophagy"], "自噬": ["autophagy"], "铁死亡": ["ferroptosis"],
    "缺氧": ["hypoxia"], "炎症": ["inflammation"], "纤维化": ["fibrosis"],
    "凋亡": ["apoptosis"], "DNA损伤": ["DNA damage"], "DNA甲基化": ["DNA methylation"],
    "DNA复制": ["DNA replication"], "RNA剪接": ["RNA splicing"],
    "去乙酰化": ["deacetylation"], "染色质重塑": ["chromatin remodeling"],
    "染色质可及性": ["chromatin accessibility"], "表观遗传": ["epigenetics"],
    "生物钟": ["circadian"], "基因沉默": ["gene silencing"],
    "胚胎发育": ["embryonic development"], "脂代谢": ["lipid metabolism"],
    "代谢": ["metabolism"], "神经退行": ["neurodegeneration"], "疼痛": ["pain"],
    "产热": ["thermogenesis"], "核糖体移码": ["ribosomal frameshift"],
    "线粒体融合": ["mitochondrial fusion"], "月经周期": ["menstrual cycle"],
    "血栓": ["thrombosis"], "血管老化": ["vascular aging"], "肺损伤": ["lung injury"],
    "抗逆": ["stress tolerance"], "生长": ["growth"], "脑损伤修复": ["brain repair"],
    "耐药": ["drug resistance"], "寿命": ["lifespan", "longevity"],
    "衰老": ["aging", "senescence"], "更年期": ["menopause"],
    "植物免疫": ["plant immunity"], "遗传关联": ["genetic association"],
    "肿瘤突变负荷": ["tumor mutational burden"], "神经样特征": ["neuron-like"],
    # --- 疾病 ---
    "阿尔茨海默病": ["Alzheimer"], "亨廷顿病": ["Huntington"], "多发性硬化": ["multiple sclerosis"],
    "癫痫": ["epilepsy"], "脑胶质瘤": ["glioma"], "胰腺癌": ["pancreatic cancer"],
    "前列腺癌": ["prostate cancer"], "男性乳腺癌": ["male breast cancer"],
    "骨髓瘤": ["myeloma"], "头颈部鳞癌": ["head and neck squamous cell carcinoma"],
    "克罗恩病": ["Crohn"], "乳糜泻": ["celiac"], "自闭症": ["autism"],
    "精神分裂症": ["schizophrenia"], "痴呆": ["dementia"], "囊性纤维化": ["cystic fibrosis"],
    "牙周炎": ["periodontitis"], "糖尿病": ["diabetes"], "肥胖": ["obesity"],
    "自身免疫": ["autoimmune"], "马铃薯晚疫病": ["potato late blight"],
    "黑色素瘤": ["melanoma"], "肿瘤": ["tumor", "cancer"],
    # --- 器官 / 区域 ---
    "脾脏": ["spleen"], "肝脏": ["liver"], "肠道": ["intestine"], "肌肉": ["muscle"],
    "脑": ["brain"], "淋巴结": ["lymph node"], "蓝斑核": ["locus coeruleus"],
    "前额叶皮层": ["prefrontal cortex"], "宫颈": ["cervix"], "血液": ["blood"],
    "脑区": ["brain region"], "根毛": ["root hair"], "侧根": ["lateral root"],
    # --- 物种 / 病原 ---
    "肠道菌群": ["gut microbiota"], "EB病毒": ["Epstein-Barr virus", "EBV"],
    "基孔肯雅": ["chikungunya"], "寨卡": ["Zika"], "埃博拉": ["Ebola"],
    "铜绿假单胞菌": ["Pseudomonas aeruginosa"], "空肠弯曲菌": ["Campylobacter jejuni"],
    "稻瘟病菌": ["Magnaporthe"], "按蚊": ["Anopheles"], "小麦": ["wheat"],
    "大麦": ["barley"], "水稻": ["rice"], "马铃薯": ["potato"],
    "昆虫病毒": ["insect virus"], "溶瘤病毒": ["oncolytic virus"], "病毒": ["virus"],
    # --- 干预 / 药物 / 方法 ---
    "疫苗": ["vaccine"], "mRNA疫苗": ["mRNA vaccine"], "化疗": ["chemotherapy"],
    "放疗": ["radiotherapy"], "免疫治疗": ["immunotherapy"], "CAR-T": ["CAR-T"],
    "电场治疗": ["tumor treating fields"], "限时进食": ["time-restricted feeding"],
    "GLP-1": ["GLP-1"], "左乙拉西坦": ["levetiracetam"], "JAK抑制剂": ["JAK inhibitor"],
    "氟西汀": ["fluoxetine"], "纳米颗粒": ["nanoparticle"], "单细胞": ["single-cell"],
    "空间组学": ["spatial omics"], "多组学": ["multi-omics"], "流式细胞术": ["flow cytometry"],
    "活体成像": ["intravital imaging"], "基因调控网络": ["gene regulatory network"],
    "病毒进化树": ["phylogeny"], "运动": ["exercise"], "饮食": ["diet"],
    "社交": ["social"], "性别差异": ["sex difference"],
}


def _get_json(url, accept="application/json"):
    """带重试的 GET -> (dict|None, err)。404 立即返回不重试（沿用 scan_journals 的做法）。"""
    last = ""
    for i in range(RETRY):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
            return json.loads(raw.decode("utf-8", "replace")), ""
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "404"
            last = "HTTP %s" % e.code
        except Exception as e:
            last = type(e).__name__
        if i < RETRY - 1:
            time.sleep(2 * (i + 1))
    return None, last


# ---------------------------------------------------------------------------
# 查询词
# ---------------------------------------------------------------------------

def is_cjk(s):
    return bool(re.search(r"[\u4e00-\u9fff]", s or ""))


def terms_of(canon, lex):
    """实体规范名 → 英文检索词：EN_TERMS ∪ ASCII 规范名 ∪ ASCII 别名，拆分复合词去重。"""
    raw = list(EN_TERMS.get(canon, []))
    if canon and not is_cjk(canon):
        raw.append(canon)
    for a in (lex.get(canon) or {}).get("aliases", []):
        if a and not is_cjk(a):
            raw.append(a)
    out, seen = [], set()
    for t in raw:
        for part in re.split(r"[/,]", t):
            part = part.strip()
            k = part.lower()
            if part and k not in seen:
                seen.add(k)
                out.append(part)
    return out


def side_terms(ents, lex):
    """一侧的实体集 → 检索词列表（去重、保序）。"""
    out, seen = [], set()
    for e in ents:
        for t in terms_of(e, lex):
            k = t.lower()
            if k not in seen:
                seen.add(k)
                out.append(t)
    return out


def specific_ents(fid, f_ents, degree, lex, exclude, limit=MAX_TERMS_PER_SIDE):
    """一条结论里最「具体」的实体：排除泛化枢纽与已共享实体，按度升序取前 limit 个。

    度越小 = 越专有 = 检索区分度越高。泛化枢纽（度 ≥ GENERIC_DEG）不入选，
    否则查询式会被「病毒 AND …」这类词冲淡。
    """
    cands = [e for e in f_ents.get(fid, set())
             if e not in exclude and degree.get(e, 1) < GENERIC_DEG and terms_of(e, lex)]
    cands.sort(key=lambda e: (degree.get(e, 99), e))
    return cands[:limit]


# ---------------------------------------------------------------------------
# 候选（统一结构：kind / refs / label / ents_a / ents_b / ctx / score / xdom）
# ---------------------------------------------------------------------------

def cands_scan(findings, lex, aidx, mats, min_degree, xdom_min, top):
    """跨领域远链（第五类候选 E）：二跳链取端点，共现取两侧具体实体。"""
    _d, _c, _t, _cf, xdom_rows, _do, _de, degree, f_by_id, _st = \
        isc.scan(findings, lex, min_degree, xdom_min)
    f_ents = eng.f_entity_sets(findings, aidx, mats)
    out = []
    for r in xdom_rows[:top]:
        if r["kind"] == "二跳链":
            s1, _p1, m, _p2, o2 = r["paths"][0]
            out.append({"kind": "跨域二跳链", "refs": [r["a"], r["b"]],
                        "label": f"{s1} → {m} → {o2}",
                        "ents_a": [s1], "ents_b": [o2], "ctx": [m],
                        "score": r["score"], "xdom": r["xdom"]})
        else:
            shared = set(r["shared"])
            ea = specific_ents(r["a"], f_ents, degree, lex, shared)
            eb = specific_ents(r["b"], f_ents, degree, lex, shared)
            if not ea or not eb:
                continue
            out.append({"kind": "跨域共现", "refs": [r["a"], r["b"]],
                        "label": f"{'、'.join(ea)} ⇄ {'、'.join(eb)}",
                        "ents_a": ea, "ents_b": eb, "ctx": sorted(shared),
                        "score": r["score"], "xdom": r["xdom"]})
    return out


def cands_bridge(findings, lex, aidx, mats, media, top):
    """R1 介质桥接：两侧取介质实体，上下文取执行器里的 ASCII 词。"""
    rows, _fm, _fb = eng.r1_bridge(findings, media, aidx, mats)
    out = []
    for r in rows[:top]:
        ctx = []
        for ex in r["executor"]:
            ctx += [p.strip() for p in re.split(r"[/,]", ex) if p.strip() and not is_cjk(p)]
        out.append({"kind": "R1 介质桥接", "refs": [r["a"], r["b"]],
                    "label": f'{media[r["ma"]]["zh"]} × {media[r["mb"]]["zh"]}',
                    "ents_a": list(media[r["ma"]]["entities"]),
                    "ents_b": list(media[r["mb"]]["entities"]),
                    "ctx": ctx, "score": r["score"], "xdom": r["xdom"]})
    return out


def cands_analogy(findings, lex, aidx, top):
    """R5 跨层级同构：两侧取被比较的两个中介节点。"""
    analogy, _ch, _gr = eng.r5_analogy(findings, lex, aidx)
    out = []
    for a in analogy[:top]:
        c1, c2 = a["c1"], a["c2"]
        out.append({"kind": "R5 跨层级同构",
                    "refs": sorted({c1["f1"], c1["f2"], c2["f1"], c2["f2"]}),
                    "label": f'{c1["m"]} ≅ {c2["m"]}',
                    "ents_a": [c1["m"]], "ents_b": [c2["m"]],
                    "ctx": [c1["y"], c2["y"]], "score": 0.0, "xdom": a["xdom"]})
    return out


# ---------------------------------------------------------------------------
# 检索（Europe PMC + Crossref）
# ---------------------------------------------------------------------------

def _clean(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", str(s))
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", s).strip()


def norm_text(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def has_term(text_norm, term):
    t = norm_text(term)
    if not t:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text_norm) is not None


def epmc_query(ta, tb, dfrom, dto):
    A = " OR ".join('"%s"' % t for t in ta)
    B = " OR ".join('"%s"' % t for t in tb)
    return "(%s) AND (%s) AND (FIRST_PDATE:[%s TO %s])" % (A, B, dfrom, dto)


def crossref_query(ta, tb, dfrom, dto):
    return "query.bibliographic=%s&filter=from-pub-date:%s,until-pub-date:%s" % (
        urllib.parse.quote(" ".join(ta[:3] + tb[:3])), dfrom, dto)


def norm_doi(d):
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(d or "").strip().lower()).rstrip(".")


def epmc_search(query):
    """Europe PMC：优先 core（带摘要），空则退回 lite（L023）。返回 (items, err)。"""
    base = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    last_err = ""
    for rtype in ("core", "lite"):
        url = base + "?" + urllib.parse.urlencode({
            "query": query, "format": "json", "resultType": rtype,
            "pageSize": str(PAGE_SIZE), "sort": "P_PDATE_D desc"})
        data, err = _get_json(url)
        if data is None:
            last_err = err
            continue
        res = data.get("resultList", {}).get("result", []) or []
        items = []
        for r in res:
            items.append({
                "doi": norm_doi(r.get("doi")),
                "title": _clean(r.get("title")),
                "abstract": _clean(r.get("abstractText")),
                "date": (r.get("firstPublicationDate") or "")[:10],
                "journal": _clean(r.get("journalTitle") or ""),
                "source": "europepmc",
            })
        return items, ""
    return [], last_err


def crossref_search(ta, tb, dfrom, dto):
    """Crossref：按书目检索召回，本地再双向确认。返回 (items, err)。"""
    params = crossref_query(ta, tb, dfrom, dto)
    url = "https://api.crossref.org/works?" + params + \
        "&rows=%d&select=DOI,title,abstract,type,published,created,container-title" % PAGE_SIZE
    data, err = _get_json(url)
    if data is None:
        return [], err
    items = []
    for it in data.get("message", {}).get("items", []) or []:
        title = _clean((it.get("title") or [""])[0])
        doi = norm_doi(it.get("DOI"))
        if not doi.startswith("10."):
            continue
        d = ""
        for key in ("published", "published-online", "created"):
            v = it.get(key)
            if isinstance(v, dict) and v.get("date-parts") and v["date-parts"][0]:
                parts = list(v["date-parts"][0]) + [1, 1]
                try:
                    d = datetime.date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
                except (ValueError, TypeError):
                    d = ""
                if d:
                    break
        items.append({"doi": doi, "title": title, "abstract": _clean(it.get("abstract")),
                      "date": d, "journal": _clean((it.get("container-title") or [""])[0]),
                      "source": "crossref"})
    return items, ""


def is_generic(term):
    return term.strip().lower() in GENERIC_TERMS


def fmt_terms(terms):
    """专有词加 ★ 前缀，便于人一眼看出「是哪一侧拖了后腿」。"""
    return "/".join(("★" + t if not is_generic(t) else t) for t in terms)


def confirm(items, ta, tb):
    """双向确认：标题/摘要里**同时**出现左侧词与右侧词才算命中。

    再按专有词分档：两侧**都**命中专有词 = 强命中（抢先）；否则 = 弱命中（疑似）。
    """
    out = []
    for it in items:
        text = norm_text((it.get("title") or "") + " " + (it.get("abstract") or ""))
        ma = [t for t in ta if has_term(text, t)]
        mb = [t for t in tb if has_term(text, t)]
        if not ma or not mb:
            continue
        core_a = [t for t in ma if not is_generic(t)]
        core_b = [t for t in mb if not is_generic(t)]
        out.append({"item": it, "ma": ma, "mb": mb, "core_a": core_a, "core_b": core_b,
                    "strong": bool(core_a) and bool(core_b)})
    return out


# ---------------------------------------------------------------------------
# 缓存（避免重复联网；按查询式 + 窗口 + 来源签名）
# ---------------------------------------------------------------------------

def load_cache():
    if os.path.isfile(CACHE_PATH):
        try:
            return json.load(io.open(CACHE_PATH, encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def save_cache(store):
    d = os.path.dirname(CACHE_PATH)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with io.open(CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False, indent=1, sort_keys=True)


def _sig(*parts):
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def cached_fetch(store, key, fn, use_cache, cache_days):
    now = time.time()
    if use_cache and key in store and now - store[key].get("at", 0) <= cache_days * 86400:
        return store[key].get("items", []), ""
    items, err = fn()
    if not err:
        store[key] = {"at": now, "items": items}
    return items, err


# ---------------------------------------------------------------------------
# 单条候选核验
# ---------------------------------------------------------------------------

def gate_one(cand, lex, store, args, dfrom, dto):
    ta = side_terms(cand["ents_a"], lex)
    tb = side_terms(cand["ents_b"], lex)
    row = dict(cand, terms_a=ta, terms_b=tb, hits=[], epmc_q="", crossref_q="", note="")
    if not ta or not tb:
        row["verdict"] = "缺词"
        row["note"] = "两侧至少一侧无英文检索词，须补 EN_TERMS"
        return row
    row["epmc_q"] = epmc_query(ta, tb, dfrom, dto)
    row["crossref_q"] = crossref_query(ta, tb, dfrom, dto)
    if args.no_net:
        row["verdict"] = "未核验"
        row["note"] = "离线模式（--no-net）"
        return row

    e_items, e_err = cached_fetch(
        store, "epmc:" + _sig(row["epmc_q"]),
        lambda: epmc_search(row["epmc_q"]), not args.no_cache, args.cache_days)
    c_items, c_err = cached_fetch(
        store, "crossref:" + _sig(row["crossref_q"]),
        lambda: crossref_search(ta, tb, dfrom, dto), not args.no_cache, args.cache_days)

    seen, items = set(), []
    for it in (e_items or []) + (c_items or []):
        if it["doi"] and it["doi"] in seen:
            continue
        seen.add(it["doi"])
        items.append(it)
    row["n_epmc"] = len(e_items or [])
    row["n_crossref"] = len(c_items or [])
    row["n_raw"] = len(items)
    row["hits"] = confirm(items, ta, tb)
    row["hits"].sort(key=lambda h: (not h["strong"], h["item"].get("date") or ""))
    strong = [h for h in row["hits"] if h["strong"]]
    # 判据只看「确认命中」：Crossref 的书目检索是模糊排序，几乎总会返回满页，
    # 召回数本身不构成抢先证据（EuropePMC 的布尔召回才有信息量，故单独列出）。
    if e_err and c_err:
        row["verdict"] = "核验失败"
        row["note"] = "两源均失败：%s / %s" % (e_err, c_err)
    elif strong:
        row["verdict"] = "抢先"
    elif row["hits"]:
        best = row["hits"][0]
        row["verdict"] = "疑似"
        row["note"] = ("命中未达双专有词门槛（左专有词 %s｜右专有词 %s）"
                       % ("/".join(best["core_a"]) or "无", "/".join(best["core_b"]) or "无"))
    else:
        row["verdict"] = "未抢先"
        row["note"] = "无双向确认命中"
    return row


def render(rows, args, dfrom, dto, sources):
    L = ["=" * 70,
         "自动新颖性闸门（novelty_gate · 抢先核验）",
         "=" * 70,
         "候选来源：%s｜窗口：%s ~ %s（近 %d 天）｜联网：%s"
         % ("、".join(sources), dfrom, dto, args.days, "否（--no-net）" if args.no_net else "是"),
         ""]
    if not rows:
        L.append("  （无候选）")
        return "\n".join(L)
    for r in rows:
        L.append("[%s] %s｜%s（%s）" % (r["verdict"], r["kind"], r["label"], "、".join(r["refs"])))
        L.append("      左：%s ｜ 右：%s%s"
                 % ("/".join(r["terms_a"]) or "—", "/".join(r["terms_b"]) or "—",
                    ("  ｜ 上下文：%s" % "/".join(r["ctx"])) if r["ctx"] else ""))
        if r["verdict"] == "缺词":
            L.append("      · %s" % r["note"])
        elif r["verdict"] in ("未核验", "核验失败"):
            L.append("      · %s" % r["note"])
        elif r["verdict"] == "抢先":
            strong = [h for h in r["hits"] if h["strong"]]
            L.append("      · 强命中 %d 篇（两侧均命中专有词；EuropePMC 召回 %d｜Crossref 召回 %d）："
                     % (len(strong), r.get("n_epmc", 0), r.get("n_crossref", 0)))
            for h in strong[:5]:
                it = h["item"]
                L.append("        - %s（%s）%s" % (it["date"] or "无日期", it["doi"], it["title"][:76]))
                L.append("          命中词：左 %s ｜ 右 %s" % (fmt_terms(h["ma"]), fmt_terms(h["mb"])))
        elif r["hits"]:
            L.append("      · 弱命中 %d 篇（EuropePMC 召回 %d｜Crossref 召回 %d）——%s"
                     % (len(r["hits"]), r.get("n_epmc", 0), r.get("n_crossref", 0), r["note"]))
            for h in r["hits"][:3]:
                it = h["item"]
                L.append("        - %s（%s）%s" % (it["date"] or "无日期", it["doi"], it["title"][:76]))
                L.append("          命中词：左 %s ｜ 右 %s" % (fmt_terms(h["ma"]), fmt_terms(h["mb"])))
        else:
            L.append("      · %s（EuropePMC 召回 %d｜Crossref 召回 %d）"
                     % (r["note"] or "无双向确认命中", r.get("n_epmc", 0), r.get("n_crossref", 0)))
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="自动新颖性闸门（D0 前置核验执行层）")
    ap.add_argument("--source", default="scan",
                    choices=["scan", "bridge", "analogy", "all"], help="候选来源")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP, help="每源核验条数上限")
    ap.add_argument("--days", type=int, default=WINDOW_DAYS, help="抢先核验窗口（天）")
    ap.add_argument("--min-degree", type=int, default=2, help="insight_scan 可配对实体度门槛")
    ap.add_argument("--xdom-min", type=float, default=isc.XDOM_MIN, help="跨领域远链展示门槛")
    ap.add_argument("--no-net", action="store_true", help="只列查询式，不联网")
    ap.add_argument("--no-cache", action="store_true", help="忽略缓存，强制联网")
    ap.add_argument("--cache-days", type=int, default=7, help="缓存有效期（天）")
    ap.add_argument("--sleep", type=float, default=0.4, help="候选之间间隔秒数（限流保护）")
    ap.add_argument("--write", default=None, help="把结果落盘为 markdown")
    ap.add_argument("--json", default=None, help="把结果落盘为 JSON")
    args = ap.parse_args()

    for p in (isc.FINDINGS, isc.ENTITIES, eng.MEDIA):
        if not os.path.isfile(p):
            print("[F] 读不到 %s" % p)
            return 2

    text = isc.read_text(isc.FINDINGS)
    findings, _links = isc.parse_findings(text)
    lex = isc.load_lexicon(isc.ENTITIES)
    media = eng.load_media(eng.MEDIA)
    aidx = isc.build_alias_index(lex)
    mats = isc.compile_matchers(lex)

    sources, cands = [], []
    if args.source in ("scan", "all"):
        sources.append("跨领域远链")
        cands += cands_scan(findings, lex, aidx, mats, args.min_degree, args.xdom_min, args.top)
    if args.source in ("bridge", "all"):
        sources.append("R1 介质桥接")
        cands += cands_bridge(findings, lex, aidx, mats, media, args.top)
    if args.source in ("analogy", "all"):
        sources.append("R5 跨层级同构")
        cands += cands_analogy(findings, lex, aidx, args.top)

    # 去重（同一 refs 对只核验一次）
    uniq, seen = [], set()
    for c in cands:
        key = tuple(sorted(c["refs"]))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)

    today = datetime.date.today()
    dfrom = (today - datetime.timedelta(days=args.days)).isoformat()
    dto = today.isoformat()

    store = load_cache()
    rows = []
    for i, c in enumerate(uniq, 1):
        rows.append(gate_one(c, lex, store, args, dfrom, dto))
        if not args.no_net and i < len(uniq):
            time.sleep(args.sleep)
    if not args.no_net:
        save_cache(store)

    print(render(rows, args, dfrom, dto, sources))
    n = {v: sum(1 for r in rows if r["verdict"] == v) for v in
         ("抢先", "疑似", "未抢先", "缺词", "核验失败", "未核验")}
    print("-" * 70)
    print("核验 %d 条：抢先 %d｜疑似 %d｜未抢先 %d｜缺词 %d｜核验失败 %d%s"
          % (len(rows), n["抢先"], n["疑似"], n["未抢先"], n["缺词"], n["核验失败"],
             ("｜未核验 %d" % n["未核验"]) if n["未核验"] else ""))
    if n["缺词"]:
        print("  缺英文词的候选已在上面列出，按需补 novelty_gate.EN_TERMS。")

    if args.write:
        d = os.path.dirname(args.write)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with io.open(args.write, "w", encoding="utf-8") as fh:
            fh.write("# 自动新颖性闸门结果（novelty_gate 自动生成）\n\n")
            fh.write("> 判据：近 %d 天内已有文章**同时**提到候选两侧概念 → 抢先（D0 否决）。\n"
                     "> 「未抢先」只代表窗口内无同期覆盖，不等于绝对新颖，仍须人工查重。\n\n"
                     % args.days)
            fh.write("```\n" + render(rows, args, dfrom, dto, sources) + "\n```\n")
        print("已落盘：%s" % args.write)

    if args.json:
        d = os.path.dirname(args.json)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        payload = {
            "checked_at": today.isoformat(),
            "window": {"from": dfrom, "to": dto, "days": args.days},
            "sources": sources,
            "offline": bool(args.no_net),
            "counts": n,
            "candidates": [{
                "kind": r["kind"], "refs": r["refs"], "label": r["label"],
                "terms_a": r["terms_a"], "terms_b": r["terms_b"], "ctx": r["ctx"],
                "score": r.get("score"), "xdom": r.get("xdom"),
                "verdict": r["verdict"], "n_epmc": r.get("n_epmc", 0),
                "n_crossref": r.get("n_crossref", 0),
                "hits": [{"doi": h["item"]["doi"], "date": h["item"]["date"],
                          "title": h["item"]["title"], "journal": h["item"]["journal"],
                          "source": h["item"]["source"], "strong": h["strong"],
                          "matched_a": h["ma"], "matched_b": h["mb"],
                          "core_a": h["core_a"], "core_b": h["core_b"]} for h in r["hits"]],
                "note": r["note"],
            } for r in rows],
        }
        with io.open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print("明细已写入 %s" % args.json)

    if n["抢先"]:
        return 2
    if n["疑似"] or n["缺词"] or n["核验失败"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
