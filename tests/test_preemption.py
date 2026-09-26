"""帧抢占(express/preemptable)单元测试 + 与独立逐字节参考的随机对照。"""

import random
import unittest

from tsn_gate import (
    BudgetExhaustedError,
    Frame,
    FrameStatus,
    GateSchedule,
    GateWindow,
    PreemptionConfig,
    TSNGateError,
    WaitReason,
    replay,
    replay_preemptive,
)
from reference import reference_preemptive, verify_occupancy


def sched(windows, cycle=1000, rate=1, guard=0, offset=0):
    # rate=1 bit/ns => 1 字节 = 8ns
    return GateSchedule(
        cycle_ns=cycle,
        windows=tuple(windows),
        link_rate_bits_per_ns=rate,
        guard_ns=guard,
        clock_offset_ns=offset,
    )


def cfg(express=(0,), frag=30, ov=6):
    return PreemptionConfig(express_queues=frozenset(express),
                            fragment_size_bytes=frag,
                            fragment_overhead_bytes=ov)


def occ_check(result, frames, windows, cycle, rate, guard, offset, c):
    frags = []
    for f in result.frames:
        frags.append(None if f.status is FrameStatus.NEVER_TRANSMITTABLE
                     else list(f.fragments))
    verify_occupancy(frames, frags, windows, cycle, rate, guard, offset,
                     set(c.express_queues), c.fragment_size_bytes,
                     c.fragment_overhead_bytes)


class TestExpressPreemptsAtBoundary(unittest.TestCase):
    def test_express_waits_for_fragment_boundary(self):
        # 窗口 [0,800) 对 q0/q1 都开;可抢占帧 90B 分 3 片(30B=240ns);
        # 紧急帧 t=100 到达,须等到片段边界 t=240 才发送
        s = sched([GateWindow(0, 800, {0, 1})])
        c = cfg()
        arrivals = [
            Frame(queue=1, arrive_ns=0, size_bytes=90),
            Frame(queue=0, arrive_ns=100, size_bytes=20),
        ]
        r = replay_preemptive(arrivals, s, c)
        f0, f1 = r.frames
        self.assertEqual(f0.fragments, ((0, 240), (400, 688), (688, 976)))
        self.assertEqual((f0.start_ns, f0.end_ns), (0, 976))
        self.assertEqual(f0.preempted_count, 1)
        self.assertEqual(f0.overhead_bytes, 12)  # 2 次恢复 * 6B
        self.assertIn(WaitReason.PREEMPTED, f0.wait_reasons)
        self.assertEqual((f1.start_ns, f1.end_ns), (240, 400))
        self.assertEqual(f1.waited_ns, 140)  # 等待到片段边界
        self.assertEqual(f1.fragments, ((240, 400),))
        occ_check(r, [(1, 0, 90, None), (0, 100, 20, None)],
                  [(0, 800, {0, 1})], 1000, 1, 0, 0, c)

    def test_preemption_disabled_keeps_old_result(self):
        # 不启用抢占(旧 replay):紧急帧须等整个 720ns 帧 + 窗口约束
        s = sched([GateWindow(0, 800, {0, 1})])
        arrivals = [
            Frame(queue=1, arrive_ns=0, size_bytes=90),
            Frame(queue=0, arrive_ns=100, size_bytes=20),
        ]
        r = replay(arrivals, s)
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (0, 720))
        # 720 时刻剩余窗口 80ns < 160ns,紧急帧等下一周期
        self.assertEqual((r.frames[1].start_ns, r.frames[1].end_ns), (1000, 1160))
        self.assertEqual(r.frames[1].fragments, ())  # 旧模式无片段信息


class TestSameInstantOrdering(unittest.TestCase):
    def test_arrival_exactly_at_fragment_boundary(self):
        # 同刻:片段完成(t=240)与紧急帧到达(t=240)-> 完成后到达即参与决策,紧急帧胜出
        s = sched([GateWindow(0, 800, {0, 1})])
        c = cfg()
        arrivals = [
            Frame(queue=1, arrive_ns=0, size_bytes=60),
            Frame(queue=0, arrive_ns=240, size_bytes=10),
        ]
        r = replay_preemptive(arrivals, s, c)
        self.assertEqual(r.frames[1].start_ns, 240)
        self.assertEqual(r.frames[1].waited_ns, 0)
        self.assertEqual(r.frames[0].fragments, ((0, 240), (320, 608)))


class TestGateRules(unittest.TestCase):
    def test_gate_close_does_not_truncate_started_fragment(self):
        # 片段 [480,720) 跨过门关闭点 600:不被截断;但 600 后不能开新片
        s = sched([GateWindow(0, 600, {1})])
        c = cfg(express=(), frag=30, ov=0)
        r = replay_preemptive([Frame(queue=1, arrive_ns=0, size_bytes=90)], s, c)
        f = r.frames[0]
        self.assertEqual(f.fragments, ((0, 240), (240, 480), (480, 720)))
        self.assertEqual(f.end_ns, 720)
        occ_check(r, [(1, 0, 90, None)], [(0, 600, {1})], 1000, 1, 0, 0, c)

    def test_no_new_fragment_after_gate_close(self):
        # 片段恰在门关闭点结束:下一片等下一周期窗口
        s = sched([GateWindow(0, 600, {1})])
        c = cfg(express=(), frag=30, ov=0)
        r = replay_preemptive([Frame(queue=1, arrive_ns=360, size_bytes=60)], s, c)
        f = r.frames[0]
        self.assertEqual(f.fragments, ((360, 600), (1000, 1240)))

    def test_express_must_satisfy_own_gate(self):
        # 紧急帧到达时自己的门未开:可抢占帧继续;紧急帧门开后等片段边界
        s = sched([GateWindow(0, 600, {1}), GateWindow(500, 900, {0})])
        c = cfg()
        arrivals = [
            Frame(queue=1, arrive_ns=0, size_bytes=90),
            Frame(queue=0, arrive_ns=100, size_bytes=10),
        ]
        r = replay_preemptive(arrivals, s, c)
        f0, f1 = r.frames
        # q1 片段 [0,240) [240,528);紧急帧门 500 才开,在边界 528 抢占;
        # 608 时 q1 门已关,第 3 片等下一周期
        self.assertEqual((f1.start_ns, f1.end_ns), (528, 608))
        self.assertIn(WaitReason.GATE_CLOSED, f1.wait_reasons)
        self.assertEqual(f0.fragments, ((0, 240), (240, 528), (1000, 1288)))
        self.assertEqual(f0.preempted_count, 1)


class TestPausedResumeOrder(unittest.TestCase):
    def test_multiple_paused_resume_in_original_start_order(self):
        # A(q1) 先开始被 X1 抢占;A 门关闭期间 B(q2) 开始,又被 X2 抢占;
        # 两帧同时暂停时按原开始顺序恢复:A(0) 先于 B(170)
        s = sched([GateWindow(0, 100, {1}),
                   GateWindow(300, 500, {1}),
                   GateWindow(0, 900, {0, 2})])
        c = cfg(frag=10, ov=0)  # 10B = 80ns 一片
        arrivals = [
            Frame(queue=1, arrive_ns=0, size_bytes=20),    # A
            Frame(queue=0, arrive_ns=10, size_bytes=10),   # X1
            Frame(queue=2, arrive_ns=170, size_bytes=20),  # B
            Frame(queue=0, arrive_ns=200, size_bytes=10),  # X2
        ]
        r = replay_preemptive(arrivals, s, c)
        a, x1, b, x2 = r.frames
        self.assertEqual((x1.start_ns, x1.end_ns), (80, 160))
        self.assertEqual((x2.start_ns, x2.end_ns), (250, 330))
        self.assertEqual(a.fragments, ((0, 80), (330, 410)))
        self.assertEqual(b.fragments, ((170, 250), (410, 490)))
        self.assertEqual(a.preempted_count, 1)
        self.assertEqual(b.preempted_count, 1)


class TestOverheadAccounting(unittest.TestCase):
    def test_each_resume_adds_overhead(self):
        s = sched([GateWindow(0, 900, {0, 1})])
        c = cfg(frag=30, ov=10)  # 恢复片 = 30+10=40B = 320ns
        arrivals = [
            Frame(queue=1, arrive_ns=0, size_bytes=90),
            Frame(queue=0, arrive_ns=10, size_bytes=10),
        ]
        r = replay_preemptive(arrivals, s, c)
        f0 = r.frames[0]
        self.assertEqual(f0.fragments, ((0, 240), (320, 640), (640, 960)))
        self.assertEqual(f0.overhead_bytes, 20)
        self.assertEqual(f0.duration_ns, 720)  # 仅载荷,不含开销


class TestNeverAndBudget(unittest.TestCase):
    def test_window_not_longer_than_guard_is_never(self):
        s = sched([GateWindow(0, 50, {0})], guard=50)  # 窗口 50 <= guard 50
        c = cfg()
        r = replay_preemptive([Frame(queue=0, arrive_ns=0, size_bytes=1)], s, c)
        self.assertEqual(r.frames[0].status, FrameStatus.NEVER_TRANSMITTABLE)

    def test_no_window_never(self):
        s = sched([GateWindow(0, 50, {0})])
        r = replay_preemptive([Frame(queue=1, arrive_ns=0, size_bytes=1)], s, cfg())
        self.assertEqual(r.frames[0].status, FrameStatus.NEVER_TRANSMITTABLE)

    def test_budget_exhausted(self):
        s = sched([GateWindow(10, 900, {0, 1})])
        with self.assertRaises(BudgetExhaustedError):
            replay_preemptive([Frame(queue=1, arrive_ns=0, size_bytes=5)],
                              s, cfg(), max_events=1)


class TestInvalidConfig(unittest.TestCase):
    def good(self):
        return sched([GateWindow(0, 100, {0})])

    def test_bad_fragment_size(self):
        with self.assertRaisesRegex(TSNGateError, "fragment_size_bytes"):
            PreemptionConfig(express_queues=frozenset(), fragment_size_bytes=0)

    def test_bad_overhead(self):
        with self.assertRaisesRegex(TSNGateError, "fragment_overhead_bytes"):
            PreemptionConfig(express_queues=frozenset(), fragment_size_bytes=10,
                             fragment_overhead_bytes=-1)

    def test_bad_express_queue(self):
        with self.assertRaisesRegex(TSNGateError, "express_queues"):
            PreemptionConfig(express_queues=frozenset({4}), fragment_size_bytes=10)
        with self.assertRaisesRegex(TSNGateError, "express_queues"):
            PreemptionConfig(express_queues=frozenset({True}), fragment_size_bytes=10)

    def test_config_type_checked(self):
        with self.assertRaisesRegex(TSNGateError, "config"):
            replay_preemptive([Frame(queue=0, arrive_ns=0, size_bytes=1)],
                              self.good(), object())

    def test_bad_frame_still_located(self):
        with self.assertRaisesRegex(TSNGateError, "size_bytes"):
            replay_preemptive([Frame(queue=0, arrive_ns=0, size_bytes=0)],
                              self.good(), cfg())


class TestAgainstByteReference(unittest.TestCase):
    """确定性随机小整数用例:事件驱动核心 vs 独立逐字节参考 + 占用核验。"""

    def test_fuzz_preemptive(self):
        rng = random.Random(20260926)
        checked = 0
        for case in range(400):
            cycle = rng.randint(20, 80)
            windows = []
            for _ in range(rng.randint(1, 3)):
                start = rng.randint(0, cycle - 2)
                end = rng.randint(start + 1, cycle)
                qs = frozenset(rng.sample(range(3), rng.randint(1, 3)))
                windows.append((start, end, qs))
            ok = True
            for q in range(3):
                segs = sorted((s, e) for (s, e, qs) in windows if q in qs)
                for (s1, e1), (s2, _e2) in zip(segs, segs[1:]):
                    if s2 < e1:
                        ok = False
            if not ok:
                continue
            checked += 1
            rate = rng.randint(1, 4)
            guard = rng.randint(0, 4)
            offset = rng.randint(-8, 8)
            frag = rng.randint(1, 5)
            ov = rng.randint(0, 3)
            express = frozenset(rng.sample(range(3), rng.randint(0, 2)))
            frames = [
                (rng.randint(0, 2), rng.randint(0, 2 * cycle), rng.randint(1, 12), None)
                for _ in range(rng.randint(1, 7))
            ]
            schedule = GateSchedule(
                cycle_ns=cycle,
                windows=tuple(GateWindow(s, e, qs) for (s, e, qs) in windows),
                link_rate_bits_per_ns=rate,
                guard_ns=guard,
                clock_offset_ns=offset,
            )
            c = PreemptionConfig(express_queues=express,
                                 fragment_size_bytes=frag,
                                 fragment_overhead_bytes=ov)
            arrivals = [Frame(queue=q, arrive_ns=a, size_bytes=z)
                        for (q, a, z, _d) in frames]
            expected, _occ = reference_preemptive(
                frames, windows, cycle, rate, guard, offset,
                set(express), frag, ov)
            result = replay_preemptive(arrivals, schedule, c)
            for i, (exp, got) in enumerate(zip(expected, result.frames)):
                e_first, e_last, e_frags = exp
                if e_first is None:
                    self.assertEqual(got.status, FrameStatus.NEVER_TRANSMITTABLE,
                                     f"case {case} frame {i}")
                else:
                    self.assertEqual((got.start_ns, got.end_ns), (e_first, e_last),
                                     f"case {case} frame {i}")
                    self.assertEqual(list(got.fragments), list(e_frags),
                                     f"case {case} frame {i} 片段时间线不一致")
            occ_check(result, frames, windows, cycle, rate, guard, offset, c)
        self.assertGreater(checked, 200)


if __name__ == "__main__":
    unittest.main()
