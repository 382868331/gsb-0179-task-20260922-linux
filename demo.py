#!/usr/bin/env python3
"""Fixed-input demonstration for tsn_gating.

Run with:  python demo.py

Prints one normal replay result (including a deadline violation caused by a
gating miss) and one genuinely triggered error boundary.  All inputs are
fixed literals in this file; nothing is read from disk or network, and no
result is pre-recorded.
"""

import time
import traceback

from tsn_gating import (
    replay,
    InvalidInputError,
    BudgetExhaustedError,
    REASON_GATE_CLOSED,
    REASON_WINDOW_TOO_SHORT,
    REASON_TRANSMITTER_BUSY,
    REASON_QUEUED_BEHIND_HEAD,
)

NS_PER_S = 10**9

REASON_TEXT = {
    REASON_GATE_CLOSED: "gate closed",
    REASON_WINDOW_TOO_SHORT: "remaining window too short (tx + guard)",
    REASON_TRANSMITTER_BUSY: "transmitter busy (non-preemptible)",
    REASON_QUEUED_BEHIND_HEAD: "queued behind same-queue head frame",
}


def print_frame(f):
    if f["status"] == "sent":
        window = f"start={f['start_ns']:>4} ns  end={f['end_ns']:>4} ns"
        viol = "  DEADLINE VIOLATION" if f["deadline_violation"] else ""
    else:
        window = "never sendable (proven: no cyclic gate window fits)"
        viol = "  deadline unmeetable" if f["deadline_violation"] else ""
    print(
        f"  {f['frame_id']:<6} q{f['queue']} "
        f"arrival={f['arrival_ns']:>3} ns tx={f['tx_duration_ns']:>2} ns "
        f"deadline={f['deadline_ns']}  -> {f['status']:<14} {window}{viol}"
    )
    for w in f["wait_intervals"]:
        print(
            f"           waited [{w['start_ns']:>4},{w['end_ns']:>4}) ns : "
            f"{REASON_TEXT[w['reason']]}"
        )


def main():
    print("=" * 78)
    print("TSN time-aware gating replay demo (all times in integer nanoseconds)")
    print("=" * 78)

    # ---- Scenario 1: normal result, including a gating-caused miss --------
    # Period 20 ns.  Gate [0,8) opens queue 0; [10,6) (i.e. [10,16)) opens
    # queue 1.  Link: 1 bit/ns (1e9 bps).  Guard = 1 ns.  Local device clock
    # runs 5 ns ahead of the reference clock.
    schedule = {
        "period_ns": 20,
        "link_rate_bps": NS_PER_S,
        "guard_ns": 1,
        "clock_offset_ns": 5,
        "windows": [
            {"start_ns": 0, "duration_ns": 8, "queues": [0]},
            {"start_ns": 10, "duration_ns": 6, "queues": [1]},
        ],
    }
    arrivals = [
        # q0 frame arrives at local t=5 (reference 0): window [0,8) has room
        # for tx=2 + guard=1 exactly (5+3 == 8), so it starts immediately.
        {"frame_id": "A", "queue": 0, "length_bits": 2,
         "arrival_ns": 0, "deadline_ns": 4},
        # q1 frame present from reference t=0; its gate is closed until
        # local 10 (reference 5), then it fits with 1 ns margin.
        {"frame_id": "B", "queue": 1, "length_bits": 4,
         "arrival_ns": 0, "deadline_ns": 20},
        # q0 frame arrives at local t=11 (reference 6): [0,8) is already
        # closed; the next opening is local t=20 (reference 15).  It is sent
        # at reference 15..20, missing deadline 14 because of gating.
        {"frame_id": "C", "queue": 0, "length_bits": 4,
         "arrival_ns": 6, "deadline_ns": 14},
        # Too long for any q1 opening (6 < 4 + 1 + ... ): tx=8 plus guard
        # can never fit a 6 ns cyclic arc -> proven never sendable.
        {"frame_id": "D", "queue": 1, "length_bits": 8,
         "arrival_ns": 0, "deadline_ns": 100},
    ]

    print("\nScenario 1: periodic gates, guard=1 ns, clock offset +5 ns")
    print(
        "  schedule: q0 open [0,8) local, q1 open [10,16) local, period 20 ns"
    )
    t0 = time.perf_counter()
    result = replay(arrivals, schedule)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    for f in result["frames"]:
        print_frame(f)
    print(
        f"  summary: sent={result['sent_count']} "
        f"never_sendable={result['never_sendable_count']} "
        f"deadline_violations={result['deadline_violation_count']} "
        f"(computed in {elapsed_ms:.3f} ms, exact integer arithmetic)"
    )
    # Assert the demo scenario really exhibits the advertised behavior.
    by_id = {f["frame_id"]: f for f in result["frames"]}
    assert by_id["C"]["deadline_violation"] is True, "demo expected a miss"
    assert by_id["C"]["start_ns"] == 15
    assert by_id["D"]["status"] == "never_sendable"

    # ---- Scenario 2: an actually triggered error boundary ---------------
    print("\n" + "-" * 78)
    print("Boundary 2a: non-finite input is rejected with a locatable error")
    bad_schedule = dict(schedule)
    bad_schedule["guard_ns"] = float("nan")
    try:
        replay(arrivals, bad_schedule)
    except InvalidInputError as exc:
        print(f"  replay(...) raised InvalidInputError:\n    {exc}")
    else:  # pragma: no cover - demo invariant
        raise AssertionError("NaN guard must be rejected")

    print("\nBoundary 2b: search-budget exhaustion is distinct from "
          "'never sendable'")
    tight_schedule = dict(schedule)
    tight_schedule["max_run_ns"] = 5  # only 5 ns of simulated run
    late = [
        {"frame_id": "L", "queue": 0, "length_bits": 4,
         "arrival_ns": 6, "deadline_ns": 50}
    ]
    try:
        replay(late, tight_schedule)
    except BudgetExhaustedError as exc:
        print(f"  replay(...) raised BudgetExhaustedError:\n    {exc}")
        print(f"  pending frames: {exc.pending_frame_ids}")
    else:  # pragma: no cover - demo invariant
        raise AssertionError("tight horizon must raise BudgetExhaustedError")

    print("\nDemo finished.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
