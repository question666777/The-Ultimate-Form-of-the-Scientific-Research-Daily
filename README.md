# The Ultimate Form of the Scientific Research Daily

> A daily is not a summary of what's new but a self-correcting loop — cover, select, connect, reason, verify, audit, learn — and that loop, not the topic, is what makes it the ultimate form of any recurring report.

This repository ships the fully runnable core of that loop: the **audit-and-self-evolution engine** that turns "will this survive review?" into reproducible, deterministic checks — and turns every recorded mistake into a lesson permanently installed for the next run.

## What's inside

- `SKILL.md` — the fifteen-dimension audit framework, from original increment and conceptual depth to format compliance, citation integrity, and metadata hygiene, plus an orthogonal *adversarial-review* dimension that attacks the draft the way a hostile reviewer would.
- `scripts/check_paper.py` — deterministic checks (format / citations / consistency / figures / submission package), exit-code gated: `0` pass, `1` warn, `2` fail.
- `scripts/_verify_doi.py` — resolves every reference DOI against Crossref.
- `lessons.md` — the experience library (P001–P018), where each mistake is pushed to one question: *can this become a script so the next run needs no human to remember it?*
- `example/` — a completed hypothesis manuscript (EN + CN) that passes every automated dimension.

## Quick start

```bash
python scripts/check_paper.py        # audit the example manuscript
python scripts/_verify_doi.py        # resolve its references against Crossref
python scripts/check_paper.py --json report.json
```

Zero third-party dependencies — Python standard library only.

## Scope

The loop is released incrementally. This repository ships its **audit → learn** stage today; the remaining stages (cover, select, connect, reason, verify) follow under the same invariant: *human judgment is never spent on something a deterministic check can do instead.*

## License

[MIT](LICENSE)