# SKILL: Recon Cross-Source (pre-instance platform mapping)

**Identity:** A pre-instance recon module. Given `(country × domain)`, produce a ranked map
of which platforms actually carry signal for that target, so the operator knows where to
deploy personas and anonymous scanners. Run BEFORE scaffolding the instance fleet.

## Trigger

`Recon <country> <domain>` (CLAUDE.md §10), or manually at instance bootstrap.

## Method — triangulate three independent sources (never one)

1. **Audience panel / usage data** — what platforms the target country's population
   actually uses (penetration, MAU, demographics). Use deep-research tools (§8).
2. **Quantitative traffic** — a traffic tool (e.g. SimilarWeb) for panel-blind sizing:
   visits/mo, session length, country rank. Ground-truths the panel data.
3. **In-country domain knowledge** — where *this domain's* conversation actually happens
   (which forums, which livestream apps, which messaging channels). Often differs from
   generic "top platforms".

Cross-verify the three. Contradictions → flag `[DISPUTED]` with both sources (§8).

## Output

A platform priority table the operator can paste into `INSTANCE.md` §4:

| Platform | Why it matters for this domain | Access mode | Priority |
|---|---|---|---|
| … | … | persona / anonymous-web | P0/P1/P2 |

Plus, per platform: does it need in-country SIM/residential IP? (gates persona feasibility,
CLAUDE.md §9 rule 4.)

## Rules

- Use external research tools, not the engine's training data (§8 PROHIBITED list).
- Always anchor temporally (`as of <month/year>`) and request cited URLs.
- Map to the domain's egg rings (yolk/white/shell) so the fleet plan follows scoping.
- Record the recon output under the instance's docs; the strategist revisits it when
  opening new monitoring tracks (§15.W).
