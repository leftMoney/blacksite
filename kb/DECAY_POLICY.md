# MODULE: KB Decay / Pruning Policy

**Trigger:** L1 cron (daily). One of the three "thin layers" Blacksite must own (CLAUDE.md
§2): intel value decays; the library must prune stale entries and keep the index honest.

## Principle

An intelligence card is only worth its storage + retrieval cost if it still informs a
decision. Stale promo bursts, expired invite codes, and one-off chatter decay fast;
structural facts (operator family-trees, durable competitor positioning) decay slowly.

## Per-class half-life (tune per instance)

| Entry class | Default half-life | Notes |
|---|---|---|
| Ephemeral promo / deal-drop | 7 days | promo codes, one-off bursts |
| Funnel / channel activity | 30 days | re-confirm on re-observation |
| Entity profile (operator/brand/KOL) | 180 days | refreshed by new corroboration |
| Structural / strategic fact | 365 days+ | rarely pruned |

Each card carries offset-aware `observed_at` / `event_at` / `valid_from` / `valid_to`
(§6.4). Decay scores against `valid_to` and last-corroboration time.

## Pruning flow (couples L1 cron with L5 index)

1. Compute a decay score per card/chunk/entity from class half-life + last corroboration.
2. Below threshold → mark `stale`; below hard floor → delete from the KB **and** the vector
   index (Qdrant) in the same pass — never orphan an index entry whose source was pruned.
3. Re-observation resets the clock (corroboration_count +1 lifts the score back up).
4. Log prunes to `system_history` (kind=`config_change`/`metric`) for auditability.

## Rule

Never prune a card still linked as evidence to an open lead or an un-shipped decision card.
Pruning is reversible-by-re-collection, but evidence integrity for live decisions comes
first.
