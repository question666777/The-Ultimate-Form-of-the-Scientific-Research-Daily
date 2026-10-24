# 示例稿件

这是 `hypothesis-paper-audit` 的示例输入——一篇真实完成的假说论文，中英双稿：

- `manuscript_EN.md` — 英文投稿稿（Medical Hypotheses 六段结构，摘要 ≤250 词 / 正文 ≤3000 词 / 关键词 6 / 文献 36 / 图 1）
- `manuscript_CN.md` — 同步的中文底稿（关键术语与英文稿对齐）

## 怎么跑

```bash
# 在仓库根目录执行
python scripts\check_paper.py          # 审计硬约束 + 启发式线索
python scripts\_verify_doi.py          # 逐条核验 36 条参考文献 DOI（联网）
```

预期结果：`check_paper.py` 退出码 `0`，`_verify_doi.py` 输出 `OK 36 / FAIL 0`。

> 说明：投稿包（docx 五件套）不含在本示例中（二进制且含文档元数据），因此 `check_paper.py` 的 D13「投稿包合规」在示例下显示为「未观察」，不参与评分——这正是脚本对「无投稿包时」的诚实处理。