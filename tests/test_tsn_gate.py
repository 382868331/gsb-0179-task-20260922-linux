"""tsn_gate 单元测试:定点用例 + 与独立逐 tick 参考的确定性随机对照。"""

import random
import unittest

from tsn_gate import (
    BudgetExhaustedError,
    Frame,
    FrameStatus,
    GateSchedule,
    GateWindow,
    TSNGateError,
    WaitReason,
    replay,
)
from reference import reference_replay


def sched(windows, cycle=100, rate=8, guard=0, offset=0):
    # rate=8 bit/ns => 1 字节恰好 1ns,便于手算期望值
    return GateSchedule(
        cycle_ns=cycle,
        windows=tuple(windows),
        link_rate_bits_per_ns=rate,
        guard_ns=guard,
        clock_offset_ns=offset,
    )


class TestBasic(unittest.TestCase):
    def test_single_frame_sends_immediately(self):
        s = sched([GateWindow(0, 50, {0})])
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=10)], s)
        f = r.frames[0]
        self.assertEqual(f.status, FrameStatus.SENT)
        self.assertEqual((f.start_ns, f.end_ns), (0, 10))  # 10B * 8bit / 8bit/ns
        self.assertEqual(f.waited_ns, 0)
        self.assertEqual(f.wait_reasons, ())
        self.assertFalse(f.deadline_violated)
        self.assertEqual(r.sent_count, 1)
        self.assertEqual(r.never_count, 0)

    def test_serialization_ceil(self):
        # rate=3 bit/ns, 1 字节 = 8 bit -> ceil(8/3) = 3 ns
        s = sched([GateWindow(0, 50, {0})], rate=3)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], s)
        self.assertEqual(r.frames[0].duration_ns, 3)
        self.assertEqual(r.frames[0].end_ns, 3)

    def test_waits_for_window_open(self):
        s = sched([GateWindow(50, 60, {0})])
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], s)
        f = r.frames[0]
        self.assertEqual((f.start_ns, f.end_ns), (50, 51))
        self.assertIn(WaitReason.GATE_CLOSED, f.wait_reasons)

    def test_arrive_before_time_zero_of_window_half_open(self):
        # 到达时刻恰为窗口终点:半开区间,门已关
        s = sched([GateWindow(0, 10, {0})])
        r = replay([Frame(queue=0, arrive_ns=10, size_bytes=1)], s)
        self.assertEqual(r.frames[0].start_ns, 100)  # 下一周期窗口起点


class TestCrossCycleBoundary(unittest.TestCase):
    def test_wait_across_cycle_boundary(self):
        s = sched([GateWindow(0, 10, {0})])
        r = replay([Frame(queue=0, arrive_ns=95, size_bytes=5)], s)
        f = r.frames[0]
        self.assertEqual((f.start_ns, f.end_ns), (100, 105))
        self.assertEqual(f.waited_ns, 5)

    def test_window_spanning_whole_cycle(self):
        s = sched([GateWindow(0, 100, {0})])
        r = replay([Frame(queue=0, arrive_ns=95, size_bytes=5)], s)
        self.assertEqual(r.frames[0].start_ns, 95)


class TestGuard(unittest.TestCase):
    def test_guard_exact_fit_allowed(self):
        # 窗口 10ns,时长 8 + guard 2 = 10,恰好顶到窗口终点:允许
        s = sched([GateWindow(0, 10, {0})], guard=2)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=8)], s)
        self.assertEqual(r.frames[0].status, FrameStatus.SENT)
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (0, 8))

    def test_guard_one_over_blocks_start(self):
        # 时长 8 + guard 3 = 11 > 剩余 10:本窗口不可开始,等下一周期也没用 -> 永不可发
        s = sched([GateWindow(0, 10, {0})], guard=3)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=8)], s)
        self.assertEqual(r.frames[0].status, FrameStatus.NEVER_TRANSMITTABLE)

    def test_guard_blocks_late_start_within_window(self):
        # 窗口 [0,10),guard 2,时长 5:8 时刻到达,剩余 2 < 7,须等下一周期
        s = sched([GateWindow(0, 10, {0})], guard=2)
        r = replay([Frame(queue=0, arrive_ns=8, size_bytes=5)], s)
        f = r.frames[0]
        self.assertEqual(f.start_ns, 100)
        self.assertIn(WaitReason.INSUFFICIENT_WINDOW, f.wait_reasons)


class TestIdenticalArrivals(unittest.TestCase):
    def test_priority_then_seq(self):
        # 同时到达:队列号小者优先;同队列按到达序号
        s = sched([GateWindow(0, 100, {0, 1})])
        arrivals = [
            Frame(queue=1, arrive_ns=0, size_bytes=1),  # seq0
            Frame(queue=0, arrive_ns=0, size_bytes=1),  # seq1
            Frame(queue=0, arrive_ns=0, size_bytes=1),  # seq2
        ]
        r = replay(arrivals, s)
        starts = {f.seq: f.start_ns for f in r.frames}
        self.assertEqual(starts[1], 0)  # 队列 0,序号小
        self.assertEqual(starts[2], 1)  # 队列 0,序号大
        self.assertEqual(starts[0], 2)  # 队列 1 最后
        self.assertIn(WaitReason.OUTRANKED, r.frames[2].wait_reasons)
        self.assertIn(WaitReason.LINK_BUSY, r.frames[0].wait_reasons)


class TestNeverTransmittable(unittest.TestCase):
    def test_long_frame_never(self):
        s = sched([GateWindow(0, 10, {0})])
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=11)], s)  # 11ns > 10ns
        f = r.frames[0]
        self.assertEqual(f.status, FrameStatus.NEVER_TRANSMITTABLE)
        self.assertIsNone(f.start_ns)
        self.assertIsNone(f.end_ns)
        self.assertEqual(r.never_count, 1)

    def test_no_window_for_queue_never(self):
        s = sched([GateWindow(0, 10, {0})])
        r = replay([Frame(queue=1, arrive_ns=0, size_bytes=1)], s)
        self.assertEqual(r.frames[0].status, FrameStatus.NEVER_TRANSMITTABLE)

    def test_never_does_not_block_others(self):
        s = sched([GateWindow(0, 10, {0})])
        arrivals = [
            Frame(queue=0, arrive_ns=0, size_bytes=11),  # 永不可发
            Frame(queue=0, arrive_ns=0, size_bytes=1),   # 正常发送
        ]
        r = replay(arrivals, s)
        self.assertEqual(r.frames[0].status, FrameStatus.NEVER_TRANSMITTABLE)
        self.assertEqual(r.frames[1].status, FrameStatus.SENT)
        self.assertEqual(r.frames[1].start_ns, 0)


class TestClockOffset(unittest.TestCase):
    def test_positive_offset_shifts_window_earlier_in_reference(self):
        # 本地 = 参考 + 5;本地窗口 [0,10) 对应参考 [95,100) ∪ [195,200) ...
        s = sched([GateWindow(0, 10, {0})], offset=5)
        r = replay([Frame(queue=0, arrive_ns=95, size_bytes=1)], s)
        self.assertEqual(r.frames[0].start_ns, 95)

    def test_negative_offset(self):
        # 本地 = 参考 - 5;本地窗口 [0,10) 对应参考 [5,15)
        s = sched([GateWindow(0, 10, {0})], offset=-5)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], s)
        self.assertEqual(r.frames[0].start_ns, 5)

    def test_offset_zero_baseline(self):
        s = sched([GateWindow(0, 10, {0})], offset=0)
        r = replay([Frame(queue=0, arrive_ns=95, size_bytes=1)], s)
        self.assertEqual(r.frames[0].start_ns, 100)


class TestDeadline(unittest.TestCase):
    def test_deadline_violation_recorded_but_frame_sent(self):
        s = sched([GateWindow(0, 50, {0})])
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=8, deadline_ns=5)], s)
        f = r.frames[0]
        self.assertEqual(f.end_ns, 8)
        self.assertTrue(f.deadline_violated)
        self.assertEqual(r.deadline_violations, (0,))

    def test_deadline_met(self):
        s = sched([GateWindow(0, 50, {0})])
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=1, deadline_ns=8)], s)
        self.assertFalse(r.frames[0].deadline_violated)
        self.assertEqual(r.deadline_violations, ())


class TestInvalidInput(unittest.TestCase):
    def good_schedule(self):
        return sched([GateWindow(0, 10, {0})])

    def test_bool_queue_rejected(self):
        with self.assertRaisesRegex(TSNGateError, r"arrivals\[0\]\.queue"):
            replay([Frame(queue=True, arrive_ns=0, size_bytes=1)], self.good_schedule())

    def test_nan_arrive_rejected(self):
        with self.assertRaisesRegex(TSNGateError, r"arrivals\[0\]\.arrive_ns.*NaN"):
            replay([Frame(queue=0, arrive_ns=float("nan"), size_bytes=1)],
                   self.good_schedule())

    def test_inf_deadline_rejected(self):
        with self.assertRaisesRegex(TSNGateError, r"deadline_ns"):
            replay([Frame(queue=0, arrive_ns=0, size_bytes=1,
                          deadline_ns=float("inf"))], self.good_schedule())

    def test_float_time_rejected_even_if_integral(self):
        with self.assertRaisesRegex(TSNGateError, r"arrivals\[0\]\.arrive_ns"):
            replay([Frame(queue=0, arrive_ns=3.0, size_bytes=1)], self.good_schedule())

    def test_queue_out_of_range(self):
        with self.assertRaisesRegex(TSNGateError, r"arrivals\[0\]\.queue"):
            replay([Frame(queue=4, arrive_ns=0, size_bytes=1)], self.good_schedule())

    def test_negative_arrive(self):
        with self.assertRaisesRegex(TSNGateError, r"arrive_ns"):
            replay([Frame(queue=0, arrive_ns=-1, size_bytes=1)], self.good_schedule())

    def test_zero_size(self):
        with self.assertRaisesRegex(TSNGateError, r"size_bytes"):
            replay([Frame(queue=0, arrive_ns=0, size_bytes=0)], self.good_schedule())

    def test_bad_schedule_types(self):
        with self.assertRaisesRegex(TSNGateError, "cycle_ns"):
            GateSchedule(cycle_ns=0, windows=(), link_rate_bits_per_ns=1)
        with self.assertRaisesRegex(TSNGateError, "link_rate_bits_per_ns"):
            GateSchedule(cycle_ns=10, windows=(), link_rate_bits_per_ns=0)
        with self.assertRaisesRegex(TSNGateError, "guard_ns"):
            GateSchedule(cycle_ns=10, windows=(), link_rate_bits_per_ns=1, guard_ns=-1)
        with self.assertRaisesRegex(TSNGateError, "clock_offset_ns"):
            GateSchedule(cycle_ns=10, windows=(), link_rate_bits_per_ns=1,
                         clock_offset_ns=True)

    def test_bad_window(self):
        with self.assertRaisesRegex(TSNGateError, "GateWindow"):
            GateWindow(10, 10, {0})  # 空区间
        with self.assertRaisesRegex(TSNGateError, "queues"):
            GateWindow(0, 10, set())  # 空队列集
        with self.assertRaisesRegex(TSNGateError, "queues"):
            GateWindow(0, 10, {4})  # 队列越界

    def test_window_beyond_cycle(self):
        with self.assertRaisesRegex(TSNGateError, r"windows\[0\]"):
            GateSchedule(cycle_ns=10, windows=(GateWindow(0, 11, {0}),),
                         link_rate_bits_per_ns=1)

    def test_overlapping_windows_same_queue(self):
        with self.assertRaisesRegex(TSNGateError, "重叠"):
            GateSchedule(
                cycle_ns=100,
                windows=(GateWindow(0, 30, {0}), GateWindow(20, 40, {0})),
                link_rate_bits_per_ns=1,
            )

    def test_adjacent_windows_same_queue_ok(self):
        GateSchedule(
            cycle_ns=100,
            windows=(GateWindow(0, 30, {0}), GateWindow(30, 40, {0})),
            link_rate_bits_per_ns=1,
        )

    def test_not_a_schedule(self):
        with self.assertRaisesRegex(TSNGateError, "gate_schedule"):
            replay([], object())

    def test_failure_leaves_no_partial_result(self):
        # 批次中任一帧非法 -> 整体抛错,无部分输出
        arrivals = [Frame(queue=0, arrive_ns=0, size_bytes=1),
                    Frame(queue=0, arrive_ns=0, size_bytes=-1)]
        with self.assertRaises(TSNGateError):
            replay(arrivals, self.good_schedule())


class TestBudget(unittest.TestCase):
    def test_budget_exhausted_is_distinct_from_never(self):
        s = sched([GateWindow(10, 20, {0})])
        with self.assertRaises(BudgetExhaustedError):
            replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], s, max_events=1)
        # 同一输入在充足预算下可正常完成,证明预算耗尽 ≠ 永不可发
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], s)
        self.assertEqual(r.frames[0].status, FrameStatus.SENT)


class TestAgainstTickReference(unittest.TestCase):
    """确定性随机小整数用例:事件驱动核心 vs 独立逐 tick 参考。"""

    def test_fuzz_small_integer_cases(self):
        rng = random.Random(20260922)
        for case in range(300):
            cycle = rng.randint(10, 60)
            nwin = rng.randint(1, 3)
            windows = []
            for _ in range(nwin):
                start = rng.randint(0, cycle - 2)
                end = rng.randint(start + 1, cycle)
                qs = frozenset(rng.sample(range(3), rng.randint(1, 3)))
                windows.append((start, end, qs))
            # 去掉同队列重叠的窗口组合(核心会拒绝,参考未定义该行为)
            ok = True
            for q in range(3):
                segs = sorted((s, e) for (s, e, qs) in windows if q in qs)
                for (s1, e1), (s2, _e2) in zip(segs, segs[1:]):
                    if s2 < e1:
                        ok = False
            if not ok:
                continue
            rate = rng.randint(1, 4)
            guard = rng.randint(0, 5)
            offset = rng.randint(-10, 10)
            nframes = rng.randint(1, 8)
            frames = []
            for _ in range(nframes):
                frames.append((
                    rng.randint(0, 2),
                    rng.randint(0, 3 * cycle),
                    rng.randint(1, 6),
                    None,
                ))
            schedule = GateSchedule(
                cycle_ns=cycle,
                windows=tuple(GateWindow(s, e, qs) for (s, e, qs) in windows),
                link_rate_bits_per_ns=rate,
                guard_ns=guard,
                clock_offset_ns=offset,
            )
            arrivals = [Frame(queue=q, arrive_ns=a, size_bytes=z, deadline_ns=d)
                        for (q, a, z, d) in frames]
            expected = reference_replay(frames, windows, cycle, rate, guard, offset)
            result = replay(arrivals, schedule)
            for i, (exp, got) in enumerate(zip(expected, result.frames)):
                if exp == (None, None):
                    self.assertEqual(
                        got.status, FrameStatus.NEVER_TRANSMITTABLE,
                        f"case {case} frame {i}: 参考判定永不可发,核心却发送",
                    )
                else:
                    self.assertEqual(
                        (got.start_ns, got.end_ns), exp,
                        f"case {case} frame {i}: 核心与逐 tick 参考不一致",
                    )


if __name__ == "__main__":
    unittest.main()
