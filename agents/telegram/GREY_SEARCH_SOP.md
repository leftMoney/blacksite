# SOP: Telegram Grey Search

**Trigger:** Telegram agent dispatch (search/join). TG is Blacksite's primary surface for
grey-market intel — expect to search and join many channels (deal funnels, operator
broadcast channels, community chatter).

## Elevated-risk posture (CLAUDE.md §11)

- **Read-only by default.** No posting, reacting, DMing, or provoking inside grey channels.
- **Exhaustive logging.** Every join/search/read is logged with timestamp + intent +
  platform response (§9 rule 5).
- **Treat state-adjacent venues as elevated-risk.** If a channel may be operated by a
  state-adjacent or law-enforcement-adjacent entity, prefer pure observation; escalate to
  the strategist before any infiltration of suspected operator-run closed groups (§14).

## Search flow

1. Seed from `agents/telegram/join_plan.yaml` + the instance's policy search seeds (in the
   target market's language).
2. Search → classify candidate channels (`tg_classifier.py`) → join with stagger/jitter
   (`tg_join.py`, default 90–180s, "gentle but not glacial", §14).
3. Listen + emit raw JSONL to `runtime/raw/<agent_id>/` (`tg_listen.py`).
4. Mine cross-channel patterns (`tg_pattern_miner.py`): shared promo codes, shared domains,
   co-occurring senders → operator family-tree hypotheses for the Section Chief.

## Hard lines

- No automated transactions on any grey site (register-only if a persona must; never
  deposit/withdraw — §9 rule 2, §11).
- No identity fraud. Personas are synthetic.
- Permanent-failure targets (dead/invite-expired/kicked) → terminal state, never retried
  (§14).
