"""tsn_gate 单元测试:定点用例 + 与独立逐 tick 参考的确定性随机对照。"""

import random
import unittest

from tsn_gate import (
    BudgetExhaustedError,
    Frame,
    FrameStatus,
    GateSchedule,
    GateWindow,
    ScheduledUpdate,
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


def _ceil_div(a, b):
    return -((-a) // b)


def audit_no_overlap_and_gating(result, arrivals, old_s, eff=None, new_s=None):
    """独立后置审计(不调用核心调度逻辑):区间不重叠、不提前、门控几何成立。

    纯几何核对每个 SENT 帧:start>=arrive、end=start+duration、
    同链路区间两两不重叠(半开相邻允许)、起始时刻所在生效表确实开门且
    时长+guard 完整落入窗口;epoch0 的发送还必须在 eff(含 guard)前完成。
    NEVER 帧在新表(有切换时)或旧表(无切换时)必须几何上确实永不可发。
    """
    def geom(schedule, queue_ns, t, need):
        phase = (t + schedule.clock_offset_ns) % schedule.cycle_ns
        return any(q in w.queues and w.start_ns <= phase < w.end_ns
                   and w.end_ns - phase >= need
                   for w in schedule.windows for q in [queue_ns])

    def maxwin(schedule, q):
        return max((w.end_ns - w.start_ns for w in schedule.windows
                    if q in w.queues), default=0)

    sent = [f for f in result.frames if f.status is FrameStatus.SENT]
    intervals = sorted((f.start_ns, f.end_ns, f.seq) for f in sent)
    for (s1, e1, _), (s2, e2, _) in zip(intervals, intervals[1:]):
        assert e1 <= s2, f"发送重叠: [{s1},{e1}) 与 [{s2},{e2})"
    for f in sent:
        src = arrivals[f.seq]
        assert f.start_ns >= src.arrive_ns, f"seq{f.seq} 凭空提前开始"
        if eff is None:
            assert f.config_epoch == 0
            use = old_s
        elif f.config_epoch == 0:
            assert f.start_ns < eff, f"seq{f.seq} 旧表发送却在切换后开始"
            use = old_s
        else:
            assert f.config_epoch == 1 and f.start_ns >= eff, \
                f"seq{f.seq} 恰在边界未按新表"
            use = new_s
        need = f.duration_ns + use.guard_ns
        assert f.end_ns == f.start_ns + f.duration_ns
        assert geom(use, f.queue, f.start_ns, need), \
            f"seq{f.seq} 在 {f.start_ns} 不满足门控几何"
        if eff is not None and f.config_epoch == 0:
            assert f.start_ns + need <= eff, "旧表发送越过切换点(含 guard)"
    for f in result.frames:
        if f.status is FrameStatus.NEVER_TRANSMITTABLE:
            judge = new_s if eff is not None else old_s
            rate = judge.link_rate_bits_per_ns
            d = _ceil_div(arrivals[f.seq].size_bytes * 8, rate)
            assert d + judge.guard_ns > maxwin(judge, f.queue), \
                f"seq{f.seq} 被判永不可发,但判定表存在容纳它的窗口"


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


class TestScheduledSwitchValidation(unittest.TestCase):
    """新表与生效时刻:接受请求前整体校验,失败不留部分结果。"""

    def test_effective_must_be_on_old_cycle_boundary(self):
        old = sched([GateWindow(0, 10, {0})], cycle=100)
        new = sched([GateWindow(0, 20, {0})], cycle=100)
        with self.assertRaisesRegex(TSNGateError, "周期边界"):
            replay([], old, scheduled_update=ScheduledUpdate(new, 50))
        # 边界上则被接受(即便没有帧)
        r = replay([], old, scheduled_update=ScheduledUpdate(new, 200))
        self.assertEqual(r.switch_effective_ns, 200)

    def test_boundary_uses_clock_offset_in_conversion(self):
        # 旧表 offset=5:本地边界 = 参考 t 满足 (t+5)%100==0 -> t=95
        old = sched([GateWindow(0, 10, {0})], cycle=100, offset=5)
        new = sched([GateWindow(0, 20, {0})], cycle=100, offset=5)
        with self.assertRaisesRegex(TSNGateError, "周期边界"):
            replay([], old, scheduled_update=ScheduledUpdate(new, 100))
        r = replay([], old, scheduled_update=ScheduledUpdate(new, 95))
        self.assertEqual(r.switch_effective_ns, 95)

    def test_new_table_validated_before_accept(self):
        old = sched([GateWindow(0, 10, {0})])
        # 新表自身非法:窗口超出周期——GateSchedule 构造时即拒绝,
        # 非法新表根本无法形成,更不可能被 replay 接受
        with self.assertRaisesRegex(TSNGateError, r"windows\[0\]"):
            GateSchedule(cycle_ns=10,
                         windows=(GateWindow(0, 11, frozenset({0})),),
                         link_rate_bits_per_ns=1)
        # 新表自身非法:同队列窗口重叠(同样在构造时拒绝)
        with self.assertRaisesRegex(TSNGateError, "重叠"):
            GateSchedule(
                cycle_ns=100,
                windows=(GateWindow(0, 30, frozenset({0})),
                         GateWindow(20, 40, frozenset({0}))),
                link_rate_bits_per_ns=1,
            )
        # 合法新表 + 非法生效时刻:在 replay 接受请求时被整体拒绝
        good_new = sched([GateWindow(0, 20, {0})], cycle=100)
        with self.assertRaisesRegex(TSNGateError, "周期边界"):
            replay([], old, scheduled_update=ScheduledUpdate(good_new, 30))

    def test_bad_update_type_and_negative_effective(self):
        old = sched([GateWindow(0, 10, {0})])
        with self.assertRaisesRegex(TSNGateError, "scheduled_update"):
            replay([], old, scheduled_update=object())
        with self.assertRaisesRegex(TSNGateError, "effective_ns"):
            ScheduledUpdate(old, -1)
        with self.assertRaisesRegex(TSNGateError, "new_schedule"):
            ScheduledUpdate(object(), 100)

    def test_no_update_field_is_none_in_result(self):
        s = sched([GateWindow(0, 10, {0})])
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], s)
        self.assertIsNone(r.switch_effective_ns)
        self.assertEqual(r.frames[0].config_epoch, 0)


class TestScheduledSwitchSemantics(unittest.TestCase):
    def test_arrival_exactly_on_boundary_old_open_new_closed_waits_new(self):
        # 旧表 q0 窗 [0,50);新表 q0 窗 [60,90)。eff=100 恰为边界。
        # 帧在 100 到达:旧表看此刻开门(若误按旧表会立刻发),新表要等到 160。
        old = sched([GateWindow(0, 50, {0})], cycle=100)
        new = sched([GateWindow(60, 90, {0})], cycle=100)
        upd = ScheduledUpdate(new, 100)
        r = replay([Frame(queue=0, arrive_ns=100, size_bytes=5)], old,
                   scheduled_update=upd)
        f = r.frames[0]
        self.assertEqual((f.start_ns, f.end_ns), (160, 165))
        self.assertEqual(f.config_epoch, 1)

    def test_arrival_on_boundary_old_fits_new_never_is_never(self):
        # 恰在边界到达:旧表 [0,50) 容得下 20ns,但旧配置在 eff 已失效;
        # 新表 [0,10) 任何周期都容不下 20ns -> 永不可发,不能借旧表发送。
        old = sched([GateWindow(0, 50, {0})], cycle=100)
        new = sched([GateWindow(0, 10, {0})], cycle=100)
        r = replay([Frame(queue=0, arrive_ns=100, size_bytes=20)], old,
                   scheduled_update=ScheduledUpdate(new, 100))
        self.assertEqual(r.frames[0].status, FrameStatus.NEVER_TRANSMITTABLE)
        self.assertIsNone(r.frames[0].start_ns)
        # 对照:不带切换时,同一帧在旧表下一周期 [100,120) 正常发送
        r0 = replay([Frame(queue=0, arrive_ns=100, size_bytes=20)], old)
        self.assertEqual((r0.frames[0].start_ns, r0.frames[0].end_ns), (100, 120))

    def test_old_blocks_new_opens(self):
        # 旧表 q0 仅窗 [40,47)(7ns):8ns 帧在旧表 alone 下即被证明永不可发;
        # 新表 q0 窗 [0,50)。帧 0 时刻到达,旧表下全程等不到机会,切换后新表
        # 立刻(100)发送——旧表的“永不可发”结论必须重算,不能丢帧。
        old = sched([GateWindow(40, 47, {0})], cycle=100)
        new = sched([GateWindow(0, 50, {0})], cycle=100)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=8)], old,
                   scheduled_update=ScheduledUpdate(new, 100))
        f = r.frames[0]
        self.assertEqual(f.status, FrameStatus.SENT)
        self.assertEqual((f.start_ns, f.end_ns), (100, 108))
        self.assertEqual(f.config_epoch, 1)
        self.assertIn(WaitReason.GATE_CLOSED, f.wait_reasons)
        self.assertIn(WaitReason.INSUFFICIENT_WINDOW, f.wait_reasons)

    def test_old_never_conclusion_recomputed_not_dropped(self):
        # 大帧 30ns:旧表最长窗 10 -> 旧表 alone 永不可发;
        # 新表窗 40ns -> 切换后发送。绝不允许直接丢帧。
        old = sched([GateWindow(0, 10, {0})], cycle=100)
        new = sched([GateWindow(0, 40, {0})], cycle=100)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=30)], old,
                   scheduled_update=ScheduledUpdate(new, 100))
        f = r.frames[0]
        self.assertEqual(f.status, FrameStatus.SENT)
        self.assertEqual(f.start_ns, 100)

    def test_frame_in_flight_completes_nonpreemptively(self):
        # 旧表 q0 窗 [0,100) 全周期开;新表 q0 无窗。eff=100。
        # 帧 60ns 在 50 到达:旧表下 50+60=110,含 guard0 顶过 eff?
        # 旧窗 [0,100) 实例末端恰为 100(边界对齐),50 时剩余 50 容不下 60,
        # 故该构造不会在途。改为帧在 40 开始、50ns 长:40+50=90 <= 100,可在途。
        old = sched([GateWindow(0, 100, {0})], cycle=100)
        new = sched([GateWindow(0, 1, {1})], cycle=100)  # q0 新表完全无窗
        arrivals = [
            Frame(queue=0, arrive_ns=40, size_bytes=50),  # [40,90) 旧表发完
            Frame(queue=0, arrive_ns=40, size_bytes=5),   # 90 时旧窗剩 10 容得下?
        ]
        # 第二帧 90 开始 -> 95 <= 100,也在旧表发完
        r = replay(arrivals, old, scheduled_update=ScheduledUpdate(new, 100))
        self.assertEqual(
            [(f.start_ns, f.end_ns, f.config_epoch) for f in r.frames
             if f.status is FrameStatus.SENT],
            [(40, 90, 0), (90, 95, 0)],
        )

    def test_no_reservation_beyond_switch_under_old_table(self):
        # 不能借旧表预约 eff 之后的旧窗口实例。
        # 旧表 q0/q1 窗均为 [0,60);eff=100。
        # q0 阻塞帧 11ns:[0,11);q1 目标帧 50ns,0 时刻可发但被(更高优先级队列号
        # 更小的)q0 帧挡住,11 时旧窗剩 49 < 50;旧表下一个 q1 实例 [100,160)
        # 在切换点之后——若错误地用旧表预约,就会在 100 违法起帧。
        # 新表 q1 最长窗 30 < 50 -> 目标帧在切换后被(动态)证明永不可发。
        old = sched([GateWindow(0, 60, {0, 1})], cycle=100)
        new = sched([GateWindow(0, 30, {0, 1})], cycle=100)
        arrivals = [
            Frame(queue=0, arrive_ns=0, size_bytes=11),
            Frame(queue=1, arrive_ns=0, size_bytes=50),
        ]
        r = replay(arrivals, old, scheduled_update=ScheduledUpdate(new, 100))
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (0, 11))
        f1 = r.frames[1]
        self.assertEqual(f1.status, FrameStatus.NEVER_TRANSMITTABLE)
        self.assertIsNone(f1.start_ns)
        self.assertIn(WaitReason.LINK_BUSY, f1.wait_reasons)
        self.assertIn(WaitReason.INSUFFICIENT_WINDOW, f1.wait_reasons)

    def test_window_straddling_switch_guard_must_finish_before(self):
        # offset 让一个旧窗口在参考时钟上横跨切换点是不可能的(eff 对齐边界),
        # 但用 guard 制造“帧本身在 eff 前结束、guard 越过 eff”的情形:
        # 旧窗 [0,100) 全周期,guard=20。帧 90ns 在 0 到达,0+90+20=110 > 100,
        # 起始 0 处旧窗剩余 100 容不下 110 -> 不能在旧表开始。
        old = sched([GateWindow(0, 100, {0})], cycle=100, guard=20)
        new = sched([GateWindow(0, 100, {0})], cycle=100, guard=0)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=90)], old,
                   scheduled_update=ScheduledUpdate(new, 100))
        # 新表 guard=0 容得下,100 开始
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (100, 190))
        self.assertEqual(r.frames[0].config_epoch, 1)

    def test_dynamic_never_only_after_switch_not_pre_dropped(self):
        # 帧在旧表最后周期才获得几何机会却被高优先级帧挤掉,新表又永不可发:
        # 旧表 q0 窗 [0,50);新表 q0 窗 [0,5)(最长5)。
        # 两帧均 10ns:帧0 在 0 开始[0,10);帧1 0 到达,10 时旧窗剩40 容得下,
        # [10,20) 也发完 —— 需要构造竞争到切换前没开始:用 5 个 10ns 帧,
        # 窗 [0,50) 恰好放 5 个;第 6 帧几何上旧表有机会(它到达时无法预知),
        # 但 50ns 已到切换点。简化:帧1 到达 45,need=10,旧窗剩 5 容不下,
        # 但更早周期实例 [0,50) 对它不存在(到达 45),几何机会只在它到达后:
        # 45+10>50 且 eff=100,旧前缀无机会 -> 属于静态不可发。
        # 真正的动态情形:帧1 在 0 到达,被帧0(40ns)占 [0,40),40 时窗剩10
        # 恰好放帧1(10ns) -> 会发掉。改帧0=41ns:帧1 在 41 时剩 9 < 10,
        # 旧前缀几何机会存在(它本可在 0..40 之间开始,竞争失败),切换后定论。
        old = sched([GateWindow(0, 50, {0})], cycle=100)
        new = sched([GateWindow(0, 5, {0})], cycle=100)
        arrivals = [
            Frame(queue=0, arrive_ns=0, size_bytes=41),  # [0,41)
            Frame(queue=0, arrive_ns=0, size_bytes=10),  # 41 时剩 9,错过
        ]
        r = replay(arrivals, old, scheduled_update=ScheduledUpdate(new, 100))
        f0, f1 = r.frames
        self.assertEqual((f0.start_ns, f0.end_ns), (0, 41))
        self.assertEqual(f1.status, FrameStatus.NEVER_TRANSMITTABLE)
        self.assertIn(WaitReason.LINK_BUSY, f1.wait_reasons)
        self.assertEqual(r.never_count, 1)

    def test_queue_order_unchanged_across_switch(self):
        # 切换后同时排队:排队先后仍按 (队列号, 到达序号)
        old = sched([GateWindow(0, 100, {0, 1})], cycle=100)
        new = sched([GateWindow(0, 100, {0, 1})], cycle=100)
        arrivals = [
            Frame(queue=1, arrive_ns=90, size_bytes=20),  # 90+20>100 旧表不开始
            Frame(queue=0, arrive_ns=90, size_bytes=20),
        ]
        r = replay(arrivals, old, scheduled_update=ScheduledUpdate(new, 100))
        starts = {f.seq: f.start_ns for f in r.frames}
        self.assertEqual(starts[1], 100)  # 队列 0 先
        self.assertEqual(starts[0], 120)

    def test_switch_with_offset_on_both_sides(self):
        # 旧表 offset=7,eff 必须满足 (eff+7)%100==0 -> eff=93。
        # 旧 q0 窗 [0,20) 在参考时钟上为 [93,113)? 93+7=100 相位0。
        # 帧 5ns 在 80 到达:旧表 80 相位 87,门闭;旧下一窗 93=eff,归新表。
        # 新表 offset=-3,新 q0 窗 [0,10):100 处相位 97 关;下一窗参考 103。
        old = sched([GateWindow(0, 20, {0})], cycle=100, offset=7)
        new = sched([GateWindow(0, 10, {0})], cycle=100, offset=-3)
        r = replay([Frame(queue=0, arrive_ns=80, size_bytes=5)], old,
                   scheduled_update=ScheduledUpdate(new, 93))
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (103, 108))

    def test_rate_change_changes_duration_after_switch(self):
        old = sched([GateWindow(0, 100, {0})], cycle=100, rate=8)  # 1B=1ns
        new = sched([GateWindow(0, 100, {0})], cycle=100, rate=4)  # 1B=2ns
        r = replay([Frame(queue=0, arrive_ns=100, size_bytes=5)], old,
                   scheduled_update=ScheduledUpdate(new, 100))
        self.assertEqual(r.frames[0].duration_ns, 10)
        self.assertEqual(r.frames[0].end_ns, 110)


class TestSwitchAgainstTickReference(unittest.TestCase):
    """带切换的确定性随机用例:事件驱动核心 vs 独立逐 tick 参考。"""

    def _random_windows(self, rng, cycle):
        nwin = rng.randint(1, 3)
        windows = []
        for _ in range(nwin):
            start = rng.randint(0, cycle - 2)
            end = rng.randint(start + 1, cycle)
            qs = frozenset(rng.sample(range(3), rng.randint(1, 3)))
            windows.append((start, end, qs))
        for q in range(3):
            segs = sorted((s, e) for (s, e, qs) in windows if q in qs)
            for (s1, e1), (s2, _e2) in zip(segs, segs[1:]):
                if s2 < e1:
                    return None
        return windows

    def test_fuzz_with_switch(self):
        rng = random.Random(20260925)
        checked = 0
        case = 0
        while checked < 300:
            case += 1
            old_cycle = rng.randint(20, 60)
            old_wins = self._random_windows(rng, old_cycle)
            new_cycle = rng.randint(20, 60)
            new_wins = self._random_windows(rng, new_cycle)
            if old_wins is None or new_wins is None:
                continue
            old_rate = rng.randint(1, 4)
            old_guard = rng.randint(0, 4)
            old_off = rng.randint(-8, 8)
            new_rate = rng.randint(1, 4)
            new_guard = rng.randint(0, 4)
            new_off = rng.randint(-8, 8)
            # eff 对齐旧表本地边界:选旧周期的第 k 个边界再平移
            k = rng.randint(1, 3)
            eff = k * old_cycle - old_off

            old_s = GateSchedule(
                cycle_ns=old_cycle,
                windows=tuple(GateWindow(s, e, qs) for s, e, qs in old_wins),
                link_rate_bits_per_ns=old_rate, guard_ns=old_guard,
                clock_offset_ns=old_off)
            new_s = GateSchedule(
                cycle_ns=new_cycle,
                windows=tuple(GateWindow(s, e, qs) for s, e, qs in new_wins),
                link_rate_bits_per_ns=new_rate, guard_ns=new_guard,
                clock_offset_ns=new_off)
            nframes = rng.randint(1, 8)
            frames = []
            for _ in range(nframes):
                frames.append((
                    rng.randint(0, 2),
                    rng.randint(0, 3 * old_cycle),
                    rng.randint(1, 6),
                    None,
                ))
            arrivals = [Frame(queue=q, arrive_ns=a, size_bytes=z)
                        for q, a, z, _ in frames]
            expected = reference_replay(
                frames, old_wins, old_cycle, old_rate, old_guard, old_off,
                update=(new_wins, new_cycle, new_rate, new_guard, new_off, eff))
            result = replay(arrivals, old_s,
                            scheduled_update=ScheduledUpdate(new_s, eff))
            for i, (exp, got) in enumerate(zip(expected, result.frames)):
                if exp == (None, None):
                    self.assertEqual(got.status, FrameStatus.NEVER_TRANSMITTABLE,
                                     f"case {case} frame {i}: 参考判永不可发")
                else:
                    self.assertEqual((got.start_ns, got.end_ns), exp,
                                     f"case {case} frame {i}: 核心与逐tick不一致")
            audit_no_overlap_and_gating(result, arrivals, old_s, eff, new_s)
            checked += 1
        self.assertGreater(checked, 200)

    def test_fuzz_without_switch_still_matches(self):
        rng = random.Random(424242)
        for case in range(120):
            cycle = rng.randint(10, 60)
            windows = self._random_windows(rng, cycle)
            if windows is None:
                continue
            rate = rng.randint(1, 4)
            guard = rng.randint(0, 5)
            offset = rng.randint(-10, 10)
            schedule = GateSchedule(
                cycle_ns=cycle,
                windows=tuple(GateWindow(s, e, qs) for s, e, qs in windows),
                link_rate_bits_per_ns=rate, guard_ns=guard,
                clock_offset_ns=offset)
            frames = [(rng.randint(0, 2), rng.randint(0, 3 * cycle),
                       rng.randint(1, 6), None) for _ in range(rng.randint(1, 8))]
            arrivals = [Frame(queue=q, arrive_ns=a, size_bytes=z)
                        for q, a, z, _ in frames]
            expected = reference_replay(frames, windows, cycle, rate, guard, offset)
            result = replay(arrivals, schedule)
            for i, (exp, got) in enumerate(zip(expected, result.frames)):
                if exp == (None, None):
                    self.assertEqual(got.status, FrameStatus.NEVER_TRANSMITTABLE)
                else:
                    self.assertEqual((got.start_ns, got.end_ns), exp)
            audit_no_overlap_and_gating(result, arrivals, schedule)


if __name__ == "__main__":
    unittest.main()
