# The Ultimate Form of the Scientific Research Daily

> A daily is not a summary of what's new but a self-correcting loop — cover, select, connect, reason, verify, audit, learn — and that loop, not the topic, is what makes it the ultimate form of any recurring report.

This repository ships that loop in full: a frontier-biology tracking system whose single invariant is — **human judgment is never spent on something a deterministic check can do instead.** Every stage runs through scripts that exit `0` pass / `1` warn / `2` fail, and every recorded mistake is pushed to one question: *can this become a script so the next run needs no human to remember it?*

## What's inside

**`AGENTS.md` + `rules/` — the constitution.** Priority-0 discipline: before touching anything, read the root goal and *ponytail* lazy mode, then self-ask the four criteria — did this remove or add redundancy? root-cause or band-aid? derived from one source, or cloned into a copy that will drift? `rules/全局规则.md` holds the global behaviour rules.

**`tools/` — the deterministic pipeline (Python standard library only).**

- **cover** — `scan_journals.py` scans 54 journals via Crossref.
- **select** — `select_topics.py` picks topics adaptively (normalization, avoidance list, skill-domain coverage).
- **connect** — `insight_scan.py` (typed triples → conflicts, two-hop chains, cross-domain bridges) and `reason_engine.py` (mechanism reasoning: bridge / gap / tension / bottleneck).
- **verify** — `novelty_gate.py` checks "was this already published?" against Europe PMC + Crossref; `generate_report.py` + `humanize_text.py` render the daily.
- **audit** — a family of `check_*.py` that grade report consistency, retractions, closure health, skill coverage, engine health, and self-review independence.
- **learn** — `memory.py` ingests conclusions; `learning/lessons.md` keeps the ≤10 active lessons that change the next run.

**`.agents/skills/` — thirteen skills.** Domain doers (bioinformatics, enrichment, literature lookups) plus the thinking / writing / audit layer. The pivotal `hypothesis-paper-audit/` is a fifteen-dimension adversarial-review framework for manuscripts, with `scripts/check_paper.py`, `scripts/_verify_doi.py` (Crossref DOI resolution), its own `lessons.md` (P001–P018), a run-log template, and a passing `example/` manuscript.

## Quick start

```bash
python tools/scan_journals.py --out tmp/scan_results.json          # cover
python tools/select_topics.py --scan tmp/scan_results.json --top 3  # select

# audit the example manuscript
cd .agents/skills/hypothesis-paper-audit
python scripts/check_paper.py
python scripts/_verify_doi.py
```

Zero third-party dependencies. To enable the email stage, copy `tools/config.example.ini` to `config.ini` and fill in your own SMTP credentials (never commit the real one).

## Scope

Everything here is public — the rules, skills, and pipeline — except the loop's **products and private artifacts**: generated daily reports, manuscripts and submission packages, run logs, and the private memory/findings. Those stay with the author; the loop is the deliverable.

## License

[MIT](LICENSE)