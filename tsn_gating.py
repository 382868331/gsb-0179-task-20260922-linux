"""TSN time-aware gating transmission replay (deterministic, integer-nanosecond).

This module implements a small, self-contained subset of time-sensitive
networking gate-control verification (in the spirit of IEEE 802.1Qbv time-
aware scheduling, without claiming standard compliance):

* Up to 4 queues; queue index 0 is the highest priority.
* A periodic gate schedule repeats every ``period_ns``.  Gate windows are
  half-open intervals ``[start_ns, start_ns + duration_ns)`` on the device's
  local clock and list which queues may transmit.
* Frames are non-preemptible.  A frame may START at local time ``t`` only
  when the whole serialization plus the guard margin fits one open window:
  ``t + tx_ns + guard_ns <= window_end`` (equality is allowed).
* Windows of the same queue that are adjacent across the period boundary
  form one cyclic open arc, so a transmission may cross the boundary.
* A fixed clock offset (local clock minus reference clock) is part of the
  input: ``local_time = reference_time + clock_offset_ns``.

All time arithmetic is exact integer arithmetic; no floats are used.  See
README.md for the full contract, input caps, design choices and limitations.
"""

from __future__ import annotations

__all__ = [
    "replay",
    "TSNGatingError",
    "InvalidInputError",
    "BudgetExhaustedError",
    "REASON_GATE_CLOSED",
    "REASON_WINDOW_TOO_SHORT",
    "REASON_TRANSMITTER_BUSY",
    "REASON_QUEUED_BEHIND_HEAD",
]

# ---------------------------------------------------------------------------
# Public limits (interface boundary; see README.md)
# ---------------------------------------------------------------------------

MAX_QUEUES = 4
MAX_ARRIVALS = 1000
MAX_PERIOD_NS = 1_000_000
MAX_GUARD_NS = 1_000_000
MAX_WINDOWS = 64
MAX_LENGTH_BITS = 10**9
MAX_RATE_BPS = 10**12
MAX_ARRIVAL_NS = 10**12
MAX_DEADLINE_NS = 10**12
MAX_OFFSET_NS = 10**9
MAX_RUN_NS = 10_000_000
EVENT_CAP = 1_000_000

_NS_PER_S = 10**9

# Wait reason vocabulary (part of the public contract).
REASON_GATE_CLOSED = "gate_closed"
REASON_WINDOW_TOO_SHORT = "window_too_short"
REASON_TRANSMITTER_BUSY = "transmitter_busy"
REASON_QUEUED_BEHIND_HEAD = "queued_behind_head"


class TSNGatingError(Exception):
    """Base class for all errors raised by this module."""


class InvalidInputError(TSNGatingError, ValueError):
    """An input value is invalid; the message identifies the offending field.

    Also a ``ValueError`` so generic value validation catches it.
    """


class BudgetExhaustedError(TSNGatingError):
    """The simulation search budget was exhausted with frames unresolved.

    This is NOT a proof that those frames can never be sent (contrast with
    ``status == "never_sendable"``); it only means the configured run horizon
    or internal event budget did not suffice.  ``pending_frame_ids`` lists
    every frame that had not been resolved.
    """

    def __init__(self, message, *, cutoff_ns, pending_frame_ids):
        super().__init__(message)
        self.cutoff_ns = cutoff_ns
        self.pending_frame_ids = list(pending_frame_ids)


# ---------------------------------------------------------------------------
# Primitive validation
# ---------------------------------------------------------------------------


def _strict_int(value, path, *, minimum=None, maximum=None):
    """Exact-int requirement.  bool is rejected; NaN/Infinity get a clear error."""
    if isinstance(value, bool):
        raise InvalidInputError(f"{path}: must be int, got bool")
    if isinstance(value, int):
        ivalue = value
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise InvalidInputError(
                f"{path}: must be a finite integer, got non-finite float"
            )
        raise InvalidInputError(
            f"{path}: must be int (exact integer nanoseconds), got float {value!r}"
        )
    else:
        raise InvalidInputError(
            f"{path}: must be int, got {type(value).__name__}"
        )
    if minimum is not None and ivalue < minimum:
        raise InvalidInputError(f"{path}: value {ivalue} is below minimum {minimum}")
    if maximum is not None and ivalue > maximum:
        raise InvalidInputError(f"{path}: value {ivalue} exceeds maximum {maximum}")
    return ivalue


def _require_mapping(value, path):
    if not isinstance(value, dict):
        raise InvalidInputError(
            f"{path}: must be an object/mapping, got {type(value).__name__}"
        )
    return value


# ---------------------------------------------------------------------------
# Schedule validation and cyclic arc construction
# ---------------------------------------------------------------------------


def _build_arcs(windows, period):
    """Per-queue maximal cyclic open arcs ``(start_mod_period, length)``.

    Adjacent raw windows of one queue (including adjacency across the period
    boundary) are merged into one arc, so a start before the boundary may
    finish after it.  Arcs are disjoint and lengths are in 1..period.
    """
    linear = [[] for _ in range(MAX_QUEUES)]
    for w in windows:
        s, e = w["start_ns"], w["end_ns"]
        for q in w["queues"]:
            linear[q].append((s, e))

    arcs = [[] for _ in range(MAX_QUEUES)]
    for q in range(MAX_QUEUES):
        merged = []
        for s, e in sorted(linear[q]):
            if merged and s <= merged[-1][1]:
                if e > merged[-1][1]:
                    merged[-1][1] = e
            else:
                merged.append([s, e])

        # Cyclic merge: [0, b) and [a, period) -> one arc starting at a.
        if len(merged) >= 2 and merged[0][0] == 0 and merged[-1][1] == period:
            head = merged.pop(0)
            tail = merged.pop()
            start = tail[0]
            length = (period - start) + head[1]
            merged.append([start, start + length])

        result = sorted((s % period, e - s) for s, e in merged)
        arcs[q] = result
    return arcs


def _validate_schedule(schedule):
    _require_mapping(schedule, "gate_schedule")

    known = {"period_ns", "link_rate_bps", "guard_ns", "windows",
             "clock_offset_ns", "max_run_ns"}
    unknown = set(schedule) - known
    if unknown:
        raise InvalidInputError(
            f"gate_schedule: unknown field(s) {sorted(unknown)}"
        )

    period = _strict_int(
        schedule.get("period_ns"), "gate_schedule.period_ns",
        minimum=1, maximum=MAX_PERIOD_NS,
    )
    rate = _strict_int(
        schedule.get("link_rate_bps"), "gate_schedule.link_rate_bps",
        minimum=1, maximum=MAX_RATE_BPS,
    )
    guard = _strict_int(
        schedule.get("guard_ns", 0), "gate_schedule.guard_ns",
        minimum=0, maximum=MAX_GUARD_NS,
    )
    offset = _strict_int(
        schedule.get("clock_offset_ns", 0), "gate_schedule.clock_offset_ns",
        minimum=-MAX_OFFSET_NS, maximum=MAX_OFFSET_NS,
    )
    max_run = schedule.get("max_run_ns")
    if max_run is not None:
        max_run = _strict_int(
            max_run, "gate_schedule.max_run_ns",
            minimum=1, maximum=MAX_RUN_NS,
        )

    raw_windows = schedule.get("windows")
    if not isinstance(raw_windows, list):
        raise InvalidInputError("gate_schedule.windows: must be a list")
    if len(raw_windows) > MAX_WINDOWS:
        raise InvalidInputError(
            f"gate_schedule.windows: at most {MAX_WINDOWS} entries, "
            f"got {len(raw_windows)}"
        )

    windows = []
    for i, w in enumerate(raw_windows):
        path = f"gate_schedule.windows[{i}]"
        _require_mapping(w, path)
        bad = set(w) - {"start_ns", "duration_ns", "queues"}
        if bad:
            raise InvalidInputError(f"{path}: unknown field(s) {sorted(bad)}")
        s = _strict_int(
            w.get("start_ns"), f"{path}.start_ns",
            minimum=0, maximum=period - 1,
        )
        d = _strict_int(
            w.get("duration_ns"), f"{path}.duration_ns",
            minimum=1, maximum=period,
        )
        if s + d > period:
            raise InvalidInputError(
                f"{path}: window [{s}, {s + d}) exceeds period {period}"
            )
        qs = w.get("queues")
        if not isinstance(qs, list) or not qs:
            raise InvalidInputError(f"{path}.queues: must be a non-empty list")
        norm_q = []
        for j, q in enumerate(qs):
            qv = _strict_int(
                q, f"{path}.queues[{j}]",
                minimum=0, maximum=MAX_QUEUES - 1,
            )
            if qv in norm_q:
                raise InvalidInputError(
                    f"{path}.queues: duplicate queue {qv}"
                )
            norm_q.append(qv)
        windows.append(
            {"start_ns": s, "duration_ns": d, "end_ns": s + d, "queues": norm_q}
        )

    windows.sort(key=lambda w: (w["start_ns"], w["end_ns"]))
    for i in range(1, len(windows)):
        prev, cur = windows[i - 1], windows[i]
        if cur["start_ns"] < prev["end_ns"]:
            raise InvalidInputError(
                "gate_schedule.windows: overlapping windows "
                f"[{prev['start_ns']}, {prev['end_ns']}) and "
                f"[{cur['start_ns']}, {cur['end_ns']})"
            )

    return {
        "period_ns": period,
        "link_rate_bps": rate,
        "guard_ns": guard,
        "clock_offset_ns": offset,
        "max_run_ns": max_run,
        "windows": windows,
        "arcs": _build_arcs(windows, period),
    }


# ---------------------------------------------------------------------------
# Frame validation
# ---------------------------------------------------------------------------


def _validate_arrivals(arrivals, sched):
    if not isinstance(arrivals, list):
        raise InvalidInputError(
            f"arrivals: must be a list, got {type(arrivals).__name__}"
        )
    if len(arrivals) > MAX_ARRIVALS:
        raise InvalidInputError(
            f"arrivals: at most {MAX_ARRIVALS} frames, got {len(arrivals)}"
        )

    rate = sched["link_rate_bps"]
    offset = sched["clock_offset_ns"]
    frames = []
    seen_ids = set()
    for i, arr in enumerate(arrivals):
        path = f"arrivals[{i}]"
        _require_mapping(arr, path)
        bad = set(arr) - {
            "frame_id", "queue", "length_bits", "arrival_ns", "deadline_ns"
        }
        if bad:
            raise InvalidInputError(f"{path}: unknown field(s) {sorted(bad)}")

        fid = arr.get("frame_id", f"frame_{i}")
        if isinstance(fid, bool) or not isinstance(fid, (str, int)):
            raise InvalidInputError(f"{path}.frame_id: must be str or int")
        if fid in seen_ids:
            raise InvalidInputError(f"{path}.frame_id: duplicate frame_id {fid!r}")
        seen_ids.add(fid)

        q = _strict_int(arr.get("queue"), f"{path}.queue",
                        minimum=0, maximum=MAX_QUEUES - 1)
        length = _strict_int(
            arr.get("length_bits"), f"{path}.length_bits",
            minimum=1, maximum=MAX_LENGTH_BITS,
        )
        arrival_ref = _strict_int(
            arr.get("arrival_ns"), f"{path}.arrival_ns",
            minimum=0, maximum=MAX_ARRIVAL_NS,
        )

        numerator = length * _NS_PER_S
        if numerator % rate != 0:
            raise InvalidInputError(
                f"{path}.length_bits: serialization time must be an integer "
                f"number of nanoseconds; {length} bits at {rate} bps is not "
                "integral"
            )
        tx = numerator // rate

        deadline_ref = arr.get("deadline_ns")
        if deadline_ref is not None:
            deadline_ref = _strict_int(
                deadline_ref, f"{path}.deadline_ns",
                minimum=0, maximum=MAX_DEADLINE_NS,
            )
            if deadline_ref < arrival_ref:
                raise InvalidInputError(
                    f"{path}.deadline_ns: deadline {deadline_ref} precedes "
                    f"arrival {arrival_ref}"
                )

        frames.append(
            {
                "frame_id": fid,
                "seq": i,
                "queue": q,
                "length_bits": length,
                "tx_ns": tx,
                "arrival_ref": arrival_ref,
                "arrival_local": arrival_ref + offset,
                "deadline_ref": deadline_ref,
                "status": None,
                "start_local": None,
                "end_local": None,
                "wait_intervals": [],  # [start_local, end_local, reason]
                "_reason": None,
                "_reason_start": None,
            }
        )
    return frames


# ---------------------------------------------------------------------------
# Cyclic gate geometry
# ---------------------------------------------------------------------------

# Sentinel for an always-open queue: no closed tick exists anywhere on the
# cyclic timeline, so "window end" is unbounded.
_INF = float("inf")


def _arc_end_at(arcs, period, q, t):
    """Absolute end of the open arc instance containing t, or None if closed.

    Half-open: t exactly at an arc end is closed.  An arc that fills the
    whole period (length == period) means the gate is cyclically always
    open: adjacent instances are contiguous with no closed tick, so the end
    is treated as unbounded (``_INF``) — a start may cross any instance
    boundary.
    """
    m = t % period
    for theta, length in arcs[q]:
        if length == period:
            return _INF
        rel = (m - theta) % period
        if rel < length:
            return t + (length - rel)
    return None


def _next_gate_open(arcs, period, q, t):
    """Smallest time strictly greater than t at which queue q's gate opens.

    None when the gate never opens again (always closed or always open).
    """
    best = None
    m = t % period
    for theta, length in arcs[q]:
        if length == period:
            # Always open: there is no closed->open transition.
            continue
        delta = (theta - m) % period
        nxt = t + delta
        if nxt <= t:
            nxt += period
        if best is None or nxt < best:
            best = nxt
    return best


def _next_send_time(arcs, period, q, ready, need):
    """Earliest s >= ready with (s + need) inside one open arc instance.

    Returns None when no cyclic arc of the queue is long enough: a static
    proof that a frame requiring ``need`` ns can never be sent.  A full-
    period arc (always open) holds any length, so s == ready.
    """
    best = None
    for theta, length in arcs[q]:
        if length == period:
            return ready
        if length < need:
            continue
        base = theta + ((ready - theta) // period) * period
        if max(ready, base) <= base + length - need:
            candidate = max(ready, base)
        else:
            candidate = base + period  # length >= need guarantees the fit
        if best is None or candidate < best:
            best = candidate
    return best


# ---------------------------------------------------------------------------
# Waiting-reason bookkeeping
# ---------------------------------------------------------------------------


def _set_reason(frame, reason, t):
    if frame["_reason"] == reason:
        return
    if frame["_reason"] is not None:
        start = frame["_reason_start"]
        if t > start:
            frame["wait_intervals"].append([start, t, frame["_reason"]])
    frame["_reason"] = reason
    frame["_reason_start"] = t


def _close_reason(frame, t):
    if frame["_reason"] is not None:
        start = frame["_reason_start"]
        if t > start:
            frame["wait_intervals"].append([start, t, frame["_reason"]])
        frame["_reason"] = None
        frame["_reason_start"] = None


def _assign_reasons(queues, heads, by_seq, arcs, period, guard, t, busy):
    """Label every arrived, unsent frame with its waiting reason at time t."""
    for q in range(MAX_QUEUES):
        head = heads[q]
        for pos in range(head, len(queues[q])):
            f = by_seq[queues[q][pos]]
            if f["arrival_local"] > t or f["status"] is not None:
                continue
            if busy:
                _set_reason(f, REASON_TRANSMITTER_BUSY, t)
            elif pos != head:
                _set_reason(f, REASON_QUEUED_BEHIND_HEAD, t)
            else:
                end = _arc_end_at(arcs, period, q, t)
                if end is None:
                    _set_reason(f, REASON_GATE_CLOSED, t)
                elif end - t < f["tx_ns"] + guard:
                    _set_reason(f, REASON_WINDOW_TOO_SHORT, t)
                else:
                    # An eligible head on an idle tick must start (selection
                    # runs before reason assignment).
                    raise RuntimeError("internal error: eligible frame left idle")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def replay(arrivals, gate_schedule):
    """Replay frame arrivals against a periodic gate schedule.

    See the module docstring and README.md for the full contract.

    Validation runs before any result is produced, so malformed input raises
    :class:`InvalidInputError` with a field-path in the message and never
    returns partial output.  :class:`BudgetExhaustedError` means the search
    budget ran out; it is distinct from ``status == "never_sendable"``,
    which is a proof that no gate window of the schedule can ever hold the
    frame.
    """
    sched = _validate_schedule(_require_mapping(gate_schedule, "gate_schedule"))
    frames = _validate_arrivals(arrivals, sched)

    period = sched["period_ns"]
    guard = sched["guard_ns"]
    arcs = sched["arcs"]

    # Static proof: the frame fits none of the queue's cyclic open arcs.
    sendable = []
    for f in frames:
        need = f["tx_ns"] + guard
        if _next_send_time(arcs, period, f["queue"], f["arrival_local"], need) is None:
            f["status"] = "never_sendable"
        else:
            sendable.append(f)

    if not sendable:
        return _build_result(frames, sched)

    return _simulate(frames, sendable, sched)


def _simulate(frames, sendable, sched):
    period = sched["period_ns"]
    guard = sched["guard_ns"]
    arcs = sched["arcs"]
    offset = sched["clock_offset_ns"]
    max_run = sched["max_run_ns"]

    by_seq = {f["seq"]: f for f in frames}
    queues = [[] for _ in range(MAX_QUEUES)]  # frame seq per queue, FIFO
    for f in sorted(sendable, key=lambda f: (f["arrival_local"], f["seq"])):
        queues[f["queue"]].append(f["seq"])
    heads = [0] * MAX_QUEUES

    arrivals_sorted = sorted(sendable, key=lambda f: (f["arrival_local"], f["seq"]))
    arrival_ptr = 0
    first_t = arrivals_sorted[0]["arrival_local"]
    last_t = arrivals_sorted[-1]["arrival_local"]
    if max_run is None:
        # After the last arrival each of the n frames waits at most about
        # one period for its turn; the factor 2 is a safety margin.
        cutoff = last_t + 2 * (len(sendable) + 2) * period
    else:
        cutoff = first_t + max_run

    def pending_at(t):
        ids = []
        for q in range(MAX_QUEUES):
            for pos in range(heads[q], len(queues[q])):
                f = by_seq[queues[q][pos]]
                if f["arrival_local"] <= t and f["status"] is None:
                    ids.append(f["frame_id"])
        return ids

    def raise_budget(t):
        pending = pending_at(t)
        for f in arrivals_sorted[arrival_ptr:]:
            pending.append(f["frame_id"])
        raise BudgetExhaustedError(
            f"search budget exhausted at local t={t} (reference {t - offset}): "
            f"{len(pending)} unresolved frame(s); this is budget exhaustion, "
            "not a never-sendable proof",
            cutoff_ns=t - offset,
            pending_frame_ids=pending,
        )

    t = first_t
    busy_until = None
    events = 0
    sim_end_local = t

    while True:
        events += 1
        if events > EVENT_CAP:
            raise BudgetExhaustedError(
                f"internal event cap {EVENT_CAP} exhausted at local t={t}",
                cutoff_ns=t - offset,
                pending_frame_ids=pending_at(t),
            )

        # Arrivals at t become available; a transmission ending at t frees
        # the transmitter at exactly t (half-open serialization interval).
        while (
            arrival_ptr < len(arrivals_sorted)
            and arrivals_sorted[arrival_ptr]["arrival_local"] <= t
        ):
            arrival_ptr += 1
        if busy_until == t:
            busy_until = None

        winner = None
        if busy_until is None:
            winner_key = None
            for q in range(MAX_QUEUES):
                if heads[q] >= len(queues[q]):
                    continue
                f = by_seq[queues[q][heads[q]]]
                if f["arrival_local"] > t:
                    continue
                need = f["tx_ns"] + guard
                if _next_send_time(arcs, period, q, t, need) == t:
                    key = (q, f["arrival_local"], f["seq"])
                    if winner_key is None or key < winner_key:
                        winner, winner_key = f, key

            if winner is not None:
                end = t + winner["tx_ns"]
                winner["status"] = "sent"
                winner["start_local"] = t
                winner["end_local"] = end
                _close_reason(winner, t)
                heads[winner["queue"]] += 1
                busy_until = end
                sim_end_local = max(sim_end_local, end)

        busy = busy_until is not None
        _assign_reasons(queues, heads, by_seq, arcs, period, guard, t, busy)

        # Next state change: arrival, transmission end, or head gate change.
        nxt = None
        if arrival_ptr < len(arrivals_sorted):
            nxt = arrivals_sorted[arrival_ptr]["arrival_local"]
        if busy_until is not None:
            nxt = busy_until if nxt is None else min(nxt, busy_until)
        else:
            for q in range(MAX_QUEUES):
                if heads[q] >= len(queues[q]):
                    continue
                f = by_seq[queues[q][heads[q]]]
                if f["arrival_local"] > t:
                    continue
                need = f["tx_ns"] + guard
                s = _next_send_time(arcs, period, q, t, need)
                if s is not None and (nxt is None or s < nxt):
                    nxt = s
                end = _arc_end_at(arcs, period, q, t)
                if end is not None:
                    cand = end  # open now: reason changes when it closes
                else:
                    cand = _next_gate_open(arcs, period, q, t)
                if cand is not None and (nxt is None or cand < nxt):
                    nxt = cand

        future = arrival_ptr < len(arrivals_sorted)
        if nxt is None:
            if not pending_at(t) and not future:
                break
            raise_budget(t)
        if nxt >= cutoff and (pending_at(t) or future):
            raise_budget(t)
        t = nxt

    return _build_result(frames, sched, sim_end_local)


def _build_result(frames, sched, sim_end_local=None):
    offset = sched["clock_offset_ns"]
    out_frames = []
    sent = never = violations = 0
    ref_ends = []
    for f in frames:
        status = f["status"]
        if status == "sent":
            sent += 1
            start_ref = f["start_local"] - offset
            end_ref = f["end_local"] - offset
            ref_ends.append(end_ref)
            violation = (
                f["deadline_ref"] is not None and end_ref > f["deadline_ref"]
            )
            waits = [
                {"start_ns": a - offset, "end_ns": b - offset, "reason": r}
                for a, b, r in f["wait_intervals"]
            ]
            start_v, end_v = start_ref, end_ref
        elif status == "never_sendable":
            never += 1
            # It is proven the frame can never be serialized, so any deadline
            # can never be met.
            violation = f["deadline_ref"] is not None
            waits = []
            start_v = end_v = None
        else:  # pragma: no cover - unresolved frames never reach the result
            raise RuntimeError("internal error: unresolved frame in result")
        if violation:
            violations += 1
        out_frames.append(
            {
                "frame_id": f["frame_id"],
                "queue": f["queue"],
                "arrival_ns": f["arrival_ref"],
                "length_bits": f["length_bits"],
                "tx_duration_ns": f["tx_ns"],
                "status": status,
                "start_ns": start_v,
                "end_ns": end_v,
                "deadline_ns": f["deadline_ref"],
                "deadline_violation": violation,
                "wait_intervals": waits,
            }
        )
        ref_ends.append(f["arrival_ref"])

    if sim_end_local is None:
        until = max(ref_ends) if ref_ends else 0
    else:
        until = max(sim_end_local - offset, max(ref_ends, default=0))
    return {
        "frames": out_frames,
        "sent_count": sent,
        "never_sendable_count": never,
        "deadline_violation_count": violations,
        "simulated_until_ns": until,
        "clock_offset_ns": offset,
    }
