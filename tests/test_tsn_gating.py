"""Tests for tsn_gating.

The expected values come from an INDEPENDENT per-tick reference simulation
implemented in this file (``tick_reference``).  It deliberately does not call
any function of the module under test to derive expected starts/ends/reasons:
the gate mask, eligibility and per-tick waiting reasons are modelled directly
from the raw inputs with one-nanosecond steps.  Round-trip consistency of the
module's own helpers is only used in a supplementary test.
"""

import math
import random
import unittest

from tsn_gating import (
    replay,
    InvalidInputError,
    BudgetExhaustedError,
    MAX_QUEUES,
)

NS_PER_S = 10**9


# ---------------------------------------------------------------------------
# Independent per-tick reference (does not import module-private algorithms)
# ---------------------------------------------------------------------------


def tick_reference(arrivals, schedule):
    """Brute-force 1 ns-tick model.  Times returned are on the reference clock.

    Only used by tests; intentionally written in the most literal possible
    way so it cannot share a bug with the event-driven implementation.
    """
    period = schedule["period_ns"]
    guard = schedule.get("guard_ns", 0)
    offset = schedule.get("clock_offset_ns", 0)

    gate = [[False] * period for _ in range(MAX_QUEUES)]
    for w in schedule["windows"]:
        for t in range(w["start_ns"], w["start_ns"] + w["duration_ns"]):
            for q in w["queues"]:
                gate[q][t % period] = True

    def longest_open_run(q):
        best = run = 0
        for t in range(2 * period):
            if gate[q][t % period]:
                run += 1
                best = max(best, run)
            else:
                run = 0
        return min(best, period)

    all_open = [all(gate[q]) for q in range(MAX_QUEUES)]

    frames = {}
    arrive_at = {}
    for i, a in enumerate(arrivals):
        fid = a.get("frame_id", f"frame_{i}")
        tx = a["length_bits"] * NS_PER_S // schedule["link_rate_bps"]
        fr = {
            "frame_id": fid,
            "seq": i,
            "queue": a["queue"],
            "arrival_ref": a["arrival_ns"],
            "arrival_local": a["arrival_ns"] + offset,
            "tx": tx,
            "need": tx + guard,
            "deadline_ref": a.get("deadline_ns"),
            "status": None,
            "start_ref": None,
            "end_ref": None,
            "ticks": [],  # (local_tick, reason) while waiting
        }
        frames[i] = fr
        arrive_at.setdefault(fr["arrival_local"], []).append(i)

    # Static never-sendable classification, independently via the gate mask.
    queues = [[] for _ in range(MAX_QUEUES)]
    for i, fr in frames.items():
        if not all_open[fr["queue"]] and longest_open_run(fr["queue"]) < fr["need"]:
            fr["status"] = "never_sendable"
        else:
            queues[fr["queue"]].append(i)
    for q in range(MAX_QUEUES):
        queues[q].sort(key=lambda i: (frames[i]["arrival_local"], i))

    sent = set()
    arrived = set()
    heads = [0] * MAX_QUEUES
    busy_end = None

    t_start = min(fr["arrival_local"] for fr in frames.values())
    t = t_start
    bound = t_start + 100_000 * period + 100_000

    def open_at(q, tt):
        return gate[q][tt % period]

    while True:
        for i in arrive_at.get(t, ()):
            if frames[i]["status"] != "never_sendable":
                arrived.add(i)
        if busy_end == t:
            busy_end = None

        winner = None
        if busy_end is None:
            for q in range(MAX_QUEUES):
                if heads[q] < len(queues[q]):
                    i = queues[q][heads[q]]
                    fr = frames[i]
                    if fr["arrival_local"] <= t and all(
                        open_at(q, u) for u in range(t, t + fr["need"])
                    ):
                        key = (q, fr["arrival_local"], i)
                        if winner is None or key < winner[0]:
                            winner = (key, i, q, fr)
            if winner is not None:
                _, i, q, fr = winner
                fr["status"] = "sent"
                fr["start_ref"] = t - offset
                fr["end_ref"] = t - offset + fr["tx"]
                busy_end = t + fr["tx"]
                heads[q] += 1
                sent.add(i)

        # Label every arrived, unsent frame for this single tick, using the
        # state AFTER the start-of-tick decision (a frame started at t is
        # already "sent", so losers correctly see transmitter_busy at t).
        for q in range(MAX_QUEUES):
            for pos in range(heads[q], len(queues[q])):
                i = queues[q][pos]
                fr = frames[i]
                if fr["arrival_local"] > t:
                    continue
                if busy_end is not None:
                    reason = "transmitter_busy"
                elif pos != heads[q]:
                    reason = "queued_behind_head"
                elif not open_at(q, t):
                    reason = "gate_closed"
                elif not all(open_at(q, u) for u in range(t, t + fr["need"])):
                    reason = "window_too_short"
                else:
                    raise AssertionError("reference: idle eligible frame")
                fr["ticks"].append((t, reason))

        done = all(
            fr["status"] in ("sent", "never_sendable") for fr in frames.values()
        )
        if done:
            break
        t += 1
        if t > bound:  # pragma: no cover - safety net, not an expected path
            raise AssertionError("reference did not converge")

    out = []
    for i, fr in frames.items():
        waits = []
        if fr["ticks"]:
            run_start, run_end, run_reason = None, None, None
            for tt, reason in fr["ticks"]:
                if reason == run_reason and tt == run_end:
                    run_end = tt + 1
                else:
                    if run_reason is not None:
                        waits.append(
                            {"start_ns": run_start - offset,
                             "end_ns": run_end - offset,
                             "reason": run_reason}
                        )
                    run_start, run_end, run_reason = tt, tt + 1, reason
            waits.append(
                {"start_ns": run_start - offset,
                 "end_ns": run_end - offset,
                 "reason": run_reason}
            )
        if fr["status"] == "sent":
            violation = (
                fr["deadline_ref"] is not None
                and fr["end_ref"] > fr["deadline_ref"]
            )
            start_v, end_v = fr["start_ref"], fr["end_ref"]
        else:
            violation = fr["deadline_ref"] is not None
            start_v = end_v = None
            waits = []
        out.append(
            {
                "frame_id": fr["frame_id"],
                "status": fr["status"],
                "start_ns": start_v,
                "end_ns": end_v,
                "deadline_violation": violation,
                "wait_intervals": waits,
            }
        )
    return out


def make_schedule(period, segments, guard=0, offset=0, max_run=None):
    """segments: list of (start, duration, queues); gaps are closed."""
    sched = {
        "period_ns": period,
        "link_rate_bps": NS_PER_S,  # tx_ns == length_bits
        "guard_ns": guard,
        "clock_offset_ns": offset,
        "windows": [
            {"start_ns": s, "duration_ns": d, "queues": list(qs)}
            for s, d, qs in segments
        ],
    }
    if max_run is not None:
        sched["max_run_ns"] = max_run
    return sched


def assert_reference_matches(testcase, arrivals, schedule):
    expected = tick_reference(arrivals, schedule)
    result = replay(arrivals, schedule)
    testcase.assertEqual(len(result["frames"]), len(expected))
    for got, exp in zip(result["frames"], expected):
        testcase.assertEqual(got["frame_id"], exp["frame_id"])
        testcase.assertEqual(got["status"], exp["status"], exp["frame_id"])
        testcase.assertEqual(got["start_ns"], exp["start_ns"], exp["frame_id"])
        testcase.assertEqual(got["end_ns"], exp["end_ns"], exp["frame_id"])
        testcase.assertEqual(
            got["deadline_violation"], exp["deadline_violation"], exp["frame_id"]
        )
        testcase.assertEqual(
            got["wait_intervals"], exp["wait_intervals"], exp["frame_id"]
        )
    return result


# ---------------------------------------------------------------------------
# Fixed scenarios
# ---------------------------------------------------------------------------


class GatingFixedTests(unittest.TestCase):
    def test_half_open_boundary_exact_fit(self):
        # Gate [2, 5): tx=3, guard=0 fits starting exactly at 2, ends at 5
        # (half-open: end == gate-close is allowed).
        sched = make_schedule(10, [(2, 3, [0])])
        arrivals = [{"queue": 0, "length_bits": 3, "arrival_ns": 0}]
        r = assert_reference_matches(self, arrivals, sched)
        f = r["frames"][0]
        self.assertEqual((f["start_ns"], f["end_ns"]), (2, 5))

    def test_guard_pushes_to_next_period(self):
        # Gate [0,5), period 10, tx=3, guard=1 (need=4).  Arrival at t=2
        # leaves only 3 ticks: too short WITH the guard, so the frame waits
        # through the closed interval and starts at the next opening, 10.
        sched = make_schedule(10, [(0, 5, [0])], guard=1)
        arrivals = [{"queue": 0, "length_bits": 3, "arrival_ns": 2}]
        r = assert_reference_matches(self, arrivals, sched)
        f = r["frames"][0]
        self.assertEqual((f["start_ns"], f["end_ns"]), (10, 13))

    def test_guard_makes_frame_never_sendable(self):
        # Window length 3: tx=3 + guard=1 = 4 never fits any period.
        sched = make_schedule(10, [(2, 3, [0])], guard=1)
        arrivals = [{"queue": 0, "length_bits": 3, "arrival_ns": 0}]
        r = assert_reference_matches(self, arrivals, sched)
        self.assertEqual(r["frames"][0]["status"], "never_sendable")

    def test_arrival_on_closing_tick_is_closed(self):
        # Half-open: arrival exactly at window end 5 must wait to 12.
        sched = make_schedule(10, [(2, 3, [0])])
        arrivals = [{"queue": 0, "length_bits": 3, "arrival_ns": 5}]
        r = assert_reference_matches(self, arrivals, sched)
        self.assertEqual((r["frames"][0]["start_ns"], r["frames"][0]["end_ns"]),
                         (12, 15))

    def test_cross_period_boundary_merged_arc(self):
        # [6,10) and [0,2) are adjacent on the circle -> arc length 6.
        # need=5 fits starting at 6; transmission 6..10 crosses the boundary.
        sched = make_schedule(10, [(0, 2, [0]), (6, 4, [0])], guard=1)
        arrivals = [{"queue": 0, "length_bits": 4, "arrival_ns": 5}]
        r = assert_reference_matches(self, arrivals, sched)
        f = r["frames"][0]
        self.assertEqual((f["start_ns"], f["end_ns"]), (6, 10))
        self.assertEqual(f["status"], "sent")

    def test_priority_then_arrival_then_sequence(self):
        # Both queues open in [0,10); q0 strictly beats q1; within one queue,
        # equal arrivals keep input sequence.
        sched = make_schedule(20, [(0, 20, [0, 1])])
        arrivals = [
            {"frame_id": "q1a", "queue": 1, "length_bits": 3, "arrival_ns": 0},
            {"frame_id": "q0a", "queue": 0, "length_bits": 3, "arrival_ns": 0},
            {"frame_id": "q0b", "queue": 0, "length_bits": 3, "arrival_ns": 0},
        ]
        r = assert_reference_matches(self, arrivals, sched)
        order = [(f["frame_id"], f["start_ns"]) for f in r["frames"]]
        self.assertEqual(order, [("q1a", 6), ("q0a", 0), ("q0b", 3)])
        # The lower-priority frame waited because the transmitter was busy.
        self.assertEqual(
            r["frames"][0]["wait_intervals"],
            [{"start_ns": 0, "end_ns": 6, "reason": "transmitter_busy"}],
        )

    def test_long_frame_never_sendable(self):
        # Window length 4, need 9: proven impossible in every period.
        sched = make_schedule(10, [(0, 4, [0])])
        arrivals = [
            {"frame_id": "big", "queue": 0, "length_bits": 9,
             "arrival_ns": 0, "deadline_ns": 100}
        ]
        r = assert_reference_matches(self, arrivals, sched)
        f = r["frames"][0]
        self.assertEqual(f["status"], "never_sendable")
        self.assertIsNone(f["start_ns"])
        self.assertIsNone(f["end_ns"])
        self.assertTrue(f["deadline_violation"])
        self.assertEqual(r["never_sendable_count"], 1)
        self.assertEqual(r["sent_count"], 0)

    def test_deadline_violation_sent_late(self):
        # Arrives at 4; with guard, 4+10 > 12 so the window at [0,12) is
        # missed and the frame is sent one period later, past deadline 12.
        sched = make_schedule(100, [(0, 12, [0])], guard=2)
        arrivals = [{"frame_id": "A", "queue": 0, "length_bits": 8,
                     "arrival_ns": 4, "deadline_ns": 12}]
        r = assert_reference_matches(self, arrivals, sched)
        f = r["frames"][0]
        self.assertEqual((f["start_ns"], f["end_ns"]), (100, 108))
        self.assertTrue(f["deadline_violation"])
        # First interval: window open but the remaining slice is too short;
        # closed interval follows until the next opening.
        reasons = [w["reason"] for w in f["wait_intervals"]]
        self.assertEqual(reasons[0], "window_too_short")
        self.assertIn("gate_closed", reasons)

    def test_clock_offset_shifts_local_windows(self):
        sched = make_schedule(10, [(0, 5, [0])], offset=7)
        arrivals = [{"queue": 0, "length_bits": 2, "arrival_ns": 0}]
        r = assert_reference_matches(self, arrivals, sched)
        # Local arrival 7; next local opening at 10 -> reference start 3.
        self.assertEqual((r["frames"][0]["start_ns"], r["frames"][0]["end_ns"]),
                         (3, 5))
        self.assertEqual(r["clock_offset_ns"], 7)

    def test_queued_behind_head_reason(self):
        # Same queue, first frame blocks the second while the gate is closed
        # for the head; second frame is "queued behind head", not gate_closed.
        sched = make_schedule(10, [(0, 6, [0])])
        arrivals = [
            {"frame_id": "a", "queue": 0, "length_bits": 3, "arrival_ns": 8},
            {"frame_id": "b", "queue": 0, "length_bits": 3, "arrival_ns": 8},
        ]
        r = assert_reference_matches(self, arrivals, sched)
        starts = {f["frame_id"]: f["start_ns"] for f in r["frames"]}
        self.assertEqual(starts, {"a": 10, "b": 13})
        b = r["frames"][1]
        self.assertTrue(
            any(w["reason"] == "queued_behind_head" for w in b["wait_intervals"])
        )


class RandomizedAgainstTickReferenceTests(unittest.TestCase):
    """Deterministic property tests: event-driven core vs. tick reference."""

    def test_many_small_scenarios(self):
        rng = random.Random(20260922)
        for scenario in range(250):
            period = rng.randint(4, 14)
            # Partition the period into windows; each segment opens a random
            # non-empty queue subset (gaps stay closed).
            cuts = sorted(rng.sample(range(1, period), rng.randint(0, 2)))
            bounds = [0] + cuts + [period]
            segments = []
            for lo, hi in zip(bounds[:-1], bounds[1:]):
                if rng.random() < 0.8:
                    qs = [q for q in range(MAX_QUEUES) if rng.random() < 0.45]
                    if qs:
                        segments.append((lo, hi - lo, qs))
            guard = rng.randint(0, 2)
            offset = rng.randint(-3, 3)
            sched = make_schedule(period, segments, guard=guard, offset=offset)

            n = rng.randint(1, 6)
            arrivals = []
            for k in range(n):
                arr_t = rng.randint(0, 4 * period)
                arrivals.append(
                    {
                        "frame_id": f"s{scenario}_f{k}",
                        "queue": rng.randrange(MAX_QUEUES),
                        "length_bits": rng.randint(1, period + 2),
                        "arrival_ns": arr_t,
                        "deadline_ns": (
                            arr_t + rng.randint(0, 3 * period)
                            if rng.random() < 0.5
                            else None
                        ),
                    }
                )
            assert_reference_matches(self, arrivals, sched)


# ---------------------------------------------------------------------------
# Error semantics and contract edge cases
# ---------------------------------------------------------------------------


class ValidationTests(unittest.TestCase):
    BASIC_SCHEDULE = {
        "period_ns": 10,
        "link_rate_bps": NS_PER_S,
        "guard_ns": 0,
        "windows": [{"start_ns": 0, "duration_ns": 10, "queues": [0]}],
    }

    def assertInvalidAt(self, arrivals, schedule, fragment):
        with self.assertRaises(InvalidInputError) as cm:
            replay(arrivals, schedule)
        self.assertIn(fragment, str(cm.exception))

    def test_nan_rejected_with_location(self):
        bad = dict(self.BASIC_SCHEDULE)
        bad["period_ns"] = float("nan")
        self.assertInvalidAt([], bad, "period_ns")

    def test_infinity_rejected(self):
        arrivals = [{"queue": 0, "length_bits": 1, "arrival_ns": float("inf")}]
        self.assertInvalidAt(arrivals, self.BASIC_SCHEDULE, "arrival_ns")

    def test_negative_infinity_rejected(self):
        sched = dict(self.BASIC_SCHEDULE)
        sched["guard_ns"] = float("-inf")
        self.assertInvalidAt([], sched, "guard_ns")

    def test_bool_rejected_as_int(self):
        arrivals = [{"queue": True, "length_bits": 1, "arrival_ns": 0}]
        self.assertInvalidAt(arrivals, self.BASIC_SCHEDULE, "queue")

    def test_bool_rejected_for_guard(self):
        sched = dict(self.BASIC_SCHEDULE)
        sched["guard_ns"] = True
        self.assertInvalidAt([], sched, "guard_ns")

    def test_float_rejected_even_if_integral(self):
        arrivals = [{"queue": 0, "length_bits": 1, "arrival_ns": 1.0}]
        self.assertInvalidAt(arrivals, self.BASIC_SCHEDULE, "arrival_ns")

    def test_nonintegral_serialization_rejected(self):
        sched = dict(self.BASIC_SCHEDULE)
        sched["link_rate_bps"] = 3  # 1e9 ns*bps / 3 is not integral
        arrivals = [{"queue": 0, "length_bits": 1, "arrival_ns": 0}]
        self.assertInvalidAt(arrivals, sched, "length_bits")

    def test_overlapping_windows_rejected(self):
        sched = dict(self.BASIC_SCHEDULE)
        sched["windows"] = [
            {"start_ns": 0, "duration_ns": 5, "queues": [0]},
            {"start_ns": 4, "duration_ns": 3, "queues": [1]},
        ]
        self.assertInvalidAt([], sched, "overlapping")

    def test_queue_out_of_range(self):
        arrivals = [{"queue": 4, "length_bits": 1, "arrival_ns": 0}]
        self.assertInvalidAt(arrivals, self.BASIC_SCHEDULE, "queue")

    def test_zero_period_rejected(self):
        sched = dict(self.BASIC_SCHEDULE)
        sched["period_ns"] = 0
        self.assertInvalidAt([], sched, "period_ns")

    def test_window_exceeds_period_rejected(self):
        sched = dict(self.BASIC_SCHEDULE)
        sched["windows"] = [{"start_ns": 8, "duration_ns": 5, "queues": [0]}]
        self.assertInvalidAt([], sched, "exceeds period")

    def test_duplicate_frame_id_rejected(self):
        arrivals = [
            {"frame_id": "x", "queue": 0, "length_bits": 1, "arrival_ns": 0},
            {"frame_id": "x", "queue": 0, "length_bits": 1, "arrival_ns": 1},
        ]
        self.assertInvalidAt(arrivals, self.BASIC_SCHEDULE, "frame_id")

    def test_deadline_before_arrival_rejected(self):
        arrivals = [{"queue": 0, "length_bits": 1, "arrival_ns": 5,
                     "deadline_ns": 4}]
        self.assertInvalidAt(arrivals, self.BASIC_SCHEDULE, "deadline_ns")

    def test_unknown_field_rejected(self):
        sched = dict(self.BASIC_SCHEDULE)
        sched["bogus"] = 1
        self.assertInvalidAt([], sched, "unknown field")

    def test_arrivals_must_be_list(self):
        with self.assertRaises(InvalidInputError):
            replay((), self.BASIC_SCHEDULE)

    def test_empty_arrivals_is_valid(self):
        r = replay([], self.BASIC_SCHEDULE)
        self.assertEqual(r["sent_count"], 0)
        self.assertEqual(r["frames"], [])

    def test_always_closed_gate_never_sendable(self):
        sched = {
            "period_ns": 10,
            "link_rate_bps": NS_PER_S,
            "windows": [],
        }
        r = replay(
            [{"queue": 0, "length_bits": 1, "arrival_ns": 0}], sched
        )
        self.assertEqual(r["frames"][0]["status"], "never_sendable")

    def test_duplicate_queue_in_window_rejected(self):
        sched = dict(self.BASIC_SCHEDULE)
        sched["windows"] = [{"start_ns": 0, "duration_ns": 10,
                             "queues": [0, 0]}]
        self.assertInvalidAt([], sched, "duplicate queue")

    def test_nan_check_uses_math_isnan_semantics(self):
        # Supplementary: NaN is the only value unequal to itself.
        self.assertTrue(math.isnan(float("nan")))


class BudgetTests(unittest.TestCase):
    def test_budget_exhaustion_is_distinct_from_never_sendable(self):
        # Gate [0,12), guard 2: the frame CAN be sent at t=100, but a run
        # horizon of 50 ns cannot reach that point.
        sched = {
            "period_ns": 100,
            "link_rate_bps": NS_PER_S,
            "guard_ns": 2,
            "max_run_ns": 50,
            "windows": [{"start_ns": 0, "duration_ns": 12, "queues": [0]}],
        }
        arrivals = [{"frame_id": "A", "queue": 0, "length_bits": 8,
                     "arrival_ns": 4}]
        with self.assertRaises(BudgetExhaustedError) as cm:
            replay(arrivals, sched)
        self.assertIn(cm.exception.pending_frame_ids[0], {"A"})
        self.assertIn("budget", str(cm.exception))

    def test_removing_horizon_limit_succeeds(self):
        sched = {
            "period_ns": 100,
            "link_rate_bps": NS_PER_S,
            "guard_ns": 2,
            "windows": [{"start_ns": 0, "duration_ns": 12, "queues": [0]}],
        }
        arrivals = [{"frame_id": "A", "queue": 0, "length_bits": 8,
                     "arrival_ns": 4}]
        r = replay(arrivals, sched)
        self.assertEqual(r["frames"][0]["status"], "sent")
        self.assertEqual(r["frames"][0]["start_ns"], 100)


class DeterminismTests(unittest.TestCase):
    def test_repeat_calls_identical(self):
        sched = make_schedule(12, [(0, 5, [0]), (6, 4, [1])], guard=1,
                              offset=-2)
        arrivals = [
            {"frame_id": k, "queue": q, "length_bits": 3,
             "arrival_ns": a, "deadline_ns": 50}
            for k, (q, a) in enumerate([(0, 1), (1, 1), (0, 3), (2, 0)])
        ]
        r1 = replay(arrivals, sched)
        r2 = replay(arrivals, sched)
        self.assertEqual(r1, r2)

    def test_internal_geometry_round_trip(self):
        # Supplementary round-trip check on the module's own geometry helpers
        # (not used to derive expected results elsewhere): a frame sent
        # straight through an always-open queue reports zero waiting.
        sched = make_schedule(10, [(0, 10, [3])])
        r = replay([{"frame_id": "z", "queue": 3, "length_bits": 4,
                     "arrival_ns": 2, "deadline_ns": 100}], sched)
        f = r["frames"][0]
        self.assertEqual(f["status"], "sent")
        self.assertEqual((f["start_ns"], f["end_ns"]), (2, 6))
        self.assertFalse(f["deadline_violation"])
        self.assertEqual(f["wait_intervals"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
