# MODULE: Insight Scoring (domain-aware value rubric)

**Trigger:** L4 batch (Stage 2 structured precision sets `kb_value_score`; Stage 3 escalates
the top slice). This rubric defines what "valuable" means so scoring is consistent.

## Scoring axes (0–100 `kb_value_score`)

| Axis | Question | Weight (tune per instance) |
|---|---|---|
| Commercial relevance | Does this change a client commercial decision (§1)? | high |
| Novelty | New vs already-known / already-in-library? | high |
| Corroboration | Seen on ≥2 platforms / ≥2 sources? | medium |
| Actionability | Can the client *do* something with it this week? | high |
| Freshness | How time-sensitive is it (decay, §ref kb/DECAY_POLICY.md)? | medium |
| Source reliability | Persona ground-truth vs anonymous scrape vs rumor? | medium |

## Value classes (`kb_value_class`)

- `decision` — directly informs a commercial move → candidate for Stage 3 + a decision card.
- `context` — useful background; admit to library, low priority.
- `noise` — kept only for noise-labeling; excluded from the commercial KPI count (§1.1).

## Per-instance overrides

Each instance may override weights + add domain-specific signal classes in its policy.
The north star (§1) is the tie-breaker: when unsure, score *commercial-advantage signal*
above generic coverage.

## Gate to Stage 3

`kb_value_score ≥ 70` AND `kb_value_class = decision` → escalate to the strategic model for
cross-case pattern + commercial-action framing.
