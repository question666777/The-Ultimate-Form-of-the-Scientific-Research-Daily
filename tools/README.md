# tools/ 工具说明

流水线的可复用脚本。**改这里的任何脚本前先读本文件**。

## 冻结接口（改动等于改公共 API，必须同步更新所有调用方）

| 脚本 | 冻结项 |
|---|---|
| `send_email.py` | CLI 参数：`--file <html> --subject <主题> [--test]` |
| `config.ini` | `[email]` 段字段名（授权码非登录密码） |
| `topic_state.json` | 字段结构：`updated` / `history[].date` / `history[].topics` / `history[].note` / `sentinel_pool` |

## 脚本清单

| 脚本 | 作用 | 依赖 |
|---|---|---|
| `send_email.py` | 邮件引擎（163 SMTP 自发自收） | 标准库 |
| `generate_report.py` | 由 report_data.json 渲染成品 HTML | 标准库 |
| `scan_journals.py` | **第 1 步·广角扫描**（54 刊 Crossref 拉取 + DOI 标题反查） | 标准库 + 联网 |
| `select_topics.py` | **第 2 步·自适应选题**（31 个候选主题 + 主题归一 + 回避清单 + sentinel_pool 由 history 派生并参与轮换加权 + 技能域覆盖校验） | 标准库 |
| `check_report_consistency.py` | **报告自洽性确定性校验**（审计第 9 维） | 标准库 |
| `check_retractions.py` | **撤稿/更正状态核查**（审计第 10 维） | 标准库 + 联网 |
| `check_closure_health.py` | **闭环健康检查**（审计第 11 维：6项检查含经验库结构健康+提案积压+假active） | 标准库 |
| `check_skills.py` | **技能库体检**（审计第 4 维：职能分类覆盖/核心层缺位/依赖就绪度/激活近似/作业线引用校验） | 标准库 |
| `skills_taxonomy.json` | 技能职能分类快照（由源库官方分类树派生，非手填；`check_skills.py` 读取） | 数据文件 |
| `skill_playbooks.json` | 作业线配方（场景→调用顺序，人工判断；`check_skills.py` 校验引用不失效） | 数据文件 |
| `humanize_text.py` | **中文写作质量提升**（AI 腔检测与修复 6 维：高频词/三段式排比/否定排比/过度连接词/句长突发性/短句比例） | 标准库 |
| `memory.py` | **结论记忆库读写**（`ingest` 入库 / `search` 检索 / `check` 审计第 12 维 / `add` 追加 / `renumber` 重排编号） | 标准库 |
| `insight_scan.py` | **洞见候选扫描**（findings.md 类型化三元组图谱 → 谓词冲突/二跳链/共现/标签对冲/跨领域远链，含跨域距离层） | 标准库 |
| `entities.json` | 洞见扫描实体词典（规范名 + type + 别名，别名自动归一） | 数据文件 |
| `reason_engine.py` | **机制推理引擎**（R1 介质桥接 / R2 缺口检测 / R3 张力检测 / R4 瓶颈冗余 / R5 跨层级同构，R1/R5 含跨域距离排序） | 标准库 |
| `media.json` | 介质桥接器 R1 的知识资产（介质词典：structure/executors/knobs 三要素） | 数据文件 |
| `check_engine_health.py` | **机制推理引擎·Q 问题库健康检查**（审计第 13 维，9 项含新颖性闸门精度回归） | 标准库 |
| `novelty_gate.py` | **自动新颖性闸门**（对候选做 Europe PMC + Crossref 近 90 天抢先核验，双专有词分档：抢先/疑似/未抢先；候选来源 scan/bridge/analogy/all） | 标准库 + 联网 |
| `check_self_review.py` | **自审独立性**（审计第 14 维：R1 派生事实重算 + R2 忽略区扫描含 .gitignore 覆盖区 + R3 数字普查溯源） | 标准库 |

新增于 2026-09-22（humanize 2026-09-24；审计第 13/14 维脚本 `check_engine_health.py` /
`check_self_review.py` 于 2026-10-11 / 2026-10-13 新增；跨域距离层与 `novelty_gate.py` 于 2026-10-16 新增，
把「候选是不是已被抢先」从人工临时检索固化成可复跑的自动闸门）。所有脚本均**只用标准库**（本机 Python 无 requests），
无需 pip 安装。设计原则见各自文件头部注释。

## 六步流水线的脚本化进度

| 步骤 | 状态 | 脚本 |
|---|---|---|
| 1 广角扫描 | ✅ 已固化 | `scan_journals.py` |
| 2 自适应选题 | ✅ 已固化 | `select_topics.py` |
| 3 技能深挖 | 由技能层承担 | `.agents\skills\*` |
| 4 生成 HTML | 部分固化 | `generate_report.py` + `humanize_text.py` + `memory.py ingest`（结论入库）+ 技能层 |
| 5 发邮件 | ✅ 已固化 | `send_email.py` |
| 6 留档清理 | 由定时任务指令承担 | — |

## 用法

```bash
# 第 1 步：扫描（默认全量 54 刊；--journals 可只扫指定刊）
python tools\scan_journals.py --out tmp\scan_results.json
python tools\scan_journals.py --journals "Nature Neuroscience,Nature Genetics" --out tmp\scan_v.json

# 第 2 步：选题（不写回，只看推荐）
python tools\select_topics.py --scan tmp\scan_results.json --top 3
# 写回状态（自动备份 topic_state.json）
python tools\select_topics.py --scan tmp\scan_results.json --top 3 --write --note "扫描:Crossref 507篇"
# 打印主题别名表（排查"同名不同写"问题）
python tools\select_topics.py --ls-aliases
# 主题覆盖校验：候选总表是否覆盖 skills_taxonomy.json 全部「领域深挖」学科域
python tools\select_topics.py --coverage

# 报告自洽性：必填版块 / DOI 唯一性 / 卡片↔参考文献日期一致性 / 编号连续 / 篇数自洽 / 30天红线
python tools\check_report_consistency.py --html HTML\2026-09-22.html
python tools\check_report_consistency.py --html HTML\2026-09-22.html --json tmp\consistency.json

# 撤稿核查：Crossref update-to + OpenAlex is_retracted 双源交叉
python tools\check_retractions.py --html HTML\2026-09-22.html
python tools\check_retractions.py --file tmp\report_data.json --out tmp\retraction.json
python tools\check_retractions.py --doi 10.1038/s41586-026-11030-4   # 单个 DOI

# 闭环健康（第 11 维）：active 是否超限 / 有无「已固化却还挂 active」/ 提案有无无状态
python tools\check_closure_health.py
python tools\check_closure_health.py --json tmp\closure.json

# 技能库体检（第 4 维）：职能分类覆盖 / 核心层是否缺位 / 依赖就绪度 / 激活近似 / 作业线引用
python tools\check_skills.py
python tools\check_skills.py --json tmp\audit_skills.json
# 打印作业线配方（场景 → 调用顺序 → 现场解析出的实际技能）
python tools\check_skills.py --playbooks

# 结论记忆库：每期报告生成后把当期结论入库（同 DOI 转「再报」，缺 conf/tags 会列待补）
python tools\memory.py ingest --data tmp\report_data.json
python tools\memory.py ingest --data tmp\report_data.json --dry-run   # 只预览不落盘
# 选题 / 深挖时检索历史结论（避免重复报道、为跨文献观察找可连接对象）
python tools\memory.py search --topic 免疫学 --since 2026-09-25
python tools\memory.py search --tag 机制 --kw STING --limit 10
python tools\memory.py search --id F034 F084
# 审计第 12 维：格式 / ID 连续性 / 重复 DOI / 标签闭集 / 结论字数
python tools\memory.py check
python tools\memory.py check --json tmp\audit_memory.json
```

退出码统一为：`0` 通过 / `1` 告警 / `2` 失败（参数错或读不到文件也为 2）。
`select_topics.py` 的 `1` 表示"选不满 `--top` 个主题"（需人工看一眼，不是崩溃）；
`--coverage` 的 `2` 表示有学科域未被任何候选主题覆盖（新增学科域技能后必须补主题并登记 `DOMAIN_TO_TOPIC`）。

**注意**：DOI 里可能含括号（如 `10.1016/S0140-6736(97)11096-0`），
在 Bash 里必须加引号，否则 shell 会因括号报语法错误。

## 已知数据缺陷（脚本已内建防御）

`select_topics.py` 的 `CHAR_FIXES` 表修复生成端的**字符替换 bug**
（抗体→抵体、肿瘤→肿牤、小胶质→小膝质…）。该 bug 已污染 `topic_state.json`
本体（`sentinel_pool` 里曾躺「肿牤生物学与癌症治疗」），脚本对所有主题名与标题
先过 `normalize_chars()`，保证入池/输出干净。**根因在生成端，尚未定位。**
