"""帧抢占(express/preemptable)单元测试:定点用例 + 与逐字节参照的随机对照。"""

import random
import unittest

from tsn_gate import (
    Frame,
    FrameStatus,
    GateSchedule,
    GateWindow,
    PreemptionConfig,
    TrafficClass,
    TSNGateError,
    WaitReason,
    replay,
)
from reference_preempt import reference_preempt_replay, verify_occupancy

EXP = TrafficClass.EXPRESS
PRE = TrafficClass.PREEMPTABLE


def psched(windows, cycle=1000, rate=8, guard=0, offset=0, frag=20, ovh=0):
    # rate=8 bit/ns => 1 字节恰好 1ns,便于手算期望值
    return GateSchedule(
        cycle_ns=cycle,
        windows=tuple(windows),
        link_rate_bits_per_ns=rate,
        guard_ns=guard,
        clock_offset_ns=offset,
        preemption=PreemptionConfig(frag, ovh),
    )


class TestExpressPreemptsAtBoundary(unittest.TestCase):
    def test_express_waits_for_fragment_boundary(self):
        # 普通帧 100B 按 (20B 载荷 + 4B 开销) 片段发送,每片 24ns;
        # 紧急帧 55ns 到达,须等到片段边界 72ns 才能发送
        s = psched([GateWindow(0, 600, {0})], frag=20, ovh=4)
        r = replay([
            Frame(queue=0, arrive_ns=0, size_bytes=100),                    # seq0
            Frame(queue=0, arrive_ns=55, size_bytes=10, traffic_class=EXP),  # seq1
        ], s)
        a, e = r.frames
        self.assertEqual(a.fragments, ((0, 24), (24, 48), (48, 72), (82, 106), (106, 130)))
        self.assertEqual(e.fragments, ((72, 82),))
        self.assertEqual((e.start_ns, e.end_ns), (72, 82))
        self.assertEqual(a.end_ns, 130)
        self.assertEqual(a.preempted_count, 1)
        self.assertIn(WaitReason.PREEMPTED, a.wait_reasons)

    def test_fragment_overhead_accounting(self):
        s = psched([GateWindow(0, 600, {0})], frag=20, ovh=4)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=100)], s)
        f = r.frames[0]
        # 5 个片段 * 24ns = 120ns 线上时长;原始序列化 100ns;开销 20ns
        self.assertEqual(sum(e - st for st, e in f.fragments), 120)
        self.assertEqual(f.duration_ns, 100)
        self.assertEqual(f.overhead_ns, 20)
        self.assertEqual(f.end_ns, 120)

    def test_arrival_exactly_at_fragment_completion(self):
        # 同刻次序:片段完成 -> 门切换 -> 帧到达 -> 决策;
        # 紧急帧恰在片段完成时刻到达,可立即在该时刻开始
        s = psched([GateWindow(0, 600, {0})], frag=20, ovh=4)
        r = replay([
            Frame(queue=0, arrive_ns=0, size_bytes=100),
            Frame(queue=0, arrive_ns=72, size_bytes=10, traffic_class=EXP),
        ], s)
        self.assertEqual(r.frames[1].start_ns, 72)
        self.assertEqual(r.frames[1].waited_ns, 0)

    def test_preemption_disabled_keeps_old_result(self):
        # 不启用抢占:与上一迭代完全一致(普通帧原子发送,紧急帧排在后面)
        arrivals = [
            Frame(queue=0, arrive_ns=0, size_bytes=100),
            Frame(queue=0, arrive_ns=55, size_bytes=10, traffic_class=EXP),
        ]
        s_old = GateSchedule(cycle_ns=1000,
                             windows=(GateWindow(0, 600, {0}),),
                             link_rate_bits_per_ns=8)
        r = replay(arrivals, s_old)
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (0, 100))
        self.assertEqual((r.frames[1].start_ns, r.frames[1].end_ns), (100, 110))
        self.assertEqual(r.frames[1].fragments, ())


class TestGateCloseDuringFrame(unittest.TestCase):
    def test_gate_close_no_new_fragment_until_next_window(self):
        # 窗口 [0,50):片段 [20,40) 正常完成;40ns 时剩余窗口 10ns 容不下
        # 20ns 片段,不能再开新片段,等到下一周期 [1000,1050) 续传
        s = psched([GateWindow(0, 50, {0})], frag=20)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=60)], s)
        f = r.frames[0]
        self.assertEqual(f.fragments, ((0, 20), (20, 40), (1000, 1020)))
        self.assertIn(WaitReason.INSUFFICIENT_WINDOW, f.wait_reasons)
        self.assertIn(WaitReason.GATE_CLOSED, f.wait_reasons)

    def test_fragment_may_end_exactly_at_window_end(self):
        # 片段结束恰好顶到窗口终点:允许(半开区间)
        s = psched([GateWindow(0, 40, {0})], frag=20)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=40)], s)
        self.assertEqual(r.frames[0].fragments, ((0, 20), (20, 40)))

    def test_long_frame_spanning_many_windows(self):
        # 帧长于任何窗口:抢占模式下按片段跨周期续传,不再判永不可发
        s = psched([GateWindow(0, 10, {0})], cycle=100, frag=5)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=25)], s)
        f = r.frames[0]
        self.assertEqual(f.status, FrameStatus.SENT)
        self.assertEqual(f.fragments,
                         ((0, 5), (5, 10), (100, 105), (105, 110), (200, 205)))


class TestExpressGate(unittest.TestCase):
    def test_express_must_satisfy_own_gate(self):
        # 紧急帧自己的门未开时不能抢占,普通帧继续发送
        s = psched([GateWindow(0, 1000, {0}), GateWindow(100, 200, {1})], frag=20)
        r = replay([
            Frame(queue=0, arrive_ns=0, size_bytes=60),                    # seq0
            Frame(queue=1, arrive_ns=5, size_bytes=10, traffic_class=EXP),  # seq1
        ], s)
        a, e = r.frames
        self.assertEqual(a.fragments, ((0, 20), (20, 40), (40, 60)))
        self.assertEqual((e.start_ns, e.end_ns), (100, 110))
        self.assertIn(WaitReason.GATE_CLOSED, e.wait_reasons)
        self.assertEqual(a.preempted_count, 0)


class TestResumeOrder(unittest.TestCase):
    def test_paused_frames_resume_in_original_start_order(self):
        # A(q0) 被门暂停,B(q1) 被紧急帧暂停;108ns 时两者都可恢复,
        # 原开始时刻更早的 A 先恢复
        s = psched([GateWindow(0, 40, {0}), GateWindow(100, 1000, {0}),
                    GateWindow(40, 1000, {1})], frag=20)
        r = replay([
            Frame(queue=0, arrive_ns=0, size_bytes=60),                     # seq0 A
            Frame(queue=1, arrive_ns=5, size_bytes=120),                    # seq1 B
            Frame(queue=1, arrive_ns=45, size_bytes=8, traffic_class=EXP),  # seq2 E
        ], s)
        a, b, e = r.frames
        self.assertEqual(a.fragments, ((0, 20), (20, 40), (108, 128)))
        self.assertEqual(b.fragments[:3], ((40, 60), (68, 88), (88, 108)))
        self.assertEqual(b.fragments[3], (128, 148))
        self.assertEqual(b.end_ns, 188)
        self.assertEqual(e.fragments, ((60, 68),))


class TestNeverTransmittablePreemptive(unittest.TestCase):
    def test_express_too_long_never(self):
        s = psched([GateWindow(0, 10, {0})], cycle=100, guard=3)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=8, traffic_class=EXP)], s)
        self.assertEqual(r.frames[0].status, FrameStatus.NEVER_TRANSMITTABLE)

    def test_fragment_overhead_makes_frame_never(self):
        # 最小片段线上时长 = (1B 载荷 + 20B 开销) = 21ns > 窗口 10ns
        s = psched([GateWindow(0, 10, {0})], cycle=100, frag=20, ovh=20)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], s)
        self.assertEqual(r.frames[0].status, FrameStatus.NEVER_TRANSMITTABLE)

    def test_never_does_not_block_others(self):
        s = psched([GateWindow(0, 10, {0})], cycle=100, frag=20, ovh=20)
        r = replay([
            Frame(queue=0, arrive_ns=0, size_bytes=1),  # 永不可发
            Frame(queue=0, arrive_ns=0, size_bytes=1, traffic_class=EXP),  # 8<=10 可发
        ], s)
        self.assertEqual(r.frames[0].status, FrameStatus.NEVER_TRANSMITTABLE)
        self.assertEqual(r.frames[1].status, FrameStatus.SENT)


class TestInvalidPreemptionInput(unittest.TestCase):
    def test_bad_fragment_bytes(self):
        with self.assertRaisesRegex(TSNGateError, "fragment_bytes"):
            PreemptionConfig(0)
        with self.assertRaisesRegex(TSNGateError, "fragment_bytes"):
            PreemptionConfig(True)
        with self.assertRaisesRegex(TSNGateError, "fragment_bytes"):
            PreemptionConfig(1.5)

    def test_bad_overhead(self):
        with self.assertRaisesRegex(TSNGateError, "fragment_overhead_bytes"):
            PreemptionConfig(10, -1)

    def test_bad_preemption_type_on_schedule(self):
        with self.assertRaisesRegex(TSNGateError, "preemption"):
            GateSchedule(cycle_ns=100, windows=(GateWindow(0, 10, {0}),),
                         link_rate_bits_per_ns=8, preemption="x")

    def test_bad_traffic_class(self):
        s = psched([GateWindow(0, 10, {0})])
        with self.assertRaisesRegex(TSNGateError, r"arrivals\[0\]\.traffic_class"):
            replay([Frame(queue=0, arrive_ns=0, size_bytes=1,
                          traffic_class="express")], s)


class TestAgainstByteReference(unittest.TestCase):
    """确定性随机小整数用例:事件驱动核心 vs 逐字节参照 + 链路占用核对。"""

    def test_fuzz_preemption(self):
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
            rate = rng.choice([1, 2, 4, 8])
            guard = rng.randint(0, 4)
            offset = rng.randint(-10, 10)
            frag = rng.randint(1, 6)
            ovh = rng.randint(0, 3)
            frames = []
            for _ in range(rng.randint(1, 7)):
                frames.append((
                    rng.randint(0, 2),
                    rng.randint(0, 3 * cycle),
                    rng.randint(1, 9),
                    rng.random() < 0.35,
                ))
            schedule = GateSchedule(
                cycle_ns=cycle,
                windows=tuple(GateWindow(s, e, qs) for (s, e, qs) in windows),
                link_rate_bits_per_ns=rate,
                guard_ns=guard,
                clock_offset_ns=offset,
                preemption=PreemptionConfig(frag, ovh),
            )
            arrivals = [Frame(queue=q, arrive_ns=a, size_bytes=z,
                              traffic_class=EXP if is_exp else PRE)
                        for (q, a, z, is_exp) in frames]
            expected = reference_preempt_replay(frames, windows, cycle, rate,
                                                guard, offset, frag, ovh)
            result = replay(arrivals, schedule)
            for i, (exp, got) in enumerate(zip(expected, result.frames)):
                if exp is None:
                    self.assertEqual(got.status, FrameStatus.NEVER_TRANSMITTABLE,
                                     f"case {case} frame {i}")
                else:
                    self.assertEqual(got.fragments, exp, f"case {case} frame {i}")
            # 链路占用核对:无重叠、无重复字节、无凭空空闲(仅统计可发帧)
            v_frames, v_frags = [], []
            for i, exp in enumerate(expected):
                if exp is not None:
                    v_frames.append(frames[i])
                    v_frags.append(result.frames[i].fragments)
            verify_occupancy(v_frags, v_frames, windows, cycle, rate, guard,
                             offset, frag, ovh)
            checked += 1
        self.assertGreater(checked, 200)


if __name__ == "__main__":
    unittest.main()
