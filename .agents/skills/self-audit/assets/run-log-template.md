# 运行复盘 YYYY-MM-DD

> 由 self-audit 技能生成。一天一份，追加不覆盖。

## 基本信息
- 运行日期：YYYY-MM-DD
- 今日主题：主题A / 主题B / 主题C
- 报告：HTML\YYYY-MM-DD.html（存在/缺失）
- 邮件：已发送 / 未发送（原因）

## 内向敌对自审（每次审计的固定前段，人工）
- 向量 C 矛盾扫描（宣称的原则有没有互斥对）：候选 N 项 / 无（附一句「为什么认为无」）
- 向量 D 缺席审查（该有 gate 的环节有没有）：候选 N 项 / 无（附一句「为什么认为无」）
- 处置：自改 N 项 / 提案 N 份
- 空转提醒：连续无发现期数 = N（≥3 须换角度，否则本机制退化成仪式）

## 十六维审计
| 维度 | 结果 | 证据 |
|---|---|---|
| 1. 时效合规 | 通过/警告/失败/未观察 | |
| 2. 数据真实 | | |
| 3. 选题质量 | | （`select_topics.py` 退出码＝0/1/2） |
| 4. 技能库体检与使用（自动） | 通过/警告/失败 | check_skills.py 退出码＝？；核心层缺位 / 未归类 / 作业线引用失效明细 + 本期各主题实调用技能 |
| 5. 报告完整 | | |
| 6. 口径一致 | | |
| 7. 流程效率 | | |
| 8. 邮件送达 | | |
| 9. 报告自洽性（自动） | 通过/警告/失败 | check_report_consistency.py 退出码＝？；失败明细抄原文 |
| 10. 撤稿状态（自动） | 通过/警告/失败 | check_retractions.py 退出码＝？；retracted/corrected 条目逐条列出 |
| 11. 闭环健康（自动） | 通过/警告/失败 | check_closure_health.py 退出码＝？；active 条数 / 假 fixed / 无状态提案 / 滞留 |
| 12. 记忆库（自动） | 通过/警告/失败 | memory.py check 退出码＝？；结论条数 / 重复 DOI / 待补标签 |
| 13. 机制推理引擎（自动） | 通过/警告/失败 | check_engine_health.py 退出码＝？；R1 回归 / Q 条数 / D2+ 占比 / 候选落库数 |
| 14. 自审独立性（自动） | 通过/警告/失败 | check_self_review.py 退出码＝？；R1 漂移明细 + R2 残留明细 + R3 孤儿勾选结果逐条抄 |
| 15. 论文线阈值闸门 | 通过/警告/失败 | 默认挂起：本期有无「无候选却自行开审计」；有 [硬] ≥2.5 候选未触发 D0 则告警 |
| 16. 检索能力健康（自动） | 通过/警告/失败 | check_retrieval.py 退出码＝？；期刊数 / 实体数 / 别名覆盖 / 脚本在位+import / 声明一致 |

## 脚本校验记录
- `check_skills.py` 退出码：0/1/2；核心层缺位 / 未归类 / 作业线引用失效明细：
- `check_report_consistency.py` 退出码：0/1/2；失败项：
- `check_retractions.py` 退出码：0/1/2；异常条目：
- `check_closure_health.py` 退出码：0/1/2；明细：
- `memory.py check` 退出码：0/1/2；明细：
- `check_engine_health.py` 退出码：0/1/2；明细：
- `check_self_review.py` 退出码：0/1/2；R1 漂移明细 / R2 残留明细 / R3 孤儿勾选：
- `check_retrieval.py` 退出码：0/1/2；期刊数 / 实体数 / 别名覆盖 / 脚本在位+import / 声明一致：
- `scan_journals.py` 是否选用（若用：扫描刊数/条目数/DOI 反查 mismatch 数）：
- `select_topics.py` 退出码：0/1/2；推荐主题与人工最终选题是否一致：

## 闭环健康明细（第 11 维）
- active 经验：N 条（**上限以 lessons.md 头部声明为准**，本文件不复制数字）
- 本轮从 active 转 fixed：L0xx（原因：已固化为 tools/xxx.py）
- 提案处理：已采纳 N 份 / 已拒绝 N 份 / 待办 N 份 / **无状态 N 份（应为 0）**

## 异常与处置
- （重试、降级、兜底、自改记录；无则写「无」）

## 经验提炼
- 新增：L0xx …（已写入 lessons.md）/ 无
- 状态变更：L0xx → fixed/retired / 无

## 改进提案
- proposals\YYYY-MM-DD.md（N 份）/ 无
