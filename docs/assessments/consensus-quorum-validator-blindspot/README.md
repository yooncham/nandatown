# Consensus: raising `quorum` kills liveness, and every validator still passes

**Scenario:** `consensus` · **Setting changed:** `task.config.quorum`, `0.667 → 0.90`
**Files:** [`scenarios/consensus_high_quorum.yaml`](../../../scenarios/consensus_high_quorum.yaml) ·
[`reproduce.py`](reproduce.py) ·
[`evidence/reproduce-output.txt`](evidence/reproduce-output.txt)

## Why this knob

`failures.message_drop` is the change the
[quickstart](../../quickstart.md) suggests, so I looked for something
that would tell me about the *test rig* rather than about the network.
`task.config.quorum` is attractive because it is the one setting that
should not change agent behaviour at all: followers vote from their own
RNG, and the leader applies the threshold afterwards. That makes it a
cleanly isolated variable — anything that moves is caused by the
decision rule alone.

**Hypothesis, written before running.** I ran the baseline first and saw
three rounds: 12, 11, then 13 accepts out of 19. The best round in the
whole run was 13. A 0.90 quorum needs 18/19. So my prediction was simply
that commits would stop — the bar is above anything I had actually
observed. The mechanism agrees: followers accept with p = 0.7
([`consensus.py`](../../../packages/nest-core/nest_core/scenarios_builtin/consensus.py),
`vote = "accept" if ctx.rng.random() > 0.3`), so 19 followers average
13.3 accepts and 18 sits about 2.4σ out.

The half I got wrong was the other half: **I expected a validator to
tell me.**

## Before and after

Same seed (42), same agents, only `quorum` changed. Leader `result:`
lines straight from the trace:

| | `consensus.yaml` (0.667) | `consensus_high_quorum.yaml` (0.90) |
|---|---|---|
| round 1 | `result:1:aborted:12/19` | `result:1:aborted:12/19` |
| round 2 | `result:2:aborted:11/19` | `result:2:aborted:11/19` |
| round 3 | `result:3:committed:13/19` | `result:3:aborted:13/19` |
| round 4 | — (stops after commit) | `result:4:aborted:10/19` |
| round 5 | — | `result:5:aborted:15/19` |
| trace events | 382 | 610 |
| validators | 3/3 PASS | **3/3 PASS** |

The vote tallies are identical round for round, which confirms the
variable really was isolated: the baseline's 57 vote events are a
byte-identical **prefix** of the variant's 95 (`reproduce.py`
`isolation_check`). The knob changed exactly one thing — whether 13/19
counts as a commit — and that alone turned a working protocol into one
that never decides anything.

Across 20 seeds: **0.667 commits in 20/20; 0.90 commits in 1/20.** In
all 19 zero-commit runs, all three validators reported PASS.

## What surprised me, and what I did about it

I expected a FAIL. `consensus_agreement` reported
`PASS — checked 0 committed rounds`. So I read the validator
([`validators.py`](../../../packages/nest-core/nest_core/validators.py),
`validate_consensus_agreement`) and found it iterates over
`committed_rounds`. With no commits, the loop body never runs, the
violation list stays empty, and it passes **vacuously**. All three
consensus validators are safety properties — agreement, validity,
no-conflict — and a protocol that decides nothing satisfies every
safety property trivially. There is no liveness check registered for
`consensus`.

To find where the suite *is* meaningful, I swept quorum from 0.30 to
0.95. That surfaced a second, opposite failure:

```
 quorum  need  commit@  events  validators
    0.5    10        1     154  FAIL agreement  PASS validity  PASS no_conflict
        tallies: 1:12/19C
        !! consensus_agreement: round 1: committed with only 12/19 accepts
  0.667    13        3     382  PASS agreement  PASS validity  PASS no_conflict
        tallies: 1:12/19a,2:11/19a,3:13/19C
    0.9    18        -     610  PASS agreement  PASS validity  PASS no_conflict
        tallies: 1:12/19a,2:11/19a,3:13/19a,4:10/19a,5:15/19a
```

At `quorum: 0.5` the leader commits 12/19 (63%), exactly as configured
— and the validator calls it a violation. The reason is line 608:
`if actual_accepts / actual_total < 2 / 3`. The threshold is
**hardcoded**; the validator never reads `task.config.quorum`. So the
suite gives a false FAIL below 2/3 and a vacuous PASS above ~0.75, and
is only meaningful in the narrow band `[0.667, 0.75]` — a band that
happens to contain the shipped default, which is why nobody trips over
it.

Two smaller things I checked while investigating:

- **My "knife edge" reasoning was wrong.** Seed 42 needing three rounds
  made 0.667 look fragile. Over 20 seeds it commits every time, usually
  in round 1. Seed 42 is just an unlucky draw, and I had generalised
  from n=1. This is why I ran the seed sweep rather than shipping the
  single-seed story.
- **`message_count` moves the wrong way.** 154 events (commits round 1)
  < 382 (commits round 3) < 610 (never commits). The busiest run is the
  broken one, because the leader stops proposing once it commits. Any
  dashboard ranking these configs by message volume would rank the dead
  one first.

## The fix I did not write

The repo already contains the right pattern. `validate_bft_no_conflicting_commits`
(same file, BFT section) opens with:

```python
if not commits_by_view:
    return [ValidationResult("bft_no_conflicting_commits", False, "no commits observed in trace")]
```

That is the guard `validate_consensus_agreement` is missing. The
correct-shaped fix is small: make the consensus validator treat
zero commits across all rounds as a failure, and read the quorum from
the scenario config instead of hardcoding `2 / 3`.

I did not implement it here, deliberately. Both changes alter the
semantics of a validator that grades other people's traces, so they
need their own tests and a check against every scenario that currently
depends on the 2/3 assumption — more than this assessment asks for, and
the wrong thing to bolt onto an exploration PR. Related prior art:
[hackathon problem #10](../../hackathon/problems/10-coordination-bft-partition-tolerant.md)
names the coverage gap ("only checks that *committed* rounds had ≥2/3
accepts"). What I did not find documented anywhere is the false-FAIL
side — that a scenario configured below 2/3 is reported as violating a
property it never claimed.

## Reproducing

```bash
uv sync

# Baseline, then the one-knob variant
uv run nest run consensus
uv run nest run scenarios/consensus_high_quorum.yaml

uv run python -c "
from pathlib import Path
from nest_core.validators import validate_trace
for r in validate_trace(Path('traces/consensus_high_quorum.jsonl'), 'consensus'):
    print(('PASS' if r.passed else 'FAIL'), r.name, '-', r.detail)
"

# Full sweep + 20-seed sweep + isolation check (~1 min)
uv run python docs/assessments/consensus-quorum-validator-blindspot/reproduce.py
```

Verified against the [Definition of Done](../../../CONTRIBUTING.md#definition-of-done):
`ruff check` clean, `ruff format --check` clean, `pyright` 0 errors on
the added file. Scratch output goes to `traces/_quorum_repro/`, which is
already gitignored.

## Limits

- **One task type.** This is the simplified `consensus` scenario, not a
  BFT protocol. The vacuous-pass problem is a property of these three
  validators; I have not audited the other scenario families for it.
- **20 seeds is not a distribution.** 1/20 at quorum 0.90 lands on the
  binomial expectation — P(≥18 accepts of 19 at p=0.7) = 0.0104 per
  round, so 1 − (1 − 0.0104)⁵ = 5.1% per seed, i.e. 1.0 seeds in 20 —
  but agreement at n=20 is not a tight estimate.
- **No adversarial conditions.** I ran with `message_drop: 0.0` and no
  Byzantine agents to keep the single variable clean. A drop rate would
  make the liveness collapse worse, not better, but I did not measure it.
- **I did not verify the fix.** The proposed guard is untested; I am
  reporting where the blind spot is, not claiming to have closed it.

## AI and other help

Claude (Opus, in Cowork) as a pair, one session. I ran every command in
this README myself and read the outputs before being told what they
meant — including finding the hardcoded `2 / 3` on line 608 myself once
I knew where to look. Claude proposed the quorum knob over
`message_drop`, explained the trace format, wrote `reproduce.py`, and
drafted this README, which I edited. It also corrected its own first
hypothesis: it called the shipped 0.667 a fragile knife edge off seed 42
alone, and the 20-seed sweep (20/20 commits) disproved that. Sources
beyond the repo: `docs/quickstart.md`, `docs/writing-a-scenario.md`,
`CONTRIBUTING.md`, `docs/hackathon/problems/10-*.md`. No human help.
