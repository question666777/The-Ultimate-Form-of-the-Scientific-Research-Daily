#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
select_topics.py — 自适应选题（第 2 步固化）

把原先每天现写的选题逻辑固化成脚本，固化 lessons L015 / L016 / L029 与
sentinel_pool 自更新规则，让"主题轮换"不再依赖临场发挥。

固化清单
--------
L015  近 3 天回避清单应以 topic_state.json 的 history 实际记录为准，
      只遍历 date ∈ [今天-3, 今天-1] **且成功写入**的条目；失败日不臆造回避项
L016  漏斗式分层读取：本脚本**只输出计数+标题**，不把全量 JSON 读进上下文
L029  聚类只匹配标题（不带摘要片段），关键词用领域专有词组
      （alzheimer/parkinson 而非 tau/memory；sars-cov/hiv 而非 virus）
L0xx  主题名归一化（2026-09-22 新增，见下）
L0xx  字符替换 bug 防御（2026-09-22 新增，见下）
池规则 哨兵池由 history 派生（每次重算，非增量追加）→ 取最近使用的 POOL_MAX 个
       （2026-10-01：候选总表由 22 扩到 31，池上限同步由 10 提到 20）
轮换   哨兵池真正参与选题：判冷 = 掉出池 或 距上次使用 ≥ COOLDOWN_DAYS 或 从未用过；
       有命中的冷主题在排序时 +ROTATION_BONUS，强制覆盖面铺开（2026-10-01 新增）

主题覆盖 候选总表必须覆盖 skills_taxonomy.json 全部「领域深挖」学科域，
       映射见 DOMAIN_TO_TOPIC；`--coverage` 校验，新增学科域技能时须同步登记。

字符替换 bug 防御（关键）
--------------------------
生成端存在字符替换问题（抗体→抵体、肿瘤→肿牤、小胶质→小膝质 等），
且**已污染 topic_state.json 本体**：实测 sentinel_pool 里躺着「肿牤生物学与癌症治疗」。
若不修，脏名字会随池回流到后续报告。本脚本对所有主题名与标题先过
normalize_chars()（CHAR_FIXES 表），保证入池/输出干净。

主题名归一化（关键）
--------------------
历史 10 期报告一共出现过 21 种「第N章」名，但真正复用的主题只有 ~16 个：
同一主题被不同日子写成不同名字（9-13「神经退行性疾病与脑健康」vs 9-18「神经退行性疾病」；
9-18「病原体变异与新发传染病监测」vs 9-22「病原体变异与传染病监测」）。
若按字符串精确匹配，回避清单会静默失效、sentinel_pool 会堆近义重复项。
因此本脚本把名字收敛到 CANONICAL 规范名：
  - 打分用 CANONICAL 的别名集合（任一别名命中即计入该规范主题）
  - 回避清单 / 池 / 输出统一用 CANONICAL 名
  - history 里的历史别名由 resolve_alias() 反查归一，兼容老数据

用法
----
    python select_topics.py --scan tmp/scan_results.json --top 3
    python select_topics.py --scan tmp/scan_results.json --top 3 --write
    python select_topics.py --scan tmp/scan_results.json --top 3 --write --note "扫描:Crossref 507篇"
    python select_topics.py --ls-aliases        # 打印别名表
    python select_topics.py --coverage          # 校验主题总表是否覆盖全部技能学科域

输出
----
    标准输出：回避清单 / 各候选主题命中数 / 推荐 3 主题 / 每主题 top5 标题
    --write 时才写回 topic_state.json（先自动备份）

退出码：0 成功（选满 --top）/ 1 告警（选不满，需人工看一眼）/ 2 参数或数据错误
只用标准库。
"""

import argparse
import datetime
import io
import json
import os
import re
import shutil
import sys

# ---------------------------------------------------------------------------
# 规范主题表
#   CANONICAL_TOPICS[规范名] = [别名...]（别名仅用于**归一化历史数据**）
#   MATCH_WORDS[规范名]      = [标题命中词组...]（用于对扫描结果打分）
# 打分词组遵守 L029：只用领域专有词组，禁用 tau / memory / virus 等通用词
# ---------------------------------------------------------------------------

CANONICAL_TOPICS = {
    "病原体变异与新发传染病监测": [
        "病原体变异与传染病监测", "病原体变异与新发传染病监测",
        "病原体变异与传染病监测与预警",
    ],
    "神经退行性疾病": [
        "神经退行性疾病与脑健康", "神经退行性疾病",
    ],
    "神经科学与脑图谱": [
        "神经科学与脑图谱", "神经科学", "神经环路与脑功能",
    ],
    "植物与农业生物技术": [
        "植物与农业生物技术", "植物科学与农业生物技术", "植物抗逆与农业生物技术",
    ],
    "肿瘤生物学与癌症治疗": [
        "肿瘤生物学与癌症治疗", "肿瘤免疫逃逸机制与治疗突破",
        "肿瘤免疫治疗新靶点", "肿瘤生物学",
    ],
    "基因编辑与基因治疗": [
        "基因编辑与基因治疗", "基因编辑", "基因治疗",
    ],
    "免疫学与新型免疫疗法": [
        "免疫学与新型免疫疗法", "免疫学与免疫治疗", "新型免疫疗法",
    ],
    "代谢与营养生物学": [
        "代谢与营养生物学", "代谢与肥胖糖尿病", "代谢生物学",
    ],
    "微生物组与宿主互作": [
        "微生物组与肠道微生态", "微生物组与宿主互作", "肠道菌群与宿主互作",
    ],
    "RNA生物学与RNA治疗": [
        "RNA生物学与RNA治疗", "RNA生物学", "RNA治疗",
    ],
    "干细胞与再生医学": [
        "干细胞与再生医学", "干细胞生物学与再生医学",
    ],
    "衰老生物学与长寿干预": [
        "衰老生物学与长寿干预", "衰老生物学与干预",
    ],
    "宿主-病原体互作与抗感染免疫": [
        "宿主-病原体互作与抗感染免疫", "宿主-病原体互作", "抗感染免疫",
    ],
    "表观遗传与染色质": [
        "表观遗传与染色质", "表观遗传与染色质调控", "表观遗传学",
    ],
    "结构生物学与蛋白设计": [
        "结构生物学与冷冻电镜", "结构生物学与蛋白设计",
        "AI蛋白质设计与结构预测", "蛋白质设计与结构预测",
    ],
    "心血管与代谢疾病": [
        "心血管与血管生物学", "心血管与代谢疾病", "心血管生物学",
    ],
    "线粒体生物学与代谢重编程": [
        "线粒体生物学与代谢重编程", "线粒体生物学",
    ],
    "单细胞与空间组学": [
        "单细胞与空间组学", "单细胞组学", "空间转录组",
    ],
    "神经免疫交叉对话": [
        "神经免疫交叉对话", "神经免疫互作",
    ],
    "致幻剂的药理机制与神经保护": [
        "致幻剂的药理机制与神经保护", "致幻剂药理",
    ],
    "发育生物学": [
        "发育生物学", "胚胎发育与器官发生",
    ],
    "合成生物学": [
        "合成生物学", "合成生物学与基因回路",
    ],
    # ---- 2026-10-01 扩充：按 skills_taxonomy.json 的「领域深挖」学科域补齐盲区 ----
    "基因组学与变异解读": [
        "基因组学与变异解读", "基因组学与遗传变异", "遗传变异与基因组医学",
    ],
    "药物发现与化学信息学": [
        "药物发现与化学信息学", "药物化学与虚拟筛选", "小分子药物发现",
    ],
    "医学影像与数字病理": [
        "医学影像与数字病理", "医学影像与影像组学", "数字病理与影像分析",
    ],
    "蛋白质组学与质谱": [
        "蛋白质组学与质谱", "蛋白质组学与代谢组学", "质谱分析",
    ],
    "系统发育与进化生物学": [
        "系统发育与进化生物学", "进化生物学与比较基因组", "系统发育与演化",
    ],
    "系统生物学与代谢工程": [
        "系统生物学与代谢工程", "系统生物学与代谢网络", "代谢工程",
    ],
    "临床AI与医疗数据科学": [
        "临床AI与医疗数据科学", "临床机器学习与医疗AI", "医疗人工智能",
    ],
    "显微成像与生物图像分析": [
        "显微成像与生物图像分析", "生物成像与图像分析", "显微成像技术",
    ],
    "药理学与药代动力学": [
        "药理学与药代动力学", "药理学与药物代谢", "PK/PD 与给药方案",
    ],
}

MATCH_WORDS = {
    "病原体变异与新发传染病监测": [
        "sars-cov", "covid", "influenza", "h5n1", "h3n2", "rsv",
        "respiratory syncytial", "mpox", "measles", "dengue", "chikungunya",
        "zika", "ebola", "nipah", "lineage", "outbreak", "epidemic",
        "surveillance", "pathogen", "neutraliz", "antiviral",
        "tuberculosis", "malaria",
    ],
    "神经退行性疾病": [
        "alzheimer", "parkinson", "huntington", "amyotrophic",
        "dementia", "neurodegener", "amyloid", "tau protein", "alpha-synuclein",
        "microglia", "locus coeruleus", "prion", "lewy body", "cognitive decline",
    ],
    "神经科学与脑图谱": [
        "neural circuit", "synap", "cortex", "hippocamp", "thalamus",
        "dopamine", "serotonin", "connectome", "brain atlas", "electrophysiolog",
        "astrocyte", "oligodendrocyte", "axon guid", "dendrit", "brain map",
    ],
    "植物与农业生物技术": [
        "plant", "rice", "maize", "wheat", "arabidopsis", "crop", "phytophthora",
        "salicylic", "jasmon", "potato", "soybean", "photosynth",
        "pathogen resistance", "stomata", "crop yield", "agricultur",
    ],
    "肿瘤生物学与癌症治疗": [
        "cancer", "tumor", "tumour", "oncogen", "carcinoma", "leukemia", "lymphoma",
        "melanoma", "metasta", "chemotherap", "immunotherap", "pd-1", "pd-l1",
        "car-t", "checkpoint", "glioblastoma", "neoplasm",
    ],
    "基因编辑与基因治疗": [
        "crispr", "cas9", "base edit", "prime edit", "gene therap", "gene edit",
        "aav", "lentiviral vector", "sgrna", "genome edit", "zinc finger",
        "exon skipping", "gene delivery",
    ],
    "免疫学与新型免疫疗法": [
        "immune", "t cell", "b cell", "cytokine", "interleukin",
        "inflammat", "macrophage", "dendritic cell", "mhc", "tcr",
        "autoimmun", "vaccin", "immunotherap",
    ],
    "代谢与营养生物学": [
        "obesity", "diabet", "insulin", "glucose", "lipid",
        "adipose", "diet", "nutrition", "caloric", "fasting", "brown fat",
        "metabolic",
    ],
    "微生物组与宿主互作": [
        "microbiome", "microbiota", "gut bacteri", "commensal", "probiotic",
        "dysbiosis", "16s rrna", "metagenom", "enterobacteria",
    ],
    "RNA生物学与RNA治疗": [
        "mrna", "lncrna", "circrna", "mirna", "splicing", "ribosom",
        "antisense oligonucleotide", "sirna", "rnai", "exon junction",
    ],
    "干细胞与再生医学": [
        "stem cell", "ipsc", "pluripoten", "organoid", "regenerat",
        "differentiat", "reprogramming", "progenitor cell", "gastruloid",
    ],
    "衰老生物学与长寿干预": [
        "aging", "ageing", "senescen", "longevity", "lifespan", "telomere",
        "cellular senescence", "rapamycin", "caloric restriction",
    ],
    "宿主-病原体互作与抗感染免疫": [
        "host-pathogen", "innate immun", "interferon", "phagocytosis",
        "nlrp3", "inflammasome", "sepsis", "viral entry", "bacterial infection",
    ],
    "表观遗传与染色质": [
        "epigenet", "chromatin", "histone", "methylation", "acetylat",
        "polycomb", "enhancer", "chromosome conformation", "hic",
    ],
    "结构生物学与蛋白设计": [
        "crystal structure", "cryo-em", "protein structure", "alphafold",
        "protein design", "binding affinity", "conformational", "domain structure",
    ],
    "心血管与代谢疾病": [
        "cardiac", "cardiovascular", "heart failure", "atheroscler",
        "hypertens", "myocard", "vascular", "endothelial", "stroke",
    ],
    "线粒体生物学与代谢重编程": [
        "mitochondri", "oxidative phosphoryl", "electron transport chain",
        "mitochondrial metabol", "metabolic reprogramming",
    ],
    "单细胞与空间组学": [
        "single-cell", "single cell", "spatial transcriptom", "scrna",
        "atac-seq", "spatial omics", "single-nucleus",
    ],
    "神经免疫交叉对话": [
        "neuroimmune", "neuro-inflammat", "brain immun", "microglia-neuron",
    ],
    "致幻剂的药理机制与神经保护": [
        "psilocybin", "psychedelic", "lsd", "ketamine", "serotonergic hallucinogen",
    ],
    "发育生物学": [
        "embryo", "morphogen", "gastrulat", "organogenesis",
        "neural tube", "somite", "zygote", "blastocyst", "lineage tracing",
    ],
    "合成生物学": [
        "synthetic biolog", "genetic circuit", "engineered bacteri", "biosensor",
        "metabolic engineering", "chassis", "genome minimization",
    ],
    # ---- 2026-10-01 扩充：新增学科域的强词组（遵守 L029：只用领域专有词组） ----
    "基因组学与变异解读": [
        "gwas", "genome-wide association", "variant interpretation",
        "polygenic", "pangenome", "structural variant", "copy number variant",
        "haplotype", "whole-genome sequencing", "genome assembly",
        "long-read sequencing", "telomere-to-telomere", "rare variant",
    ],
    "药物发现与化学信息学": [
        "drug discovery", "chemoinformatic", "virtual screening",
        "molecular docking", "admet", "pharmacophore", "lead compound",
        "structure-based drug", "de novo design", "drug target",
        "molecular glue", "covalent inhibitor", "protac",
    ],
    "医学影像与数字病理": [
        "radiomic", "digital pathology", "histopatholog", "medical imaging",
        "whole-slide", "radiology", "magnetic resonance imaging",
        "computed tomography", "image segmentation", "diagnostic imaging",
    ],
    "蛋白质组学与质谱": [
        "proteomic", "mass spectrometry", "metabolomic", "lipidomic",
        "post-translational modification", "phosphoproteom", "protein abundance",
        "single-cell proteom", "ion mobility",
    ],
    "系统发育与进化生物学": [
        "phylogen", "comparative genomic", "natural selection", "speciation",
        "molecular clock", "convergent evolution", "ancestral reconstruction",
        "lineage divergence",
    ],
    "系统生物学与代谢工程": [
        "systems biology", "genome-scale metabolic", "flux balance",
        "metabolic model", "constraint-based", "metabolic network model",
        "biosynthetic pathway",
    ],
    "临床AI与医疗数据科学": [
        "clinical machine learning", "electronic health record",
        "real-world evidence", "clinical prediction model",
        "clinical decision support", "health informatics", "medical artificial intelligence",
        "patient outcome prediction",
    ],
    "显微成像与生物图像分析": [
        "super-resolution microscopy", "cryo-electron tomography",
        "live-cell imaging", "fluorescence microscopy", "bioimage analysis",
        "expansion microscopy", "single-molecule imaging", "correlative light",
    ],
    "药理学与药代动力学": [
        "pharmacokinetic", "pharmacodynamic", "drug metabolism",
        "dose optimization", "therapeutic window", "drug-drug interaction",
        "bioavailability", "cytochrome p450",
    ],
}

POOL_MAX = 20  # sentinel_pool 上限（随候选总表 22→31 扩充，2026-10-01 由 10 提到 20）
AVOID_DAYS = 3  # 近 3 天回避（硬回避：命中即跳过）

# 轮换加权（2026-10-01 新增）：让「哨兵池」真正影响选题，而不是只被写入。
# 目的——31 个候选主题若只按命中篇数排序，高产的少数主题会长期霸榜，覆盖面反而变窄。
# 规则——某主题距上次使用 ≥ COOLDOWN_DAYS 天（或从未用过），在排序时加 ROTATION_BONUS 分。
#   权重标定：冷却够久 ≈ 多命中 1 篇的价值。因此强证据仍能压过轮换分（8 篇 > 1 篇+1 分），
#   证据持平时（各 1 篇）则优先给久未露面的主题，把覆盖铺开。
COOLDOWN_DAYS = 21
ROTATION_BONUS = 1.0

# 入选资格：**至少命中一个强词组**（非 WEAK_WORDS）。
# 实测 22 篇真实标题的得分分布只有 0.5 与 1.0 两档（期刊标题很短，罕有多词共现），
# 所以不能用"分数 >= N"当门槛，否则会把全部候选滤空。
# 正确规则：弱词单独命中不算数（例如代谢论文标题里出现 sars-cov 对照），
# 必须有一个领域专有强词支撑。MIN_SCORE 保留为极小值，仅用于表达"必须 > 0"。
MIN_SCORE = 1.0

# 非研究性条目：勘误/社论/新闻等不该作为选题证据（AGENTS 第 5 节：
# 窗口内的期刊收录/版本更新不构成进展条目）。仅按**标题前缀**判定，
# 避免误杀正文里出现 "correction" 的真论文。
NON_RESEARCH_PREFIXES = (
    "author correction", "publisher correction", "correction to",
    "erratum", "retraction", "expression of concern", "editorial",
    "corrigendum", "a correction",
)

# 通用/易误伤词组：单独命中不足以把文章归入该主题（L029 延伸）。
# 例如 "sars-cov" 出现在代谢论文标题的对照说明里。
WEAK_WORDS = {
    "sars-cov", "covid", "rsv", "vaccin", "vaccine", "metabolic", "diet",
    "infection", "protein", "brain", "cancer", "immune", "aging", "dopamine",
    "evolutionary",  # 肿瘤演化论文常出现，单命中不足以归入系统发育主题
}

# 反查表：别名 -> 规范名
ALIAS_TO_CANON = {}
for _canon, _aliases in CANONICAL_TOPICS.items():
    ALIAS_TO_CANON[_canon] = _canon
    for _a in _aliases:
        ALIAS_TO_CANON[_a] = _canon

# ---------------------------------------------------------------------------
# 技能域 → 候选主题 映射（2026-10-01 新增）
#   目的：项目装了什么技能，就应有对应主题可选题，避免「技能在、主题缺」的盲区。
#   键=skills_taxonomy.json 的 category（K-Dense 官方分类），值=规范主题名。
#   用 `python select_topics.py --coverage` 校验；新增学科域技能时必须同步登记。
# ---------------------------------------------------------------------------
DOMAIN_TO_TOPIC = {
    "Scientific Packages > Bioinformatics & Genomics": [
        "单细胞与空间组学", "表观遗传与染色质", "RNA生物学与RNA治疗",
        "基因编辑与基因治疗", "基因组学与变异解读", "微生物组与宿主互作",
    ],
    "Scientific Packages > Protein Engineering & Design": ["结构生物学与蛋白设计"],
    "Scientific Packages > Cheminformatics & Drug Discovery": ["药物发现与化学信息学"],
    "Scientific Packages > Neuroscience & Electrophysiology": ["神经科学与脑图谱"],
    "Scientific Packages > Medical Imaging & Digital Pathology": ["医学影像与数字病理"],
    "Scientific Packages > Proteomics & Mass Spectrometry": ["蛋白质组学与质谱"],
    "Scientific Packages > Phylogenetics & Evolutionary Biology": ["系统发育与进化生物学"],
    "Scientific Packages > Materials Science & Chemistry": ["系统生物学与代谢工程"],
    "Scientific Packages > Healthcare AI & Clinical Machine Learning": ["临床AI与医疗数据科学"],
    "Scientific Integrations > Microscopy & Bio-image Data": ["显微成像与生物图像分析"],
    "Scientific Packages > Pharmacology & Pharmacometrics": ["药理学与药代动力学"],
}

# ---------------------------------------------------------------------------
# 字符替换 bug 修复表（2026-09-22 实测：生成端有字符替换问题，
# 且已污染 topic_state.json 本体——pool 里躺着「肿牤生物学与癌症治疗」）
# 键=错字，值=正字。normalize_chars() 对外一律先过这一层。
CHAR_FIXES = {
    "抵体": "抗体",
    "小膝质": "小胶质",
    "水杌酸": "水杨酸",
    "创车": "刹车",
    "初车": "刹车",
    "留尼汯": "留尼汪",
    "去甲肾腰素": "去甲肾上腺素",
    "膝质": "胶质",
    "盐滍": "耐盐",
    "肿牤": "肿瘤",
    "肿廇": "肿瘤",
}


def normalize_chars(text):
    """修复已知字符替换 bug。用于主题名与标题（防止脏数据进池/进报告）。"""
    if not text:
        return text
    for bad, good in CHAR_FIXES.items():
        if bad in text:
            text = text.replace(bad, good)
    return text


def resolve_alias(name):
    """把任意历史/当前主题名收敛到规范名。未知名字原样返回（不丢信息）。
    先修字符 bug，再查别名表，最后宽松兜底。"""
    if not name:
        return name
    key = normalize_chars(name).strip()
    if key in ALIAS_TO_CANON:
        return ALIAS_TO_CANON[key]
    # 宽松兜底：去空白与连接符后再比
    squashed = re.sub(r"[\s\-－—·]+", "", key)
    for alias, canon in ALIAS_TO_CANON.items():
        if re.sub(r"[\s\-－—·]+", "", alias) == squashed:
            return canon
    return key


def load_state(path):
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def get_avoid_list(state, today_str):
    """L015：只看 history 里 date ∈ [today-3, today-1] 且成功写入的条目。
    返回 (规范名集合, 来源日期说明列表)。"""
    today = datetime.date.fromisoformat(today_str)
    lo = today - datetime.timedelta(days=AVOID_DAYS)
    hi = today - datetime.timedelta(days=1)
    avoid, used_dates = set(), []
    for h in state.get("history", []) or []:
        d = (h.get("date") or "")[:10]
        try:
            dd = datetime.date.fromisoformat(d)
        except ValueError:
            continue
        if lo <= dd <= hi:
            topics = h.get("topics") or []
            if topics:  # 失败日无 topics，不臆造
                for t in topics:
                    avoid.add(resolve_alias(t))
                used_dates.append("%s(%d)" % (d, len(topics)))
    return avoid, used_dates


def get_last_used(state):
    """每个规范主题最近一次被使用的日期（遍历全部 history，不限窗口）。
    用于轮换加权：越久没用过，越该轮到它。返回 {规范名: date}。"""
    last = {}
    for h in state.get("history", []) or []:
        d = (h.get("date") or "")[:10]
        try:
            dd = datetime.date.fromisoformat(d)
        except ValueError:
            continue
        for t in h.get("topics") or []:
            c = resolve_alias(t)
            if c not in last or dd > last[c]:
                last[c] = dd
    return last


def staleness(last_used, topic, today_str):
    """距上次使用的天数；从未用过返回 None（调用方按最大优先级处理）。"""
    d = last_used.get(topic)
    if not d:
        return None
    return (datetime.date.fromisoformat(today_str) - d).days


def is_research_item(it):
    """过滤勘误/社论等非研究性条目。基于标题前缀，不误杀正文含 correction 的真论文。"""
    title = (it.get("title") or "").strip().lower()
    for p in NON_RESEARCH_PREFIXES:
        if title.startswith(p):
            return False
    return True


def score_topics(items):
    """L029：只匹配标题，不碰摘要。返回 {规范主题: [(score, item), ...]}。

    计分：强词组命中 +1，弱词组（WEAK_WORDS）命中 +0.5。
    这样单靠 "sars-cov" 之类的通用词无法把文章拉进主题（需 >=MIN_SCORE）。
    """
    hits = {t: [] for t in MATCH_WORDS}
    skipped = 0
    for it in items:
        if not is_research_item(it):
            skipped += 1
            continue
        title = normalize_chars(it.get("title") or "").lower()
        if not title:
            continue
        for topic, words in MATCH_WORDS.items():
            score = 0.0
            strong = []
            for w in words:
                if w in title:
                    if w in WEAK_WORDS:
                        score += 0.5
                    else:
                        score += 1.0
                        strong.append(w)
            if score > 0:
                hits[topic].append((score, strong, it))
    for t in hits:
        hits[t].sort(key=lambda x: -x[0])
    return hits, skipped


def derive_pool(state, today_str, selected):
    """由 history（+ 今日入选）派生哨兵池：按最近使用时间倒序去重的规范主题，取前 POOL_MAX 个。
    关键——池不再增量追加，而是每次从 history 重算。增量维护会漏项失真
    （2026-10-01 实测：池只有 10 项，却漏掉了 2 天前刚用过的主题），
    而池要参与轮换判冷，失真会直接导致「刚用过的主题被误判为冷」。
    返回 (新池, 相对旧池的新增项, 被淘汰项)。"""
    entries = []
    for h in state.get("history", []) or []:
        d = (h.get("date") or "")[:10]
        for t in h.get("topics") or []:
            entries.append((d, resolve_alias(t)))
    for t in selected:
        entries.append((today_str, resolve_alias(t)))
    # 日期倒序；同日保持写入顺序（stable sort）
    entries.sort(key=lambda x: x[0], reverse=True)

    old_pool = [resolve_alias(t) for t in (state.get("sentinel_pool") or []) if t]
    pool, seen = [], set()
    for _d, t in entries:
        if t not in seen:
            seen.add(t)
            pool.append(t)
    dropped = pool[POOL_MAX:]
    pool = pool[:POOL_MAX]
    added = [t for t in pool if t not in old_pool]
    return pool, added, dropped


def check_coverage():
    """校验候选主题总表是否覆盖 skills_taxonomy.json 的全部「领域深挖」学科域。"""
    tax_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills_taxonomy.json")
    if not os.path.isfile(tax_path):
        print("[错误] 找不到 %s" % tax_path)
        return 2
    with io.open(tax_path, encoding="utf-8") as fh:
        tax = json.load(fh)

    by_cat = {}
    for name, meta in (tax.get("skills") or {}).items():
        if meta.get("role") != "领域深挖":
            continue
        by_cat.setdefault(meta.get("category") or "未收录", []).append(name)

    n_skill = sum(len(v) for v in by_cat.values())
    print("领域深挖技能域 → 候选主题（技能 %d 个｜学科域 %d 个）" % (n_skill, len(by_cat)))
    print("-" * 62)

    fails = []
    for cat in sorted(by_cat):
        skills = sorted(by_cat[cat])
        if cat == "未收录":  # 未分类技能（源库也没有归类），不构成学科域盲区
            print("  [未分类] %s：%s（源库未归类，不计入覆盖）" % (cat, ", ".join(skills)))
            continue
        if cat not in DOMAIN_TO_TOPIC:
            fails.append("%s：未在 DOMAIN_TO_TOPIC 中登记（%d 个技能）" % (cat, len(skills)))
            print("  [未登记] %s（%s）" % (cat, ", ".join(skills[:4])))
            continue
        topics = DOMAIN_TO_TOPIC[cat]
        valid = [t for t in topics if t in CANONICAL_TOPICS]
        bad = [t for t in topics if t not in CANONICAL_TOPICS]
        if bad:
            fails.append("%s：主题名不在总表里 → %s" % (cat, ", ".join(bad)))
        if not valid and not bad:
            fails.append("%s：未指派任何主题" % cat)
        print("  %-34s ← %d 个技能：%s" % (
            " / ".join(valid) if valid else "（空）", len(skills), ", ".join(skills[:4])))

    pointed = set()
    for topics in DOMAIN_TO_TOPIC.values():
        pointed.update(topics)
    orphan = [t for t in CANONICAL_TOPICS if t not in pointed]

    print("-" * 62)
    print("候选主题总表 %d 个｜被技能域指向 %d 个｜未指向（历史/跨域主题）%d 个" % (
        len(CANONICAL_TOPICS), len(pointed & set(CANONICAL_TOPICS)), len(orphan)))
    if orphan:
        print("  未指向：%s" % " / ".join(orphan))
    if fails:
        print("[失败] %d 处未覆盖：" % len(fails))
        for f in fails:
            print("    - %s" % f)
        return 2
    print("[通过] 全部「领域深挖」学科域均有候选主题")
    return 0


def main():
    ap = argparse.ArgumentParser(description="自适应选题（第 2 步）")
    ap.add_argument("--scan", help="scan_journals.py 产出的 JSON")
    ap.add_argument("--state", default=os.path.join("tools", "topic_state.json"))
    ap.add_argument("--top", type=int, default=3, help="推荐主题数（默认 3）")
    ap.add_argument("--date", help="覆盖日期（默认今天）")
    ap.add_argument("--write", action="store_true", help="写回 topic_state.json")
    ap.add_argument("--note", default="", help="写入 history 的备注")
    ap.add_argument("--ls-aliases", action="store_true", help="打印别名表后退出")
    ap.add_argument("--coverage", action="store_true", help="校验候选主题是否覆盖全部技能学科域后退出")
    args = ap.parse_args()

    if args.coverage:
        return check_coverage()

    if args.ls_aliases:
        print("规范主题 → 别名（共 %d 个规范主题）" % len(CANONICAL_TOPICS))
        for canon in CANONICAL_TOPICS:
            others = [a for a in CANONICAL_TOPICS[canon] if a != canon]
            print("  %s" % canon)
            for a in others:
                print("      ← %s" % a)
        return 0

    if not args.scan:
        print("[错误] 必须给 --scan（或 --ls-aliases）")
        return 2
    if not os.path.isfile(args.scan):
        print("[错误] 找不到扫描结果：%s" % args.scan)
        return 2
    if not os.path.isfile(args.state):
        print("[错误] 找不到状态文件：%s" % args.state)
        return 2

    today_str = args.date or datetime.date.today().isoformat()
    with io.open(args.scan, encoding="utf-8") as fh:
        scan = json.load(fh)
    items = scan.get("items", []) or []
    if not items:
        print("[错误] 扫描结果里没有条目")
        return 2

    state = load_state(args.state)
    avoid, used = get_avoid_list(state, today_str)

    print("候选文献：%d 篇（窗口 %s ~ %s）" % (
        len(items), scan.get("window", {}).get("from", "?"), scan.get("window", {}).get("to", "?")))
    print("回避清单（近 %d 天 history，实际有记录的）：%d 个主题" % (AVOID_DAYS, len(avoid)))
    if used:
        print("  来源日期：%s" % ", ".join(used))
    else:
        print("  来源日期：（无成功记录，不臆造回避项 —— L015）")
    if avoid:
        for t in sorted(avoid):
            print("    - %s" % t)
    print("-" * 62)

    hits, skipped = score_topics(items)
    if skipped:
        print("已排除 %d 篇勘误/社论（非研究性，不作选题证据）" % skipped)

    # 轮换加权：让哨兵池真正参与选题（2026-10-01）
    #   「冷」= 掉出哨兵池（不在最近 POOL_MAX 个用过的主题里）或 距上次使用已超冷却期/从未用过。
    #   池由 history 派生（derive_pool），因此判冷不会因池失真而误伤刚用过的主题。
    last_used = get_last_used(state)
    pool_set = set(resolve_alias(t) for t in (state.get("sentinel_pool") or []) if t)

    # 排序：轮换加权后的有效分 > 强词总数 > 最高分
    ranked = []
    for topic, lst in hits.items():
        qualified = [x for x in lst if x[0] >= MIN_SCORE]
        n = len(qualified)
        best = qualified[0][0] if qualified else 0
        strongest = sum(len(x[1]) for x in qualified)
        days = staleness(last_used, topic, today_str)
        stale = days is None or days >= COOLDOWN_DAYS
        out_of_pool = topic not in pool_set
        cold = stale or out_of_pool
        # 轮换分只加给「有证据」的主题：零命中的主题无论如何都选不上，
        # 给它加分只会把空主题顶进排行、污染人工判读（2026-10-01）
        bonus = ROTATION_BONUS if (cold and n > 0) else 0.0
        eff = n + bonus
        ranked.append({"eff": eff, "n": n, "strongest": strongest, "best": best,
                       "days": days, "cold": cold, "stale": stale, "out_of_pool": out_of_pool,
                       "topic": topic, "bonus": bonus, "qualified": qualified})
    ranked.sort(key=lambda r: (-r["eff"], -r["strongest"], -r["best"]))

    n_never = sum(1 for r in ranked if r["days"] is None)
    print("主题命中排行（前 12；达标 = 至少 1 个强词组；★轮换 = 有命中且判冷，+%.1f）"
          % ROTATION_BONUS)
    print("  判冷口径：掉出哨兵池（池 %d/%d 项） 或 距上次使用≥%d天 或 从未用过"
          % (len(pool_set), POOL_MAX, COOLDOWN_DAYS))
    for r in ranked[:12]:
        marks = []
        if r["topic"] in avoid:
            marks.append("[回避]")
        if r["bonus"] > 0:
            why = []
            if r["stale"]:
                why.append("久未用")
            if r["out_of_pool"]:
                why.append("出池")
            marks.append("★轮换(%s)" % "+".join(why))
        dstr = "从未用过" if r["days"] is None else ("%d 天前" % r["days"])
        print("  %4.1f 分  %3d 篇  强词 %2d  上次 %-8s %s  %s" % (
            r["eff"], r["n"], r["strongest"], dstr, r["topic"], " ".join(marks)))
    print("  （候选 %d 个主题中，%d 个从未用过）" % (len(ranked), n_never))

    # 选题：跳过回避项、跳过不达标
    selected = []
    for r in ranked:
        if len(selected) >= args.top:
            break
        if r["topic"] in avoid:
            continue
        if r["n"] == 0:
            continue
        selected.append(r["topic"])

    if len(selected) < args.top:
        print("\n[警告] 只选出 %d 个主题（目标 %d）—— 候选不足或被回避占满" % (len(selected), args.top))
        print("       建议：放宽窗口 / 增加期刊 / 检查回避清单 / 补充主题词表")

    print("-" * 62)
    print("推荐主题：")
    for i, t in enumerate(selected, 1):
        qualified = [x for x in hits[t] if x[0] >= MIN_SCORE]
        print("\n  %d. %s（达标 %d 篇）" % (i, t, len(qualified)))
        for score, strong, it in qualified[:5]:
            print("     [%g] %s" % (score, (it.get("title") or "")[:72]))
            if strong:
                print("          强词：%s" % ", ".join(strong))
            print("          %s | %s | %s" % (it.get("journal", "?"), it.get("date", "?"), it.get("doi", "?")))

    print("-" * 62)
    if args.write:
        bak = args.state + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M")
        shutil.copy2(args.state, bak)
        pool, added, dropped = derive_pool(state, today_str, selected)
        state["sentinel_pool"] = pool
        state["updated"] = today_str
        state.setdefault("history", []).append({
            "date": today_str,
            "topics": selected,
            "note": args.note or ("选题：%s" % " / ".join(selected)),
        })
        with io.open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        print("已写回 %s（备份 %s）" % (args.state, os.path.basename(bak)))
        print("  入池新增：%s" % (added or "无"))
        print("  末位淘汰：%s" % (dropped or "无"))
        print("  池当前 %d 个：%s" % (len(state["sentinel_pool"]), " / ".join(state["sentinel_pool"])))
        print("  history 追加 %s 一条" % today_str)
    else:
        print("（未写回状态文件。加 --write 才落盘）")
    print("提示：本脚本只输出计数与标题，不把原始 JSON 读进上下文 —— L016")

    # 选不满 = 当日选题会缺版块，属需人工介入的告警
    if len(selected) < args.top:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
