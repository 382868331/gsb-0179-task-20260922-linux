"""tsn_synth 单元测试:手算定点用例 + 独立暴力排程参考的随机对照。

期望值全部手算或由 tests/reference_synth.py 的朴素暴力实现给出,
不调用被测合成器自身生成期望;replay 交叉校验只作为补充。
"""

import random
import unittest

from tsn_gate import BudgetExhaustedError, FrameStatus, TSNGateError
from tsn_synth import (
    DEFAULT_MAX_NODES,
    FlowSpec,
    InfeasibleScheduleError,
    Link,
    SynthesisBudgetExhaustedError,
    SynthesisStatus,
    replay_synthesis,
    synthesize,
    verify_synthesis,
)
from reference_synth import reference_feasible


def L(lid, rate=8, guard=0):
    return Link(link_id=lid, rate_bits_per_ns=rate, guard_ns=guard)


class TestHandComputed(unittest.TestCase):
    def test_single_hop_two_flows_phase_offsets(self):
        # 两条流共享链路 0:P=10, 1B@8 => d=1ns(无 guard)
        # 字典序首个可行解:s0=0(flow0),s=1(flow1)
        r = synthesize([
            FlowSpec(10, (L(0),), 10, 1, 10),
            FlowSpec(11, (L(0),), 10, 1, 10),
        ])
        self.assertEqual(r.status, SynthesisStatus.FEASIBLE)
        self.assertEqual(r.hyperperiod_ns, 10)
        offs = {p.flow_id: p.hops[0].start_offset_ns for p in r.flow_plans}
        self.assertEqual(offs, {10: 0, 11: 1})
        # 队列按输入下标分配
        self.assertEqual([p.queue for p in r.flow_plans], [0, 1])
        # 门表:两个邻接窗口
        gate = r.link_gates[0].gate
        self.assertEqual(
            sorted((w.start_ns, w.end_ns) for w in gate.windows),
            [(0, 1), (1, 2)],
        )
        # 超周期内每流 1 个实例
        self.assertEqual(len(r.transmissions), 2)

    def test_two_hop_store_forward_offsets(self):
        # 3B@8 => d=3ns 每跳;P=10, deadline=6
        # 最紧凑:s0=0 [0,3) -> 跳1 到达 3,s1=3 [3,6),恰好赶上 deadline
        r = synthesize([FlowSpec(7, (L(0), L(1)), 10, 3, 6)])
        p = r.flow_plans[0]
        self.assertEqual(
            [(h.link_id, h.start_offset_ns, h.end_offset_ns, h.duration_ns)
             for h in p.hops],
            [(0, 0, 3, 3), (1, 3, 6, 3)],
        )
        t0, t1 = sorted(r.transmissions, key=lambda x: x.hop_index)
        self.assertEqual((t0.release_ns, t0.arrive_ns, t0.start_ns, t0.end_ns),
                         (0, 0, 0, 3))
        self.assertEqual((t1.arrive_ns, t1.start_ns, t1.end_ns), (3, 3, 6))
        self.assertEqual(t1.deadline_ns, 6)

    def test_hyperperiod_repeats_instances(self):
        # P=4 与 P=6 -> H=12;单跳 d=1
        r = synthesize([
            FlowSpec(1, (L(0),), 4, 1, 4),
            FlowSpec(2, (L(0),), 6, 1, 6),
        ])
        self.assertEqual(r.hyperperiod_ns, 12)
        starts = {(tr.flow_id, tr.start_ns) for tr in r.transmissions}
        # flow1: 偏移 0 的实例 0,4,8;flow2 首个可行偏移 1(避开 [0,1)):1,7
        self.assertEqual({s for (f, s) in starts if f == 1}, {0, 4, 8})
        self.assertEqual({s for (f, s) in starts if f == 2}, {1, 7})
        # 全部区间两两不相交(半开,端点相接允许)
        iv = sorted((tr.start_ns, tr.end_ns) for tr in r.transmissions)
        for (s1, e1), (s2, e2) in zip(iv, iv[1:]):
            self.assertLessEqual(e1, s2)

    def test_link_specific_rate_changes_duration(self):
        # 3 字节:rate=8 -> d=3;rate=4 -> ceil(24/4)=6
        r = synthesize([FlowSpec(7, (L(0, rate=8), L(1, rate=4)), 10, 3, 9)])
        durs = [h.duration_ns for h in r.flow_plans[0].hops]
        self.assertEqual(durs, [3, 6])
        # s0=0 [0,3), s1=3 [3,9),deadline=9 恰好
        self.assertEqual(r.flow_plans[0].hops[1].end_offset_ns, 9)


class TestBoundaries(unittest.TestCase):
    def test_minimum_period_one(self):
        # P=1, 1B@8 => d=1, deadline=1:窗口 [0,1),end 恰好 = deadline/cycle
        r = synthesize([FlowSpec(0, (L(0),), 1, 1, 1)])
        w = r.link_gates[0].gate.windows[0]
        self.assertEqual((w.start_ns, w.end_ns), (0, 1))
        self.assertEqual(r.transmissions[0].end_ns, 1)

    def test_exact_deadline_met_vs_one_over(self):
        # 两跳各 d=3,makespan=6:deadline=6 可行;deadline=5 无解
        synthesize([FlowSpec(0, (L(0), L(1)), 10, 3, 6)])
        with self.assertRaises(InfeasibleScheduleError):
            synthesize([FlowSpec(0, (L(0), L(1)), 10, 3, 5)])

    def test_duration_plus_guard_exactly_period_allowed(self):
        # d=3, guard=1, P=4:占用恰好 4,偏移只能是 0,窗口顶到周期终点
        r = synthesize([FlowSpec(0, (L(0, guard=1),), 4, 3, 4)])
        gate = r.link_gates[0].gate
        self.assertEqual([(w.start_ns, w.end_ns) for w in gate.windows], [(0, 4)])
        reps = replay_synthesis(r)
        self.assertEqual(reps[0].result.sent_count, 1)

    def test_half_open_endpoint_touch_allowed(self):
        # 同周期两流 d=2, P=4:窗口 [0,2) 与 [2,4),端点相接不算冲突
        r = synthesize([
            FlowSpec(0, (L(0),), 4, 2, 4),
            FlowSpec(1, (L(0),), 4, 2, 4),
        ])
        wins = sorted((w.start_ns, w.end_ns) for w in r.link_gates[0].gate.windows)
        self.assertEqual(wins, [(0, 2), (2, 4)])

    def test_flow_revisits_same_link(self):
        # 路径 0->1->0:同一流在同周期的链路 0 上需要两个不重叠窗口
        # d=2 每跳, P=10: s0=0 [0,2), s1=2(在链路1)[2,4), s2=4(链路0)[4,6)
        r = synthesize([FlowSpec(0, (L(0), L(1), L(0)), 10, 2, 6)])
        p = r.flow_plans[0]
        self.assertEqual([h.start_offset_ns for h in p.hops], [0, 2, 4])
        link0_wins = sorted((w.start_ns, w.end_ns) for w in r.link_gates[0].gate.windows)
        self.assertEqual(link0_wins, [(0, 2), (4, 6)])


class TestInfeasible(unittest.TestCase):
    def test_makespan_exceeds_deadline(self):
        with self.assertRaisesRegex(InfeasibleScheduleError, "最小时长"):
            synthesize([FlowSpec(0, (L(0), L(1), L(2)), 10, 1, 2)])  # d=1*3=3 > 2

    def test_duration_guard_over_period(self):
        with self.assertRaisesRegex(InfeasibleScheduleError, "容不下"):
            synthesize([FlowSpec(0, (L(0, guard=5),), 4, 1, 4)])  # 1+5 > 4

    def test_link_utilization_over_one(self):
        with self.assertRaisesRegex(InfeasibleScheduleError, "占用率"):
            synthesize([
                FlowSpec(0, (L(0),), 4, 3, 4),
                FlowSpec(1, (L(0),), 4, 3, 4),
            ])  # (3+3)/4 > 1

    def test_phase_inseparable_but_under_full_load(self):
        # P=4 d=2 与 P=6 d=3:占用率 = 1/2 + 1/2 = 1,
        # 但任何相位都冲突(手算枚举 s∈[0,2],t∈[0,3] 全部相交)
        with self.assertRaises(InfeasibleScheduleError):
            synthesize([
                FlowSpec(0, (L(0),), 4, 2, 4),
                FlowSpec(1, (L(0),), 6, 3, 6),
            ])

    def test_structural_infeasible_holds_under_tiny_budget(self):
        # 搜索前的结构性证明(makespan > deadline)不消耗枚举预算:
        # 即使 max_nodes=1 也必须给出“无解”而非“预算不足”
        with self.assertRaises(InfeasibleScheduleError):
            synthesize(
                [FlowSpec(0, (L(0), L(1), L(2)), 10, 1, 2)],
                max_nodes=1,
            )

    def test_infeasible_is_value_error(self):
        # 与既有错误体系一致,可按 ValueError/TSNGateError 捕获
        with self.assertRaises(TSNGateError):
            synthesize([FlowSpec(0, (L(0), L(1)), 10, 3, 5)])


class TestBudget(unittest.TestCase):
    def test_budget_exhausted_distinct_from_infeasible(self):
        flows = [FlowSpec(0, (L(0),), 50, 1, 50)]  # 实际平凡可行
        with self.assertRaises(SynthesisBudgetExhaustedError):
            synthesize(flows, max_nodes=1)
        r = synthesize(flows)  # 默认预算下成功
        self.assertEqual(r.status, SynthesisStatus.FEASIBLE)

    def test_budget_error_inheritance(self):
        self.assertTrue(issubclass(SynthesisBudgetExhaustedError,
                                   BudgetExhaustedError))
        self.assertTrue(issubclass(SynthesisBudgetExhaustedError, TSNGateError))

    def test_bad_max_nodes(self):
        flows = [FlowSpec(0, (L(0),), 4, 1, 4)]
        with self.assertRaisesRegex(TSNGateError, "max_nodes"):
            synthesize(flows, max_nodes=0)
        with self.assertRaisesRegex(TSNGateError, "max_nodes"):
            synthesize(flows, max_nodes=True)
        self.assertGreaterEqual(DEFAULT_MAX_NODES, 1)


class TestInvalidInput(unittest.TestCase):
    def reject(self, flows, pat, **kw):
        with self.assertRaisesRegex(TSNGateError, pat):
            synthesize(flows, **kw)

    def test_empty_and_too_many_flows(self):
        self.reject([], "至少")
        self.reject([FlowSpec(i, (L(0),), 4, 1, 4) for i in range(5)], "上限 4")

    def test_path_hop_limits(self):
        self.reject([FlowSpec(0, (), 4, 1, 4)], "路径为空")
        self.reject([FlowSpec(0, (L(0), L(1), L(2), L(3)), 4, 1, 4)], "跳数")

    def test_scalar_ranges_and_types(self):
        good = (L(0),)
        self.reject([FlowSpec(True, good, 4, 1, 4)], "bool")
        self.reject([FlowSpec(0, good, 0, 1, 0)], "period_ns")
        self.reject([FlowSpec(0, good, 4, 0, 4)], "size_bytes")
        self.reject([FlowSpec(0, good, 4, -1, 4)], "size_bytes")
        self.reject([FlowSpec(0, good, 4, 1, 0)], "deadline_ns")
        self.reject([FlowSpec(0, good, 4, 1, 5)], "deadline_ns")
        self.reject([FlowSpec(0, good, 4.0, 1, 4)], "float")
        self.reject([FlowSpec(0, good, 4, 1, float("nan"))], "NaN")

    def test_element_types(self):
        self.reject([FlowSpec(0, (0,), 4, 1, 4)], "需要 Link")
        self.reject([object()], "需要 FlowSpec")

    def test_duplicate_flow_id(self):
        self.reject([
            FlowSpec(0, (L(0),), 4, 1, 4),
            FlowSpec(0, (L(1),), 4, 1, 4),
        ], "重复")

    def test_inconsistent_link_params(self):
        self.reject([
            FlowSpec(0, (L(0, rate=8), L(0, rate=4)), 4, 1, 4),
        ], "不一致")
        self.reject([
            FlowSpec(0, (L(0, guard=0), L(0, guard=2)), 4, 1, 4),
        ], "不一致")

    def test_link_validation(self):
        with self.assertRaisesRegex(TSNGateError, "Link.link_id"):
            Link(link_id=-1)
        with self.assertRaisesRegex(TSNGateError, "rate_bits_per_ns"):
            Link(link_id=0, rate_bits_per_ns=0)
        with self.assertRaisesRegex(TSNGateError, "guard_ns"):
            Link(link_id=0, guard_ns=-1)
        with self.assertRaisesRegex(TSNGateError, "bool"):
            Link(link_id=False)

    def test_hyperperiod_limit(self):
        self.reject([
            FlowSpec(0, (L(0),), 9973, 1, 9973),
            FlowSpec(1, (L(0),), 9967, 1, 9967),
        ], "超周期")

    def test_failure_is_atomic(self):
        # 非法批次整体抛错;合法流对象不被修改,之后仍可单独成功
        good = FlowSpec(0, (L(0),), 4, 1, 4)
        with self.assertRaises(TSNGateError):
            synthesize([good, FlowSpec(1, (L(0),), 4, 0, 4)])
        r = synthesize([good])
        self.assertEqual(r.status, SynthesisStatus.FEASIBLE)

    def test_replay_synthesis_type_check(self):
        with self.assertRaisesRegex(TSNGateError, "SynthesisResult"):
            replay_synthesis(object())


class TestReplayConsistency(unittest.TestCase):
    def test_every_instance_replays_as_planned(self):
        # guard 只在链路 0;三流共享链路 1,占用率合计 5/6,可行
        g0, l1 = L(0, guard=1), L(1)
        r = synthesize([
            FlowSpec(0, (g0, l1), 6, 2, 6),
            FlowSpec(1, (g0, l1), 6, 1, 6),
            FlowSpec(2, (l1,), 3, 1, 3),
        ])
        verify_synthesis(r)  # 不抛即逐实例一致
        for lr in replay_synthesis(r):
            self.assertEqual(lr.result.never_count, 0)
            for f in lr.result.frames:
                self.assertEqual(f.status, FrameStatus.SENT)

    def test_arrival_counts_match_periods(self):
        r = synthesize([
            FlowSpec(0, (L(0), L(1)), 4, 1, 4),
            FlowSpec(1, (L(0),), 6, 1, 6),
        ])
        H = r.hyperperiod_ns
        per_link = {0: H // 4 + H // 6, 1: H // 4}
        for lg in r.link_gates:
            self.assertEqual(len(lg.arrivals), per_link[lg.link_id])

    def test_result_is_frozen_and_complete(self):
        r = synthesize([FlowSpec(0, (L(0), L(1)), 4, 1, 4)])
        with self.assertRaises(Exception):
            r.hyperperiod_ns = 5
        self.assertEqual(len(r.link_gates), 2)
        self.assertTrue(all(lg.gate.cycle_ns == r.hyperperiod_ns
                            for lg in r.link_gates))


class TestAgainstBruteReference(unittest.TestCase):
    """与独立朴素暴力参考 reference_synth.reference_feasible 的可行性对照。"""

    def test_random_small_cases(self):
        rng = random.Random(20260924)
        feasible = infeasible = 0
        for case in range(240):
            nlinks = rng.randint(1, 4)
            links = {
                i: (rng.choice([4, 8]), rng.randint(0, 1))
                for i in range(nlinks)
            }
            period_pool = [2, 3, 4, 5, 6]
            nflows = rng.randint(1, 4)
            max_hops = 2 if nflows >= 3 else 3
            flow_specs = []
            ref_flows = []
            for fi in range(nflows):
                nh = rng.randint(1, max_hops)
                path_ids = tuple(rng.randrange(nlinks) for _ in range(nh))
                path = tuple(L(lid, rate=links[lid][0], guard=links[lid][1])
                             for lid in path_ids)
                P = rng.choice(period_pool)
                size = 1
                durs = [(size * 8 + links[lid][0] - 1) // links[lid][0]
                        for lid in path_ids]
                makespan = sum(durs)
                if makespan > P or any(d + links[lid][1] > P
                                       for d, lid in zip(durs, path_ids)):
                    break
                dl = rng.randint(makespan, P)
                flow_specs.append(FlowSpec(fi, path, P, size, dl))
                ref_flows.append((fi, list(path_ids), P, size, dl))
            else:
                ref_ok, H_ref = reference_feasible(ref_flows, links)
                try:
                    res = synthesize(flow_specs)
                    got_ok = True
                except InfeasibleScheduleError:
                    got_ok = False
                self.assertIs(
                    got_ok, ref_ok,
                    f"case {case}: 合成器判定 {'可行' if got_ok else '无解'},"
                    f"暴力参考判定 {'可行' if ref_ok else '无解'}:"
                    f" {ref_flows}/{links}",
                )
                if got_ok:
                    feasible += 1
                    self.assertEqual(res.hyperperiod_ns, H_ref)
                    verify_synthesis(res)  # 可行解必须自身可重放
                else:
                    infeasible += 1
        # 两类结论都应确实出现,防止参考/用例退化到单边
        self.assertGreater(feasible, 10)
        self.assertGreater(infeasible, 10)


if __name__ == "__main__":
    unittest.main()
