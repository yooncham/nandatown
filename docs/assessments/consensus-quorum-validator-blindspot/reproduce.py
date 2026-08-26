# SPDX-License-Identifier: Apache-2.0
"""Reproduce the consensus quorum experiment and its validator blind spot.

Takes ``scenarios/consensus.yaml`` and changes only ``task.config.quorum``
(and ``seed``, for the second sweep), then reports what the leader decided
and what the three registered ``consensus`` validators said about it.

Sweep 1 maps where the validator suite is meaningful at all. Two things
show up:

1. At quorum >= 0.8 nothing commits, and every validator still reports
   PASS -- they are safety-only, so a protocol that never decides
   satisfies them vacuously. There is no liveness check registered.
2. At quorum < 2/3 the leader commits exactly as configured, and
   ``consensus_agreement`` reports FAIL, because it hardcodes ``2 / 3``
   instead of reading ``task.config.quorum``.

Sweep 2 repeats the shipped 0.667 and the raised 0.90 over 20 seeds, to
avoid generalising from the single seed the scenario ships with.

Run from the repository root::

    uv run python docs/assessments/consensus-quorum-validator-blindspot/reproduce.py

Example::

    exit_code = main()
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

from nest_core.validators import validate_trace

BASE = Path("scenarios/consensus.yaml")
OUT = Path("traces/sweep")  # under traces/, which is already gitignored
FOLLOWERS = 19
SEEDS = range(1, 21)
QUORUM_SWEEP = (0.30, 0.50, 0.60, 0.667, 0.70, 0.75, 0.80, 0.90, 0.95)


def run(quorum: float, seed: int) -> Path:
    """Build a one-knob variant of the stock scenario, run it, return the trace.

    Example::

        trace = run(0.9, 42)
    """
    OUT.mkdir(parents=True, exist_ok=True)
    trace = OUT / f"q{quorum}_s{seed}.jsonl"
    scenario = OUT / f"q{quorum}_s{seed}.yaml"
    scenario.write_text(
        BASE.read_text()
        .replace("quorum: 0.667", f"quorum: {quorum}")
        .replace("seed: 42", f"seed: {seed}")
        .replace("./traces/consensus.jsonl", trace.as_posix())
    )
    subprocess.run(
        [sys.executable, "-m", "nest_core.cli", "run", scenario.as_posix()],
        capture_output=True,
        check=True,
    )
    return trace


def _sends(trace: Path) -> list[tuple[str, str]]:
    """Return ``(agent, payload)`` for every send event in a trace.

    Example::

        events = _sends(Path("trace.jsonl"))
    """
    out: list[tuple[str, str]] = []
    for line in trace.read_text().splitlines():
        if not line.strip():
            continue
        event: dict[str, object] = json.loads(line)
        if event.get("kind") != "send":
            continue
        payload = str(event.get("msg", "")).rsplit("|sig:", 1)[0]
        out.append((str(event.get("agent", "")), payload))
    return out


def round_results(trace: Path) -> dict[int, tuple[str, str]]:
    """Map round number to ``(outcome, tally)`` from the leader's result lines.

    Example::

        results = round_results(Path("trace.jsonl"))
    """
    results: dict[int, tuple[str, str]] = {}
    for _agent, payload in _sends(trace):
        if not payload.startswith("result:"):
            continue
        part = payload.split(":")
        if len(part) >= 4:
            results[int(part[1])] = (part[2], part[3])
    return results


def vote_sequence(trace: Path) -> list[tuple[str, str]]:
    """Return the ordered follower vote events, for cross-run comparison.

    Example::

        votes = vote_sequence(Path("trace.jsonl"))
    """
    return [(a, m) for a, m in _sends(trace) if m.startswith("vote:")]


def verdicts(trace: Path) -> str:
    """Return a one-line PASS/FAIL summary of the consensus validators.

    Example::

        line = verdicts(Path("trace.jsonl"))
    """
    parts: list[str] = []
    for result in validate_trace(trace, "consensus"):
        flag = "PASS" if result.passed else "FAIL"
        parts.append(f"{flag} {result.name.removeprefix('consensus_')}")
    return "  ".join(parts)


def quorum_sweep() -> None:
    """Sweep quorum at the shipped seed and print validator verdicts.

    Example::

        quorum_sweep()
    """
    print("=== Sweep 1: quorum 0.30 -> 0.95, seed 42 (only quorum changes) ===")
    print(f"{'quorum':>7} {'need':>5} {'commit@':>8} {'events':>7}  validators")
    for quorum in QUORUM_SWEEP:
        trace = run(quorum, 42)
        results = round_results(trace)
        committed = [r for r, (outcome, _) in results.items() if outcome == "committed"]
        events = len([ln for ln in trace.read_text().splitlines() if ln.strip()])
        tallies = ",".join(
            f"{r}:{tally}{'C' if outcome == 'committed' else 'a'}"
            for r, (outcome, tally) in sorted(results.items())
        )
        print(
            f"{quorum:>7} {math.ceil(quorum * FOLLOWERS):>5} "
            f"{(committed[0] if committed else '-'):>8} {events:>7}  {verdicts(trace)}"
        )
        print(f"{'':>8}tallies: {tallies}")
        for result in validate_trace(trace, "consensus"):
            if not result.passed:
                print(f"{'':>8}!! {result.name}: {result.detail}")


def isolation_check() -> None:
    """Assert the changed knob does not perturb follower voting behaviour.

    Example::

        isolation_check()
    """
    baseline = vote_sequence(run(0.667, 42))
    raised = vote_sequence(run(0.90, 42))
    matches = raised[: len(baseline)] == baseline
    print(
        f"\n=== Variable isolation ===\n"
        f"baseline votes: {len(baseline)}   high-quorum votes: {len(raised)}\n"
        f"baseline vote sequence is a byte-identical prefix of high-quorum: {matches}"
    )
    if not matches:
        raise AssertionError("quorum changed follower behaviour; the variable is not isolated")


def seed_sweep() -> None:
    """Compare commit rates at the shipped 0.667 versus the raised 0.90.

    Example::

        seed_sweep()
    """
    print(f"\n=== Sweep 2: {len(SEEDS)} seeds each ===")
    for quorum in (0.667, 0.90):
        committed = 0
        vacuous = 0
        for seed in SEEDS:
            trace = run(quorum, seed)
            hit = any(outcome == "committed" for outcome, _ in round_results(trace).values())
            committed += int(hit)
            if not hit and all(r.passed for r in validate_trace(trace, "consensus")):
                vacuous += 1
        print(
            f"quorum={quorum}: committed in {committed}/{len(SEEDS)} seeds; "
            f"{vacuous} zero-commit runs where all validators still PASS"
        )


def main() -> int:
    """Run both sweeps and the isolation check; return a process exit code.

    Example::

        exit_code = main()
    """
    quorum_sweep()
    isolation_check()
    seed_sweep()
    print(f"\nScratch scenarios and traces are in {OUT.as_posix()}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
