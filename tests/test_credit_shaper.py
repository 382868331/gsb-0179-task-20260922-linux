"""信用整形(CreditShaper)单元测试:定点用例 + 细时间格参考随机对照。"""

import random
import unittest
from fractions import Fraction

from tsn_gate import (
    CreditShaper,
    Frame,
    FrameStatus,
    GateSchedule,
    GateWindow,
    TSNGateError,
    WaitReason,
    replay,
)
from reference import reference_replay_shaped


def sched(windows, cycle=100, rate=8, guard=0, offset=0):
    return GateSchedule(
        cycle_ns=cycle,
        windows=tuple(windows),
        link_rate_bits_per_ns=rate,
        guard_ns=guard,
        clock_offset_ns=offset,
    )


class TestCreditBasics(unittest.TestCase):
    def test_send_makes_credit_negative_and_delays_next_frame(self):
        # 恒开门;idle=1/ns,send=-3/ns。10B 帧 10ns 发完 -> 信用 -30,
        # 恢复 30ns 后第二帧才能开始(无整形时 10ns 即可连发)。
        s = sched([GateWindow(0, 100, {0})])
        sh = (CreditShaper(queue=0, idle_slope=1, send_slope=-3),)
        r = replay(
            [Frame(0, 0, 10), Frame(0, 0, 1)],
            s, shapers=sh,
        )
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (0, 10))
        self.assertEqual((r.frames[1].start_ns, r.frames[1].end_ns), (40, 41))
        self.assertIn(WaitReason.CREDIT_NEGATIVE, r.frames[1].wait_reasons)

        plain = replay([Frame(0, 0, 10), Frame(0, 0, 1)], s)
        self.assertEqual(plain.frames[1].start_ns, 10)  # 旧无整形模式

    def test_fractional_credit_zero_time(self):
        # send=-2/ns,5ns 发送 -> -10;idle=5/ns -> 10/5 = 2ns 后归零,精确分数
        s = sched([GateWindow(0, 100, {0})])
        sh = (CreditShaper(queue=0, idle_slope=5, send_slope=-2,
                           lo_credit=-100),)
        r = replay([Frame(0, 0, 5), Frame(0, 0, 1)], s, shapers=sh)
        self.assertEqual(r.frames[1].start_ns, 7)  # 5 + 2

        # 非整除归零时刻:lo=-6,10ns 发送信用截到 -6,恢复 6/5 ns
        sh2 = (CreditShaper(queue=0, idle_slope=5, send_slope=-2,
                            lo_credit=-6),)
        r2 = replay([Frame(0, 0, 10), Frame(0, 0, 1)], s, shapers=sh2)
        self.assertEqual(r2.frames[1].start_ns, Fraction(56, 5))  # 10 + 6/5
        self.assertEqual(r2.frames[1].end_ns, Fraction(61, 5))
        self.assertEqual(r2.frames[1].waited_ns, Fraction(56, 5))


class TestGateClosesFreezesCredit(unittest.TestCase):
    def test_negative_credit_frozen_during_closed_gate(self):
        # 窗 [0,50),周期 100。f0 40ns -> 信用 -40;开门内只能恢复 10ns
        # (到 50 为 -30);关门 [50,100) 冻结;重开后恢复 30ns -> f1 130 开始
        s = sched([GateWindow(0, 50, {0})])
        sh = (CreditShaper(queue=0, idle_slope=1, send_slope=-1),)
        r = replay([Frame(0, 0, 40), Frame(0, 0, 1)], s, shapers=sh)
        self.assertEqual((r.frames[0].start_ns, r.frames[0].end_ns), (0, 40))
        self.assertEqual(r.frames[1].start_ns, 130)
        self.assertEqual(r.frames[1].end_ns, 131)
        self.assertIn(WaitReason.GATE_CLOSED, r.frames[1].wait_reasons)
        self.assertIn(WaitReason.CREDIT_NEGATIVE, r.frames[1].wait_reasons)

        # 对照:无整形时 f1 在 40 发出,证明延迟确为信用恢复所致
        plain = replay([Frame(0, 0, 40), Frame(0, 0, 1)], s)
        self.assertEqual(plain.frames[1].start_ns, 40)


class TestEmptyQueue(unittest.TestCase):
    def test_positive_credit_reset_to_zero_when_empty(self):
        # q0 占链路 10ns,q1(整形)积压期间信用 idle=10 累积到 100;
        # q1 发 1B(send=-1)后信用 99 且队列空 -> 立即归 0;
        # 同刻(20)到达两帧:第一帧 20 发送后信用 -1,第二帧须等到 22
        s = sched([GateWindow(0, 100, {0, 1})])
        sh = (CreditShaper(queue=1, idle_slope=10, send_slope=-1),)
        arrivals = [
            Frame(0, 0, 10),
            Frame(1, 0, 1),
            Frame(1, 20, 1),
            Frame(1, 20, 1),
        ]
        r = replay(arrivals, s, shapers=sh)
        self.assertEqual((r.frames[1].start_ns, r.frames[1].end_ns), (10, 11))
        self.assertEqual(r.frames[2].start_ns, 20)
        # idle=10/ns:信用 -1 只需 1/10 ns 恢复;若空队列未清零(信用留 99)
        # 则 f3 会在 21 立即开始,这里精确证明清零发生过
        self.assertEqual(r.frames[3].start_ns, Fraction(211, 10))

    def test_negative_credit_recovers_while_empty_then_new_frame_starts_at_zero(self):
        # f0 发完信用 -10;空队列恢复到 0(20 时刻);f1 恰在 20 到达即发
        s = sched([GateWindow(0, 100, {0})])
        sh = (CreditShaper(queue=0, idle_slope=1, send_slope=-1),)
        r = replay([Frame(0, 0, 10), Frame(0, 20, 1)], s, shapers=sh)
        self.assertEqual(r.frames[1].start_ns, 20)
        self.assertNotIn(WaitReason.CREDIT_NEGATIVE, r.frames[1].wait_reasons)

    def test_negative_credit_recovery_and_zero_cap_while_empty(self):
        # f0 发完(10)信用 -10;空队列恢复,15 到达的 f1 须等到 20(归零时刻);
        # f1 发完(22)信用 -2;同刻到达的 f2 等到 24;
        # 30 到达的 f3 距上次归零已久,信用停在 0 不会“超额恢复”出正信用:
        # f3 发完(32)信用 -2,f4 等到 34 而非更早
        s = sched([GateWindow(0, 100, {0})])
        sh = (CreditShaper(queue=0, idle_slope=1, send_slope=-1),)
        arrivals = [
            Frame(0, 0, 10),
            Frame(0, 15, 2),
            Frame(0, 15, 2),
            Frame(0, 30, 2),
            Frame(0, 30, 2),
        ]
        r = replay(arrivals, s, shapers=sh)
        starts = [f.start_ns for f in r.frames]
        self.assertEqual(starts, [0, 20, 24, 30, 34])


class TestCreditBounds(unittest.TestCase):
    def test_hi_and_lo_credit_clamp(self):
        # hi=10:q1 积压 5ns * idle 5 = 25 截到 10;发 1B -> 8,空队列归 0
        # lo=-6:f2 发送 10ns 应到 -20,截到 -6;恢复 6/5 ns 后 f3 开始
        s = sched([GateWindow(0, 100, {0, 1})])
        sh = (CreditShaper(queue=1, idle_slope=5, send_slope=-2,
                           hi_credit=10, lo_credit=-6),)
        arrivals = [
            Frame(0, 0, 5),
            Frame(1, 0, 1),
            Frame(1, 10, 10),
            Frame(1, 10, 1),
        ]
        r = replay(arrivals, s, shapers=sh)
        self.assertEqual(r.frames[1].start_ns, 5)
        self.assertEqual(r.frames[2].start_ns, 10)
        self.assertEqual(r.frames[3].start_ns, Fraction(106, 5))  # 20 + 6/5


class TestShaperValidation(unittest.TestCase):
    def test_bad_shapers_rejected(self):
        s = sched([GateWindow(0, 50, {0})])
        cases = [
            ("idle 为负", (0, -1, -1), {}),
            ("send 为正", (0, 1, 1), {}),
            ("idle 为 bool", (0, True, -1), {}),
            ("send 为 float", (0, 1, -1.0), {}),
            ("hi 为负", (0, 1, -1), {"hi_credit": -1}),
            ("lo 为正", (0, 1, -1), {"lo_credit": 1}),
            ("队列越界", (4, 1, -1), {}),
        ]
        for label, args, kwargs in cases:
            with self.subTest(label=label):
                with self.assertRaises(TSNGateError):
                    replay([Frame(0, 0, 1)], s,
                           shapers=(CreditShaper(*args, **kwargs),))

    def test_duplicate_shaper_rejected(self):
        s = sched([GateWindow(0, 50, {0})])
        sh = (
            CreditShaper(0, 1, -1),
            CreditShaper(0, 2, -1),
        )
        with self.assertRaises(TSNGateError):
            replay([Frame(0, 0, 1)], s, shapers=sh)

    def test_wrong_type_rejected(self):
        s = sched([GateWindow(0, 50, {0})])
        with self.assertRaises(TSNGateError):
            replay([Frame(0, 0, 1)], s, shapers=((0, 1, -1),))


class TestCrossCheckReference(unittest.TestCase):
    def test_random_small_configs_match_fine_grid_reference(self):
        rng = random.Random(20260926)
        for case in range(400):
            cycle = rng.randint(6, 30)
            nq = rng.randint(1, 2)
            windows = []
            qwin_ref = {}
            for q in range(nq):
                length = rng.randint(2, cycle)
                start = rng.randint(0, cycle - length)
                windows.append(GateWindow(start, start + length, frozenset({q})))
                qwin_ref[q] = (start, start + length)
            rate = rng.choice([1, 2, 4, 8])
            guard = rng.randint(0, 3)
            offset = rng.randint(-4, 4)

            sh_tsn = []
            sh_ref = {}
            for q in range(nq):
                if rng.random() < 0.7:
                    idle = Fraction(rng.randint(1, 4), rng.choice([1, 1, 2]))
                    send = Fraction(-rng.randint(1, 4), rng.choice([1, 1, 2]))
                    hi = rng.choice([None, None, rng.randint(1, 8)])
                    lo = rng.choice([None, None, -rng.randint(1, 8)])
                    sh_tsn.append(CreditShaper(q, idle, send, hi, lo))
                    sh_ref[q] = (idle, send, hi, lo)

            nf = rng.randint(1, 7)
            frames = []
            for _ in range(nf):
                q = rng.randrange(nq)
                # 限制帧长,避免大量“永不可发”淹没对照
                max_len = max(e - s for s, e in qwin_ref.values())
                size = rng.randint(1, max(1, max_len * rate // 8))
                frames.append(Frame(q, rng.randint(0, cycle * 2), size))

            schedule = sched(windows, cycle=cycle, rate=rate,
                             guard=guard, offset=offset)
            ref = reference_replay_shaped(
                [(f.queue, f.arrive_ns, f.size_bytes, None) for f in frames],
                [(w.start_ns, w.end_ns, w.queues) for w in windows],
                cycle, rate, guard, offset, sh_ref,
            )
            got = replay(frames, schedule, shapers=tuple(sh_tsn))
            for i, f in enumerate(got.frames):
                rs, re_ = ref[i]
                if rs is None:
                    self.assertEqual(f.status, FrameStatus.NEVER_TRANSMITTABLE,
                                     f"case {case} seq{i}")
                    continue
                self.assertEqual(f.status, FrameStatus.SENT, f"case {case} seq{i}")
                self.assertEqual(
                    Fraction(f.start_ns), Fraction(rs),
                    f"case {case} seq{i} start 不一致; frames={frames},"
                    f" shapers={sh_ref}, cycle={cycle}, rate={rate},"
                    f" guard={guard}, offset={offset}",
                )
                self.assertEqual(
                    Fraction(f.end_ns), Fraction(re_),
                    f"case {case} seq{i} end 不一致",
                )


if __name__ == "__main__":
    unittest.main()
