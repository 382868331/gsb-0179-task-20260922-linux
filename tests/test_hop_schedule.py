"""hop_schedule 单元测试:手算定点用例 + 失败/边界 + 与独立参考枚举器的随机对照。

参考实现 tests/reference_hop.py 不导入 hop_schedule:冲突判定用周期实例
逐条展开,可行性用按流递归的独立枚举,与被测核心的模运算判据 + DFS 完全不同。
"""

import random
import unittest

from tsn_gate import BudgetExhaustedError, TSNGateError
from hop_schedule import (
    DEFAULT_MAX_SEARCH_NS,
    Flow,
    InfeasibleError,
    Link,
    MultiHopSchedule,
    Network,
    _patterns_conflict,
    replay_schedule,
    synthesize,
)
from reference_hop import check_schedule, reference_verdict, trains_conflict


def net(*links, rate=8, guard=0):
    return Network(links=tuple(links), default_rate_bits_per_ns=rate,
                   default_guard_ns=guard)


def hop_of(sched, flow_name, hop_index):
    for fs in sched.flows:
        if fs.name == flow_name:
            return fs.hops[hop_index]
    raise AssertionError(f"流 {flow_name} 不存在")


class TestHandComputed(unittest.TestCase):
    """手算期望值的小用例(期望不来自被测实现)。"""

    def test_single_flow_two_hops(self):
        # rate=8 -> 10B = 10ns/跳;首跳 [0,10),次跳 [10,20),截止 20 恰好满足
        n = net(Link("A", "B"), Link("B", "C"))
        flows = [Flow(name="f", path=("A", "B", "C"), period_ns=100,
                      deadline_ns=20, sizes_bytes=10)]
        s = synthesize(flows, n)
        self.assertEqual(s.hyperperiod_ns, 100)
        h0, h1 = s.flows[0].hops
        self.assertEqual((h0.link, h0.start_ns, h0.end_ns), ("A->B", 0, 10))
        self.assertEqual((h1.link, h1.start_ns, h1.end_ns), ("B->C", 10, 20))
        self.assertEqual(s.links, ("A->B", "B->C"))

    def test_two_flows_share_link(self):
        # f0 周期 100 占 A->B [0,10);f1 周期 50 时长 10,最早不冲突起点为 10
        n = net(Link("A", "B"), Link("B", "C"))
        flows = [
            Flow(name="f0", path=("A", "B", "C"), period_ns=100,
                 deadline_ns=60, sizes_bytes=10),
            Flow(name="f1", path=("A", "B"), period_ns=50,
                 deadline_ns=40, sizes_bytes=10),
        ]
        s = synthesize(flows, n)
        self.assertEqual(s.hyperperiod_ns, 100)
        self.assertEqual((hop_of(s, "f0", 0).start_ns, hop_of(s, "f0", 0).end_ns),
                         (0, 10))
        self.assertEqual((hop_of(s, "f0", 1).start_ns, hop_of(s, "f0", 1).end_ns),
                         (10, 20))
        self.assertEqual((hop_of(s, "f1", 0).start_ns, hop_of(s, "f1", 0).end_ns),
                         (10, 20))

    def test_offset_delays_release(self):
        n = net(Link("A", "B"))
        flows = [Flow(name="f", path=("A", "B"), period_ns=50, deadline_ns=30,
                      sizes_bytes=10, offset_ns=7)]
        s = synthesize(flows, n)
        self.assertEqual(hop_of(s, "f", 0).start_ns, 7)
        self.assertEqual(s.flows[0].release_ns, 7)
        self.assertEqual(s.flows[0].deadline_ns, 37)

    def test_per_link_rate_changes_duration(self):
        # A->B: 8 bit/ns -> 10B = 10ns;B->C: 4 bit/ns -> 10B = 20ns
        n = Network(links=(Link("A", "B"), Link("B", "C")),
                    default_rate_bits_per_ns=8,
                    link_rates_bits_per_ns={"B->C": 4})
        flows = [Flow(name="f", path=("A", "B", "C"), period_ns=100,
                      deadline_ns=60, sizes_bytes=10)]
        s = synthesize(flows, n)
        self.assertEqual(hop_of(s, "f", 0).duration_ns, 10)
        self.assertEqual(hop_of(s, "f", 1).duration_ns, 20)
        self.assertEqual(hop_of(s, "f", 1).start_ns, 10)

    def test_per_hop_sizes(self):
        n = net(Link("A", "B"), Link("B", "C"))
        flows = [Flow(name="f", path=("A", "B", "C"), period_ns=100,
                      deadline_ns=60, sizes_bytes=(10, 20))]
        s = synthesize(flows, n)
        self.assertEqual(hop_of(s, "f", 0).duration_ns, 10)
        self.assertEqual(hop_of(s, "f", 1).duration_ns, 20)

    def test_guard_extends_replay_window_not_duration(self):
        n = net(Link("A", "B"), guard=5)
        flows = [Flow(name="f", path=("A", "B"), period_ns=100,
                      deadline_ns=30, sizes_bytes=10)]
        s = synthesize(flows, n)
        lr = s.link_replays["A->B"]
        (w,) = lr.gate_schedule.windows
        self.assertEqual((w.start_ns, w.end_ns), (0, 15))  # 10ns 时长 + 5ns guard
        self.assertEqual(hop_of(s, "f", 0).duration_ns, 10)


class TestReplayable(unittest.TestCase):
    """可重放性:合成结果用 tsn_gate.replay 逐链路重放,与排程精确一致。"""

    def test_replay_matches_schedule(self):
        n = net(Link("A", "B"), Link("B", "C"), Link("C", "D"))
        flows = [
            Flow(name="f0", path=("A", "B", "C"), period_ns=60,
                 deadline_ns=50, sizes_bytes=10),
            Flow(name="f1", path=("B", "C", "D"), period_ns=40,
                 deadline_ns=45, sizes_bytes=5),
            Flow(name="f2", path=("A", "B"), period_ns=30,
                 deadline_ns=25, sizes_bytes=8),
        ]
        s = synthesize(flows, n)
        results = s.replay_all()
        self.assertEqual(set(results), set(s.links))
        for lk, lr in s.link_replays.items():
            got = [(f.start_ns, f.end_ns) for f in results[lk].frames]
            self.assertEqual(tuple(got), lr.expected, f"链路 {lk} 重放与排程不符")
            self.assertEqual(results[lk].deadline_violations, ())
        # 便捷入口与对象方法一致
        again = replay_schedule(s)
        for lk in s.links:
            self.assertEqual(
                [f.start_ns for f in again[lk].frames],
                [f.start_ns for f in results[lk].frames])

    def test_replay_link_unknown_rejected(self):
        n = net(Link("A", "B"))
        s = synthesize([Flow(name="f", path=("A", "B"), period_ns=10,
                             deadline_ns=5, sizes_bytes=1)], n)
        with self.assertRaisesRegex(TSNGateError, "未知链路"):
            s.replay_link("X->Y")
        with self.assertRaisesRegex(TSNGateError, "schedule"):
            replay_schedule(object())


class TestBoundary(unittest.TestCase):
    def test_deadline_exact_fit_ok(self):
        # 两跳各 10ns,截止恰好 20:末跳结束 == 截止,允许
        n = net(Link("A", "B"), Link("B", "C"))
        flows = [Flow(name="f", path=("A", "B", "C"), period_ns=100,
                      deadline_ns=20, sizes_bytes=10)]
        s = synthesize(flows, n)
        self.assertEqual(s.flows[0].hops[-1].end_ns, 20)

    def test_deadline_one_ns_tighter_infeasible(self):
        n = net(Link("A", "B"), Link("B", "C"))
        flows = [Flow(name="f", path=("A", "B", "C"), period_ns=100,
                      deadline_ns=19, sizes_bytes=10)]
        with self.assertRaises(InfeasibleError):
            synthesize(flows, n)

    def test_touching_instances_ok(self):
        # 两条流周期相同、时长之和恰等于周期:首尾相接不冲突(半开区间)
        n = net(Link("A", "B"))
        flows = [
            Flow(name="f0", path=("A", "B"), period_ns=20, deadline_ns=20,
                 sizes_bytes=10),
            Flow(name="f1", path=("A", "B"), period_ns=20, deadline_ns=20,
                 sizes_bytes=10),
        ]
        s = synthesize(flows, n)
        self.assertEqual(hop_of(s, "f0", 0).start_ns, 0)
        self.assertEqual(hop_of(s, "f1", 0).start_ns, 10)

    def test_max_flows_and_hops_accepted(self):
        # 上限边界:4 条流、每条 3 跳(4 节点),全部成功合成
        links = tuple(Link(a, b) for a, b in
                      [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")])
        n = net(*links)
        paths = [("A", "B", "C", "D"), ("B", "C", "D", "E"),
                 ("A", "B", "C", "D"), ("B", "C", "D", "E")]
        flows = [Flow(name=f"f{i}", path=paths[i], period_ns=120,
                      deadline_ns=90, sizes_bytes=5) for i in range(4)]
        s = synthesize(flows, n)
        self.assertEqual(len(s.flows), 4)
        for fs in s.flows:
            self.assertEqual(len(fs.hops), 3)


class TestInfeasible(unittest.TestCase):
    def test_duration_exceeds_period(self):
        # 11B -> 11ns > 周期 10ns:自身相邻实例必然重叠
        n = net(Link("A", "B"))
        flows = [Flow(name="f", path=("A", "B"), period_ns=10,
                      deadline_ns=50, sizes_bytes=11)]
        with self.assertRaisesRegex(InfeasibleError, "超过周期"):
            synthesize(flows, n)

    def test_two_flows_cannot_share_period(self):
        # 周期 20,两流各需 11ns:11+11 > 20,任何相位都冲突
        n = net(Link("A", "B"))
        flows = [
            Flow(name="f0", path=("A", "B"), period_ns=20, deadline_ns=20,
                 sizes_bytes=11),
            Flow(name="f1", path=("A", "B"), period_ns=20, deadline_ns=20,
                 sizes_bytes=11),
        ]
        with self.assertRaises(InfeasibleError):
            synthesize(flows, n)

    def test_cross_instance_capacity_infeasible(self):
        # f0 周期 10、f1 周期 20 共享一条链路:g=gcd=10,可行 iff d0+d1 <= 10
        # (f0 每 10ns 占 d0,留给 f1 的最大连续空隙为 10-d0)
        n = net(Link("A", "B"))

        def mk(d0, d1):
            return [
                Flow(name="f0", path=("A", "B"), period_ns=10, deadline_ns=40,
                     sizes_bytes=d0),
                Flow(name="f1", path=("A", "B"), period_ns=20, deadline_ns=40,
                     sizes_bytes=d1),
            ]

        s = synthesize(mk(6, 4), n)          # 6+4 = 10:恰好可行
        self.assertIsInstance(s, MultiHopSchedule)
        with self.assertRaises(InfeasibleError):
            synthesize(mk(6, 5), n)          # 6+5 = 11 > 10:不可行

    def test_infeasible_is_not_budget_error(self):
        n = net(Link("A", "B"))
        flows = [Flow(name="f", path=("A", "B"), period_ns=10,
                      deadline_ns=5, sizes_bytes=8)]  # 8ns > 5ns 截止
        with self.assertRaises(InfeasibleError) as ctx:
            synthesize(flows, n)
        self.assertNotIsInstance(ctx.exception, BudgetExhaustedError)


class TestBudget(unittest.TestCase):
    def test_node_budget_exhausted(self):
        n = net(Link("A", "B"), Link("B", "C"))
        flows = [
            Flow(name="f0", path=("A", "B", "C"), period_ns=100,
                 deadline_ns=60, sizes_bytes=10),
            Flow(name="f1", path=("A", "B"), period_ns=50,
                 deadline_ns=40, sizes_bytes=10),
        ]
        with self.assertRaises(BudgetExhaustedError):
            synthesize(flows, n, max_nodes=2)
        # 同一输入预算充足时可行:预算耗尽 ≠ 无解
        s = synthesize(flows, n)
        self.assertIsInstance(s, MultiHopSchedule)

    def test_hyperperiod_budget(self):
        n = net(Link("A", "B"))
        flows = [
            Flow(name="f0", path=("A", "B"), period_ns=100, deadline_ns=50,
                 sizes_bytes=1),
            Flow(name="f1", path=("A", "B"), period_ns=50, deadline_ns=50,
                 sizes_bytes=1),
        ]
        with self.assertRaisesRegex(BudgetExhaustedError, "超周期"):
            synthesize(flows, n, max_search_ns=99)
        s = synthesize(flows, n, max_search_ns=100)
        self.assertEqual(s.hyperperiod_ns, 100)

    def test_deadline_budget(self):
        n = net(Link("A", "B"))
        flows = [Flow(name="f", path=("A", "B"), period_ns=10,
                      deadline_ns=5000, sizes_bytes=1)]
        with self.assertRaisesRegex(BudgetExhaustedError, "截止"):
            synthesize(flows, n, max_search_ns=1000)

    def test_budget_params_validated(self):
        n = net(Link("A", "B"))
        flows = [Flow(name="f", path=("A", "B"), period_ns=10,
                      deadline_ns=5, sizes_bytes=1)]
        with self.assertRaisesRegex(TSNGateError, "max_nodes"):
            synthesize(flows, n, max_nodes=0)
        with self.assertRaisesRegex(TSNGateError, "max_search_ns"):
            synthesize(flows, n, max_search_ns=True)


class TestInvalidInput(unittest.TestCase):
    def good(self):
        n = net(Link("A", "B"))
        f = Flow(name="f", path=("A", "B"), period_ns=10, deadline_ns=5,
                 sizes_bytes=1)
        return f, n

    def test_bool_and_float_rejected(self):
        f, n = self.good()
        with self.assertRaisesRegex(TSNGateError, r"flows\[0\]\.period_ns"):
            synthesize([Flow(name="f", path=("A", "B"), period_ns=True,
                             deadline_ns=5, sizes_bytes=1)], n)
        with self.assertRaisesRegex(TSNGateError, r"flows\[0\]\.deadline_ns.*NaN"):
            synthesize([Flow(name="f", path=("A", "B"), period_ns=10,
                             deadline_ns=float("nan"), sizes_bytes=1)], n)
        with self.assertRaisesRegex(TSNGateError, r"flows\[0\]\.offset_ns"):
            synthesize([Flow(name="f", path=("A", "B"), period_ns=10,
                             deadline_ns=5, sizes_bytes=1, offset_ns=1.5)], n)

    def test_flow_count_and_hop_limits(self):
        f, n = self.good()
        with self.assertRaisesRegex(TSNGateError, "flows"):
            synthesize([], n)
        with self.assertRaisesRegex(TSNGateError, "flows"):
            synthesize([f] * 5, n)  # 5 > MAX_FLOWS(同名也会先触发数量错误)
        links4 = tuple(Link(a, b) for a, b in
                       [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")])
        n4 = net(*links4)
        with self.assertRaisesRegex(TSNGateError, r"flows\[0\]\.path"):
            synthesize([Flow(name="f", path=("A", "B", "C", "D", "E"),
                             period_ns=10, deadline_ns=9, sizes_bytes=1)], n4)

    def test_duplicate_name_and_unknown_link(self):
        n = net(Link("A", "B"))
        dup = [Flow(name="f", path=("A", "B"), period_ns=10, deadline_ns=5,
                    sizes_bytes=1),
               Flow(name="f", path=("A", "B"), period_ns=10, deadline_ns=5,
                    sizes_bytes=1)]
        with self.assertRaisesRegex(TSNGateError, "重复"):
            synthesize(dup, n)
        with self.assertRaisesRegex(TSNGateError, "不在 Network.links"):
            synthesize([Flow(name="g", path=("A", "C"), period_ns=10,
                             deadline_ns=5, sizes_bytes=1)], n)

    def test_sizes_validation(self):
        n = net(Link("A", "B"), Link("B", "C"))
        with self.assertRaisesRegex(TSNGateError, "sizes_bytes"):
            synthesize([Flow(name="f", path=("A", "B", "C"), period_ns=10,
                             deadline_ns=9, sizes_bytes=0)], n)
        with self.assertRaisesRegex(TSNGateError, "sizes_bytes"):
            synthesize([Flow(name="f", path=("A", "B", "C"), period_ns=10,
                             deadline_ns=9, sizes_bytes=(1,))], n)  # 长度≠跳数
        with self.assertRaisesRegex(TSNGateError, r"sizes_bytes\[1\]"):
            synthesize([Flow(name="f", path=("A", "B", "C"), period_ns=10,
                             deadline_ns=9, sizes_bytes=(1, -2))], n)

    def test_flow_and_network_construction(self):
        with self.assertRaisesRegex(TSNGateError, "path"):
            Flow(name="f", path=("A", "A"), period_ns=1, deadline_ns=1)
        with self.assertRaisesRegex(TSNGateError, "name"):
            Flow(name="", path=("A", "B"), period_ns=1, deadline_ns=1)
        with self.assertRaisesRegex(TSNGateError, "links"):
            Network(links=())
        with self.assertRaisesRegex(TSNGateError, "重复"):
            Network(links=("A->B", "A->B"))
        with self.assertRaisesRegex(TSNGateError, "不在 links"):
            Network(links=("A->B",), link_rates_bits_per_ns={"X->Y": 8})
        with self.assertRaisesRegex(TSNGateError, "default_rate_bits_per_ns"):
            Network(links=("A->B",), default_rate_bits_per_ns=0)

    def test_wrong_types(self):
        f, n = self.good()
        with self.assertRaisesRegex(TSNGateError, "network"):
            synthesize([f], object())
        with self.assertRaisesRegex(TSNGateError, r"flows\[0\]"):
            synthesize([object()], n)

    def test_failure_leaves_inputs_untouched(self):
        # 非法批次整体抛错;合法调用的 Flow 对象不被归一化改写
        n = net(Link("A", "B"), Link("B", "C"))
        f = Flow(name="f", path=("A", "B", "C"), period_ns=100,
                 deadline_ns=60, sizes_bytes=10)
        s = synthesize([f], n)
        self.assertIsInstance(s, MultiHopSchedule)
        self.assertEqual(f.sizes_bytes, 10)  # 未被改写成 (10, 10)
        bad = Flow(name="g", path=("A", "B"), period_ns=10, deadline_ns=5,
                   sizes_bytes=1)
        with self.assertRaises(TSNGateError):
            synthesize([bad, Flow(name="h", path=("A", "B"), period_ns=-1,
                                  deadline_ns=5, sizes_bytes=1)], n)


class TestModularCriterion(unittest.TestCase):
    """模运算冲突判据 vs 独立逐实例展开(解析关系核对)。"""

    def test_against_expansion(self):
        rng = random.Random(20260924)
        for _ in range(4000):
            pa = rng.randint(1, 30)
            pb = rng.randint(1, 30)
            da = rng.randint(1, pa)
            db = rng.randint(1, pb)
            ta = rng.randint(0, 50)
            tb = rng.randint(0, 50)
            horizon = max(ta, tb) + 4 * max(pa, pb) + da + db
            expected = trains_conflict(ta, pa, da, tb, pb, db, horizon)
            got = _patterns_conflict(ta, pa, da, tb, pb, db)
            self.assertEqual(got, expected,
                             f"判据与展开不符: {(ta, pa, da, tb, pb, db)}")


class TestAgainstReference(unittest.TestCase):
    """确定性随机小用例:被测核心 vs 独立参考枚举器(判定一致 + 输出合法)。"""

    def test_fuzz_small_cases(self):
        rng = random.Random(20260924)
        nodes = ("N0", "N1", "N2")       # 节点少 -> 链路共享多,两类判定都有覆盖
        checked = feasible = 0
        for case in range(150):
            nflows = rng.randint(1, 3)
            flows = []
            ref_flows = []
            for fi in range(nflows):
                nhops = rng.randint(1, 2)
                path = rng.sample(nodes, nhops + 1)
                period = rng.choice([4, 6, 12])
                offset = rng.randint(0, 3)
                sizes = tuple(rng.randint(1, 3) for _ in range(nhops))
                durs = sizes                           # rate=8 -> 1B=1ns
                if rng.random() < 0.25:
                    deadline = rng.randint(1, sum(durs))        # 偏紧,常无解
                else:
                    deadline = sum(durs) + rng.randint(0, 4)
                name = f"f{fi}"
                flows.append(Flow(name=name, path=tuple(path),
                                  period_ns=period, deadline_ns=deadline,
                                  sizes_bytes=sizes, offset_ns=offset))
                ref_flows.append((period, offset, deadline,
                                  tuple((Link(path[h], path[h + 1]), durs[h])
                                        for h in range(nhops))))
            all_links = sorted({lk for (_p, _o, _d, hops) in ref_flows
                                for (lk, _d) in hops})
            n = Network(links=tuple(all_links), default_rate_bits_per_ns=8)
            exp_feasible, _witness = reference_verdict(ref_flows)
            checked += 1
            if exp_feasible:
                feasible += 1
                s = synthesize(flows, n)
                # 输出本身必须独立合法(不依赖被测实现的内部结论)
                starts = [tuple(h.start_ns for h in fs.hops) for fs in s.flows]
                problems = check_schedule(ref_flows, starts)
                self.assertEqual(problems, [], f"case {case}: {problems}")
                # 可重放性:replay 结果与排程一致
                for lk, lr in s.link_replays.items():
                    got = tuple((f.start_ns, f.end_ns) for f in lr.result.frames)
                    self.assertEqual(got, lr.expected,
                                     f"case {case} 链路 {lk} 重放不符")
            else:
                with self.assertRaises(InfeasibleError,
                                       msg=f"case {case}: 参考判无解,核心却合成成功"):
                    synthesize(flows, n)
        #  sanity:两类情形都覆盖到了
        self.assertGreater(feasible, 20)
        self.assertGreater(checked - feasible, 20)


if __name__ == "__main__":
    unittest.main()
