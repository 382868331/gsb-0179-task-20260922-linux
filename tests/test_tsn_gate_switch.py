"""预定门表切换(ConfigChange)单元测试:定点用例 + 与独立逐 tick 参考的随机对照。"""

import random
import unittest

from tsn_gate import (
    BudgetExhaustedError,
    ConfigChange,
    Frame,
    FrameStatus,
    GateSchedule,
    GateWindow,
    TSNGateError,
    WaitReason,
    replay,
)
from reference import reference_replay_switch, verify_timeline


def sched(windows, cycle=100, rate=8, guard=0, offset=0):
    # rate=8 bit/ns => 1 字节恰好 1ns,便于手算期望值
    return GateSchedule(
        cycle_ns=cycle,
        windows=tuple(windows),
        link_rate_bits_per_ns=rate,
        guard_ns=guard,
        clock_offset_ns=offset,
    )


def change(new, eff, acc=0):
    return ConfigChange(schedule=new, effective_ns=eff, accepted_ns=acc)


class TestChangeValidation(unittest.TestCase):
    def setUp(self):
        self.old = sched([GateWindow(0, 50, {0})])
        self.new = sched([GateWindow(0, 80, {0})])

    def test_effective_must_hit_old_boundary(self):
        with self.assertRaisesRegex(TSNGateError, "effective_ns.*旧周期边界"):
            replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], self.old,
                   config_change=change(self.new, 150))

    def test_boundary_checked_in_local_time_with_offset(self):
        # 本地 = 参考 + 5,周期 100:本地边界对应参考时刻 95, 195, ...
        old = sched([GateWindow(0, 50, {0})], offset=5)
        new = sched([GateWindow(0, 80, {0})], offset=5)
        with self.assertRaisesRegex(TSNGateError, "effective_ns"):
            replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], old,
                   config_change=change(new, 100))  # (100+5)%100 != 0
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], old,
                   config_change=change(new, 95))    # (95+5)%100 == 0
        self.assertEqual(r.frames[0].status, FrameStatus.SENT)

    def test_accepted_after_effective_rejected(self):
        with self.assertRaisesRegex(TSNGateError, "accepted_ns"):
            change(self.new, 100, acc=200)

    def test_negative_times_rejected(self):
        with self.assertRaisesRegex(TSNGateError, "effective_ns"):
            change(self.new, -100)
        with self.assertRaisesRegex(TSNGateError, "accepted_ns"):
            change(self.new, 100, acc=-1)

    def test_offset_must_match(self):
        new = sched([GateWindow(0, 80, {0})], offset=1)
        with self.assertRaisesRegex(TSNGateError, "clock_offset_ns"):
            replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], self.old,
                   config_change=change(new, 100))

    def test_rate_must_match(self):
        new = sched([GateWindow(0, 80, {0})], rate=4)
        with self.assertRaisesRegex(TSNGateError, "link_rate_bits_per_ns"):
            replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], self.old,
                   config_change=change(new, 100))

    def test_bool_and_float_times_rejected(self):
        with self.assertRaisesRegex(TSNGateError, "effective_ns"):
            change(self.new, True)
        with self.assertRaisesRegex(TSNGateError, "effective_ns"):
            change(self.new, 100.0)
        with self.assertRaisesRegex(TSNGateError, "effective_ns"):
            change(self.new, float("nan"))

    def test_new_schedule_must_be_gate_schedule(self):
        with self.assertRaisesRegex(TSNGateError, "ConfigChange.schedule"):
            ConfigChange(schedule=object(), effective_ns=100)

    def test_config_change_type_checked_in_replay(self):
        with self.assertRaisesRegex(TSNGateError, "config_change"):
            replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], self.old,
                   config_change=object())

    def test_rejected_change_leaves_no_partial_result(self):
        # 非法切换请求整体抛错;同一批帧在单表重放下不受影响
        arrivals = [Frame(queue=0, arrive_ns=0, size_bytes=1)]
        with self.assertRaises(TSNGateError):
            replay(arrivals, self.old, config_change=change(self.new, 150))
        r = replay(arrivals, self.old)
        self.assertEqual(r.frames[0].status, FrameStatus.SENT)


class TestArrivalExactlyAtBoundary(unittest.TestCase):
    def test_boundary_arrival_uses_new_table(self):
        # 恰在生效时刻到达:按新表判断(旧表此时可发,新表门未开)
        old = sched([GateWindow(0, 60, {0})])
        new = sched([GateWindow(10, 60, {0})])
        r = replay([Frame(queue=0, arrive_ns=200, size_bytes=1)], old,
                   config_change=change(new, 200))
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (210, 211))

    def test_boundary_arrival_new_open_sends_immediately(self):
        # 反向:旧表此刻门关,新表门开
        old = sched([GateWindow(10, 60, {0})])
        new = sched([GateWindow(0, 60, {0})])
        r = replay([Frame(queue=0, arrive_ns=200, size_bytes=1)], old,
                   config_change=change(new, 200))
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (200, 201))


class TestOldOnlyAndNewOnly(unittest.TestCase):
    def test_old_only_frame_sent_before_switch(self):
        # 新表最长窗口 10 < need 20:只能靠旧表,且存在可行旧表开始
        old = sched([GateWindow(0, 60, {0})])
        new = sched([GateWindow(0, 10, {0})])
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=20)], old,
                   config_change=change(new, 200))
        f = r.frames[0]
        self.assertEqual(f.status, FrameStatus.SENT)
        self.assertEqual((f.start_ns, f.end_ns), (0, 20))

    def test_old_only_frame_too_late_becomes_never(self):
        # 到达 185:旧表最晚可行开始 140 已错过,新表容不下 -> 重算后永不可发
        # (单表重放下该帧本可在 200 发送,证明结论确实被重新计算)
        old = sched([GateWindow(0, 60, {0})])
        new = sched([GateWindow(0, 10, {0})])
        frame = Frame(queue=0, arrive_ns=185, size_bytes=20)
        r = replay([frame], old, config_change=change(new, 200))
        f = r.frames[0]
        self.assertEqual(f.status, FrameStatus.NEVER_TRANSMITTABLE)
        self.assertIsNone(f.start_ns)
        self.assertEqual(r.never_count, 1)
        r_single = replay([frame], old)
        self.assertEqual(r_single.frames[0].status, FrameStatus.SENT)
        self.assertEqual(r_single.frames[0].start_ns, 200)

    def test_old_never_new_can_is_recomputed_not_dropped(self):
        # 旧表任何窗口都容不下(旧表永不可发),新表容得下 -> 生效后发送
        old = sched([GateWindow(0, 10, {0})])
        new = sched([GateWindow(0, 60, {0})])
        frame = Frame(queue=0, arrive_ns=0, size_bytes=20)
        r = replay([frame], old, config_change=change(new, 200))
        f = r.frames[0]
        self.assertEqual(f.status, FrameStatus.SENT)
        self.assertEqual((f.start_ns, f.end_ns), (200, 220))
        r_single = replay([frame], old)
        self.assertEqual(r_single.frames[0].status, FrameStatus.NEVER_TRANSMITTABLE)

    def test_old_only_frame_missing_last_start_during_contention(self):
        # 两帧争用:低优先级帧被挡过最后旧表开始时刻,重放中证明永不可发
        old = sched([GateWindow(0, 60, {0})])
        new = sched([GateWindow(0, 10, {0})])
        arrivals = [
            Frame(queue=0, arrive_ns=130, size_bytes=20),  # seq0 先发(130..150)
            Frame(queue=0, arrive_ns=135, size_bytes=20),  # seq1 最后可行开始 140 被错过
        ]
        r = replay(arrivals, old, config_change=change(new, 200))
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (130, 150))
        self.assertEqual(r.frames[1].status, FrameStatus.NEVER_TRANSMITTABLE)
        self.assertIsNone(r.frames[1].start_ns)


class TestInFlightFrames(unittest.TestCase):
    def test_in_flight_at_acceptance_continues_to_completion(self):
        # acc=50 时 seq0 已在途(0..60):不受切换影响,继续发送到完成
        old = sched([GateWindow(0, 100, {0})])
        new = sched([GateWindow(0, 100, {0})])
        arrivals = [
            Frame(queue=0, arrive_ns=0, size_bytes=60),    # 在 acc 前开始
            Frame(queue=0, arrive_ns=0, size_bytes=1),     # 等链路,acc 后按约束判断
        ]
        r = replay(arrivals, old, config_change=change(new, 200, acc=50))
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (0, 60))
        self.assertEqual((r.frames[1].start_ns, r.frames[1].end_ns), (60, 61))
        self.assertIn(WaitReason.LINK_BUSY, r.frames[1].wait_reasons)
        verify_timeline(
            [(f.start_ns, f.end_ns) for f in r.frames],
            [(f.queue, f.arrive_ns, f.size_bytes, None) for f in arrivals],
            [(0, 100, frozenset({0}))], 100, 0,
            [(0, 100, frozenset({0}))], 100, 0,
            rate=8, offset=0, eff=200, acc=50,
        )

    def test_started_frames_are_never_rejudged_by_new_table(self):
        # 已开始的帧即使在新表下“永不可发”(新表容不下),也照常完成
        old = sched([GateWindow(0, 100, {0})])
        new = sched([GateWindow(0, 10, {0})])
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=50)], old,
                   config_change=change(new, 200, acc=50))
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (0, 50))


class TestGuardAndReservationAtSwitch(unittest.TestCase):
    def test_guard_included_when_finishing_before_switch(self):
        # guard 计入:seq1 的 end+guard 恰好顶到生效时刻,允许
        old = sched([GateWindow(0, 100, {0})], guard=10)
        new = sched([GateWindow(0, 100, {0})], guard=10)
        arrivals = [
            Frame(queue=0, arrive_ns=0, size_bytes=90),   # need 100:0..90
            Frame(queue=0, arrive_ns=1, size_bytes=90),   # 100..190,guard 到 200 == eff
            Frame(queue=0, arrive_ns=2, size_bytes=90),   # 只能等生效后按新表
        ]
        r = replay(arrivals, old, config_change=change(new, 200))
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (0, 90))
        self.assertEqual((r.frames[1].start_ns, r.frames[1].end_ns), (100, 190))
        self.assertEqual((r.frames[2].start_ns, r.frames[2].end_ns), (200, 290))

    def test_window_edge_at_switch_not_reserved_by_old_table(self):
        # 旧窗口剩余部分容不下,旧表下一窗口起点在生效后 -> 不预约,按新表判断
        old = sched([GateWindow(50, 100, {0})])
        new = sched([GateWindow(0, 100, {0})])
        r = replay([Frame(queue=0, arrive_ns=150, size_bytes=55)], old,
                   config_change=change(new, 200))
        f = r.frames[0]
        self.assertEqual((f.start_ns, f.end_ns), (200, 255))
        self.assertIn(WaitReason.INSUFFICIENT_WINDOW, f.wait_reasons)


class TestQueueOrderAcrossSwitch(unittest.TestCase):
    def test_priority_then_seq_unchanged(self):
        windows = [GateWindow(0, 50, {0, 1})]
        old = sched(windows)
        new = sched(windows)
        arrivals = [
            Frame(queue=1, arrive_ns=200, size_bytes=1),  # seq0
            Frame(queue=0, arrive_ns=200, size_bytes=1),  # seq1
            Frame(queue=0, arrive_ns=200, size_bytes=1),  # seq2
        ]
        r = replay(arrivals, old, config_change=change(new, 200))
        starts = {f.seq: f.start_ns for f in r.frames}
        self.assertEqual(starts[1], 200)
        self.assertEqual(starts[2], 201)
        self.assertEqual(starts[0], 202)


class TestBudgetWithChange(unittest.TestCase):
    def test_budget_exhausted_is_distinct_from_never(self):
        old = sched([GateWindow(10, 20, {0})])
        new = sched([GateWindow(10, 20, {0})])
        with self.assertRaises(BudgetExhaustedError):
            replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], old,
                   config_change=change(new, 100), max_events=1)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], old,
                   config_change=change(new, 100))
        self.assertEqual(r.frames[0].status, FrameStatus.SENT)


class TestSwitchAgainstTickReference(unittest.TestCase):
    """确定性随机小整数用例:事件驱动核心 vs 独立逐 tick 切换参考,
    并对核心输出逐纳秒扫描验证无重叠、无凭空提前开始。"""

    def test_fuzz_switch_cases(self):
        rng = random.Random(20260925)
        checked = 0
        for case in range(400):
            old_cycle = rng.randint(10, 60)
            new_cycle = rng.randint(10, 60)

            def rand_windows(cycle):
                wins = []
                for _ in range(rng.randint(1, 3)):
                    s = rng.randint(0, cycle - 2)
                    e = rng.randint(s + 1, cycle)
                    qs = frozenset(rng.sample(range(3), rng.randint(1, 3)))
                    wins.append((s, e, qs))
                return wins

            def overlaps(wins):
                for q in range(3):
                    segs = sorted((s, e) for (s, e, qs) in wins if q in qs)
                    for (s1, e1), (s2, _e2) in zip(segs, segs[1:]):
                        if s2 < e1:
                            return True
                return False

            old_w, new_w = rand_windows(old_cycle), rand_windows(new_cycle)
            if overlaps(old_w) or overlaps(new_w):
                continue
            rate = rng.randint(1, 4)
            old_guard = rng.randint(0, 5)
            new_guard = rng.randint(0, 5)
            offset = rng.randint(-10, 10)
            eff = rng.randint(1, 4) * old_cycle - offset  # 旧周期边界(本地相位 0)
            if eff < 0:
                continue
            acc = rng.randint(0, eff)
            frames = []
            for _ in range(rng.randint(1, 8)):
                frames.append((
                    rng.randint(0, 2),
                    rng.randint(0, eff + 2 * new_cycle + 20),
                    rng.randint(1, 6),
                    None,
                ))
            old_sched = GateSchedule(
                cycle_ns=old_cycle,
                windows=tuple(GateWindow(s, e, qs) for (s, e, qs) in old_w),
                link_rate_bits_per_ns=rate, guard_ns=old_guard,
                clock_offset_ns=offset,
            )
            new_sched = GateSchedule(
                cycle_ns=new_cycle,
                windows=tuple(GateWindow(s, e, qs) for (s, e, qs) in new_w),
                link_rate_bits_per_ns=rate, guard_ns=new_guard,
                clock_offset_ns=offset,
            )
            arrivals = [Frame(queue=q, arrive_ns=a, size_bytes=z, deadline_ns=d)
                        for (q, a, z, d) in frames]
            expected = reference_replay_switch(
                frames, old_w, old_cycle, old_guard,
                new_w, new_cycle, new_guard, rate, offset, eff, acc,
            )
            result = replay(
                arrivals, old_sched,
                config_change=ConfigChange(schedule=new_sched,
                                           effective_ns=eff, accepted_ns=acc),
            )
            got = [(f.start_ns, f.end_ns) for f in result.frames]
            for i, (exp, g) in enumerate(zip(expected, got)):
                self.assertEqual(
                    g, exp, f"case {case} frame {i}: 核心与逐 tick 切换参考不一致",
                )
            # 逐纳秒可行性扫描:无重叠发送、无凭空提前开始、不预约切换后时段
            verify_timeline(got, frames, old_w, old_cycle, old_guard,
                            new_w, new_cycle, new_guard, rate, offset, eff, acc)
            checked += 1
        self.assertGreater(checked, 100)  # 确认随机用例确实大量执行


if __name__ == "__main__":
    unittest.main()
