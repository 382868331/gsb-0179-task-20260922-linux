"""信用整形(CreditShaper)测试:定点用例 + 与独立细时间格参考的随机对照。"""

import random
import unittest
from fractions import Fraction

from tsn_gate import (
    BudgetExhaustedError,
    CreditShaper,
    Frame,
    FrameStatus,
    GateSchedule,
    GateWindow,
    TSNGateError,
    WaitReason,
    replay,
)
from reference import reference_replay_credit


def shaped_sched(windows, shapers, cycle=100, rate=8, guard=0, offset=0):
    # rate=8 bit/ns => 1 字节恰好 1ns,便于手算期望值
    return GateSchedule(
        cycle_ns=cycle,
        windows=tuple(windows),
        link_rate_bits_per_ns=rate,
        guard_ns=guard,
        clock_offset_ns=offset,
        credit_shapers=shapers,
    )


class TestShaperValidation(unittest.TestCase):
    def test_slopes_must_be_one_positive_one_negative(self):
        with self.assertRaisesRegex(TSNGateError, "idle_slope"):
            CreditShaper(idle_slope=0, send_slope=-1, hi_credit=5, lo_credit=-5)
        with self.assertRaisesRegex(TSNGateError, "idle_slope"):
            CreditShaper(idle_slope=-1, send_slope=-1, hi_credit=5, lo_credit=-5)
        with self.assertRaisesRegex(TSNGateError, "send_slope"):
            CreditShaper(idle_slope=1, send_slope=0, hi_credit=5, lo_credit=-5)
        with self.assertRaisesRegex(TSNGateError, "send_slope"):
            CreditShaper(idle_slope=1, send_slope=2, hi_credit=5, lo_credit=-5)

    def test_bool_and_float_slopes_rejected(self):
        with self.assertRaisesRegex(TSNGateError, "idle_slope"):
            CreditShaper(idle_slope=True, send_slope=-1, hi_credit=5, lo_credit=-5)
        with self.assertRaisesRegex(TSNGateError, "send_slope"):
            CreditShaper(idle_slope=1, send_slope=-1.0, hi_credit=5, lo_credit=-5)

    def test_credit_bounds(self):
        with self.assertRaisesRegex(TSNGateError, "hi_credit"):
            CreditShaper(idle_slope=1, send_slope=-1, hi_credit=-1, lo_credit=-5)
        with self.assertRaisesRegex(TSNGateError, "lo_credit"):
            CreditShaper(idle_slope=1, send_slope=-1, hi_credit=5, lo_credit=1)
        # 边界值 0 合法
        CreditShaper(idle_slope=1, send_slope=-1, hi_credit=0, lo_credit=0)

    def test_schedule_shaper_mapping_validated(self):
        win = (GateWindow(0, 10, {0}),)
        with self.assertRaisesRegex(TSNGateError, "credit_shapers"):
            GateSchedule(cycle_ns=10, windows=win, link_rate_bits_per_ns=8,
                         credit_shapers=[1, 2])
        with self.assertRaisesRegex(TSNGateError, "credit_shapers"):
            GateSchedule(cycle_ns=10, windows=win, link_rate_bits_per_ns=8,
                         credit_shapers={4: CreditShaper(1, -1, 0, 0)})
        with self.assertRaisesRegex(TSNGateError, "credit_shapers"):
            GateSchedule(cycle_ns=10, windows=win, link_rate_bits_per_ns=8,
                         credit_shapers={0: "not-a-shaper"})

    def test_no_shapers_keeps_old_behavior(self):
        # credit_shapers=None(默认)与空 dict 不同:None 走旧引擎,输出纯 int
        s = GateSchedule(cycle_ns=100, windows=(GateWindow(0, 50, {0}),),
                         link_rate_bits_per_ns=8)
        r = replay([Frame(queue=0, arrive_ns=0, size_bytes=2)], s)
        self.assertIs(type(r.frames[0].start_ns), int)
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (0, 2))


class TestCreditPacing(unittest.TestCase):
    def test_send_then_wait_for_credit(self):
        # 门常开;idle=1, send=-2。A 发 4ns -> 信用 -8;B 等到 t=12;C 等到 15
        s = shaped_sched([GateWindow(0, 100, {0})],
                         {0: CreditShaper(1, -2, 10, -20)})
        r = replay([Frame(0, 0, 4), Frame(0, 4, 1), Frame(0, 5, 1)], s)
        starts = [f.start_ns for f in r.frames]
        self.assertEqual(starts, [0, 12, 15])
        self.assertIn(WaitReason.CREDIT_NEGATIVE, r.frames[1].wait_reasons)

    def test_same_moment_arrival_at_credit_zero(self):
        # B 恰在信用恢复到 0 的时刻到达:信用非负即可开始
        s = shaped_sched([GateWindow(0, 100, {0})],
                         {0: CreditShaper(1, -2, 10, -20)})
        r = replay([Frame(0, 0, 4), Frame(0, 12, 1)], s)
        self.assertEqual(r.frames[1].start_ns, 12)

    def test_exact_fractional_recovery_time(self):
        # idle=3, send=-1:A 发 1ns -> 信用 -1;恢复到 0 需 1/3 ns
        s = shaped_sched([GateWindow(0, 100, {0})],
                         {0: CreditShaper(3, -1, 10, -20)})
        r = replay([Frame(0, 0, 1), Frame(0, 0, 1)], s)
        self.assertEqual(r.frames[1].start_ns, Fraction(4, 3))
        self.assertEqual(r.frames[1].end_ns, Fraction(7, 3))
        self.assertEqual(r.frames[1].waited_ns, Fraction(4, 3))
        # 与细时间格参考一致(量子 1/3,所有事件落在格上)
        exp = reference_replay_credit(
            [(0, 0, 1, None), (0, 0, 1, None)],
            [(0, 100, frozenset({0}))],
            cycle=100, rate=8, guard=0, offset=0,
            shapers={0: (3, -1, 10, -20)}, quantum=Fraction(1, 3),
        )
        self.assertEqual(Fraction(r.frames[1].start_ns), exp[1][0])

    def test_negative_credit_frozen_while_gate_closed(self):
        # 窗口 [0,10) 周期 20;A 发 6ns -> 信用 -12;[6,10) 积压恢复到 -8;
        # 关门 [10,20) 冻结(本题规定);开门后恢复 8ns -> B 在 t=28 开始
        s = shaped_sched([GateWindow(0, 10, {0})],
                         {0: CreditShaper(1, -2, 10, -30)}, cycle=20)
        r = replay([Frame(0, 0, 6), Frame(0, 5, 1)], s)
        self.assertEqual(r.frames[1].start_ns, 28)
        self.assertIn(WaitReason.GATE_CLOSED, r.frames[1].wait_reasons)
        self.assertIn(WaitReason.CREDIT_NEGATIVE, r.frames[1].wait_reasons)

    def test_empty_queue_positive_credit_resets(self):
        # q1 帧占链路 [0,10);q0 的 A 在 t=1 到达,积压期间信用涨到 hi=5;
        # A 发 [10,12) 后信用 5-4=1,队列变空 -> 正信用归 0;
        # C 发 [13,16) 后信用 0-6=-6(若未归 0 会是 -5);
        # D 需等 6ns 恢复 -> t=22(若未归 0 会是 t=21)
        s = shaped_sched([GateWindow(0, 100, {0, 1})],
                         {0: CreditShaper(1, -2, 5, -20)})
        arrivals = [
            Frame(1, 0, 10),   # q1,占链路 [0,10)
            Frame(0, 1, 2),    # A
            Frame(0, 13, 3),   # C(在队列变空、正信用归 0 之后到达)
            Frame(0, 16, 1),   # D
        ]
        r = replay(arrivals, s)
        self.assertEqual(r.frames[1].start_ns, 10)
        self.assertEqual(r.frames[2].start_ns, 13)
        self.assertEqual(r.frames[3].start_ns, 22)

    def test_hi_credit_cap(self):
        # hi=3:积压 9ns 信用也只到 3;A 发 2ns 后信用 3-4=-1,B 等 1ns
        s = shaped_sched([GateWindow(0, 100, {0, 1})],
                         {0: CreditShaper(1, -2, 3, -20)})
        arrivals = [Frame(1, 0, 10), Frame(0, 1, 2), Frame(0, 12, 1)]
        r = replay(arrivals, s)
        self.assertEqual(r.frames[1].start_ns, 10)
        self.assertEqual(r.frames[2].start_ns, 13)  # 12 + 1ns 恢复

    def test_lo_credit_floor(self):
        # lo=-5, send=-10:A 发 4ns 信用被夹到 -5(而非 -40);idle=5 -> B 等 1ns
        s = shaped_sched([GateWindow(0, 100, {0})],
                         {0: CreditShaper(5, -10, 10, -5)})
        r = replay([Frame(0, 0, 4), Frame(0, 4, 1)], s)
        self.assertEqual(r.frames[1].start_ns, 5)

    def test_unshaped_queue_unaffected_in_mixed_schedule(self):
        # 同一门表下,未整形队列的发送时刻与无整形模式完全一致
        windows = [GateWindow(0, 50, {0}), GateWindow(50, 100, {1})]
        arrivals = [Frame(1, 0, 3), Frame(0, 0, 3), Frame(1, 60, 2), Frame(0, 10, 2)]
        shaped = shaped_sched(windows, {0: CreditShaper(4, -1, 10, -10)})
        plain = GateSchedule(cycle_ns=100, windows=tuple(windows),
                             link_rate_bits_per_ns=8)
        rs = replay(arrivals, shaped)
        rp = replay(arrivals, plain)
        for i in (0, 2):  # 队列 1 未整形
            self.assertEqual(rs.frames[i].start_ns, rp.frames[i].start_ns)
            self.assertEqual(rs.frames[i].end_ns, rp.frames[i].end_ns)

    def test_never_transmittable_still_proven_with_shaper(self):
        s = shaped_sched([GateWindow(0, 10, {0})],
                         {0: CreditShaper(1, -1, 5, -5)})
        r = replay([Frame(0, 0, 11), Frame(0, 0, 1)], s)
        self.assertEqual(r.frames[0].status, FrameStatus.NEVER_TRANSMITTABLE)
        self.assertEqual(r.frames[1].status, FrameStatus.SENT)

    def test_budget_exhausted_distinct_in_shaped_mode(self):
        s = shaped_sched([GateWindow(10, 20, {0})],
                         {0: CreditShaper(1, -1, 5, -5)})
        with self.assertRaises(BudgetExhaustedError):
            replay([Frame(0, 0, 1)], s, max_events=1)
        r = replay([Frame(0, 0, 1)], s)
        self.assertEqual(r.frames[0].status, FrameStatus.SENT)


class TestAgainstGridReference(unittest.TestCase):
    """确定性随机小整数用例:事件跳跃核心 vs 独立细时间格参考。"""

    def _run_fuzz(self, seed, cases, slope_choices, quantum):
        rng = random.Random(seed)
        checked = 0
        for case in range(cases):
            cycle = rng.randint(10, 60)
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
            rate = rng.randint(1, 4)
            guard = rng.randint(0, 5)
            offset = rng.randint(-10, 10)
            shapers = {}
            for q in range(3):
                if rng.random() < 0.6:
                    idle = rng.choice(slope_choices)
                    send = -rng.choice(slope_choices)
                    hi = rng.randint(0, 12)
                    lo = -rng.randint(0, 12)
                    shapers[q] = (idle, send, hi, lo)
            if not shapers:
                continue
            frames = []
            for _ in range(rng.randint(1, 6)):
                frames.append((
                    rng.randint(0, 2),
                    rng.randint(0, cycle),
                    rng.randint(1, 4),
                    None,
                ))
            schedule = GateSchedule(
                cycle_ns=cycle,
                windows=tuple(GateWindow(s, e, qs) for (s, e, qs) in windows),
                link_rate_bits_per_ns=rate,
                guard_ns=guard,
                clock_offset_ns=offset,
                credit_shapers={q: CreditShaper(*p) for q, p in shapers.items()},
            )
            arrivals = [Frame(queue=q, arrive_ns=a, size_bytes=z, deadline_ns=d)
                        for (q, a, z, d) in frames]
            expected = reference_replay_credit(
                frames, windows, cycle, rate, guard, offset, shapers,
                quantum=quantum,
            )
            result = replay(arrivals, schedule)
            for i, (exp, got) in enumerate(zip(expected, result.frames)):
                if exp == (None, None):
                    self.assertEqual(
                        got.status, FrameStatus.NEVER_TRANSMITTABLE,
                        f"case {case} frame {i}: 参考判定永不可发,核心却发送",
                    )
                else:
                    self.assertIsNotNone(got.start_ns,
                                         f"case {case} frame {i}: 核心未发送")
                    self.assertEqual(
                        (Fraction(got.start_ns), Fraction(got.end_ns)), exp,
                        f"case {case} frame {i}: 核心与细时间格参考不一致",
                    )
            checked += 1
        self.assertGreaterEqual(checked, cases // 2, "有效用例太少,生成器可能有问题")

    def test_fuzz_power_of_two_slopes(self):
        # 斜率整除 4 -> 量子 1/4 即可让所有信用过零时刻落在格上
        self._run_fuzz(seed=20260926, cases=120, slope_choices=(1, 2, 4),
                       quantum=Fraction(1, 4))

    def test_fuzz_thirds_slopes(self):
        # 斜率含 3 -> 量子 1/12,覆盖 1/3、1/6 等分数恢复时刻
        self._run_fuzz(seed=20260927, cases=40, slope_choices=(1, 2, 3, 4, 6, 12),
                       quantum=Fraction(1, 12))


if __name__ == "__main__":
    unittest.main()
