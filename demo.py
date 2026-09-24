"""演示:单跳门控重放(既有能力)+ 多跳存储转发排程合成(本次新增)。

直接运行: python demo.py   (纯计算,无 sleep、无网络、无预录结果;约 1 秒内完成)

所有打印数字均来自实际计算,断言用于当场核对语义,不是预先录好的答案。
"""

from tsn_gate import (
    Frame,
    FrameStatus,
    GateSchedule,
    GateWindow,
    TSNGateError,
    replay,
)
from tsn_synth import (
    FlowSpec,
    InfeasibleScheduleError,
    Link,
    SynthesisBudgetExhaustedError,
    synthesize,
    replay_synthesis,
)


def show_legacy_replay() -> None:
    print("=" * 68)
    print("一、既有能力:单跳门控发送重放(接口保持不变)")
    print("=" * 68)
    schedule = GateSchedule(
        cycle_ns=1000,
        windows=(
            GateWindow(0, 400, frozenset({0})),
            GateWindow(400, 800, frozenset({1})),
        ),
        link_rate_bits_per_ns=1,
        guard_ns=50,
    )
    arrivals = [
        Frame(queue=1, arrive_ns=350, size_bytes=40, deadline_ns=None),
        Frame(queue=1, arrive_ns=350, size_bytes=40, deadline_ns=1600),
        Frame(queue=1, arrive_ns=360, size_bytes=100, deadline_ns=None),
    ]
    result = replay(arrivals, schedule)
    for f in result.frames:
        print(f"  seq={f.seq} 状态={f.status.value:>22} "
              f"开始={f.start_ns} 结束={f.end_ns} "
              f"deadline违例={f.deadline_violated}")
    print(f"  汇总:发送 {result.sent_count},永不可发 {result.never_count}")
    assert result.frames[1].deadline_violated
    assert result.frames[2].status is FrameStatus.NEVER_TRANSMITTABLE


def show_synthesis_normal() -> None:
    print()
    print("=" * 68)
    print("二、新增能力:多跳存储转发排程合成(正常结果)")
    print("=" * 68)
    # 速率 8 bit/ns => 1 字节恰好 1ns;guard=0,便于手算核对。
    l0, l1, l2, l3 = (Link(i, rate_bits_per_ns=8) for i in range(4))
    flows = [
        # f10: 3 跳路径 0->1->2,每跳 2ns,P=10,deadline=6:
        #   唯一最紧凑形态 s=(0,2,4),末跳结束恰为 6
        FlowSpec(10, (l0, l1, l2), period_ns=10, size_bytes=2, deadline_ns=6),
        # f11: 路径 0->3,P=10;链路 0 的 [0,2) 已被 f10 占用,首跳至少 2
        FlowSpec(11, (l0, l3), period_ns=10, size_bytes=2, deadline_ns=10),
        # f12: 只走链路 1,P=5(超周期 H=lcm(10,10,5)=10),每周期一个实例;
        #   避开 f10 在链路 1 的 [2,4):s=0 -> 实例 [0,1) 与 [5,6)
        FlowSpec(12, (l1,), period_ns=5, size_bytes=1, deadline_ns=5),
    ]
    result = synthesize(flows)
    print(f"  超周期 H = {result.hyperperiod_ns}ns")
    for p in result.flow_plans:
        hops = " -> ".join(
            f"链路{h.link_id}[{h.start_offset_ns},{h.end_offset_ns})"
            for h in p.hops
        )
        print(f"  flow {p.flow_id}(队列{p.queue}) P={p.period_ns} "
              f"deadline={p.deadline_ns}: {hops}")
    # 手算期望逐条核对
    plan = {p.flow_id: [h.start_offset_ns for h in p.hops] for p in result.flow_plans}
    assert plan[10] == [0, 2, 4], plan
    assert plan[11] == [2, 4], plan
    assert plan[12] == [0], plan

    print("  超周期内发送实例(按开始时刻):")
    for tr in result.transmissions:
        print(f"    t={tr.start_ns:>2}..{tr.end_ns:<2} 链路{tr.link_id} "
              f"flow{tr.flow_id} 第{tr.hop_index}跳 释放={tr.release_ns}")
    f12_starts = sorted(tr.start_ns for tr in result.transmissions
                        if tr.flow_id == 12)
    assert f12_starts == [0, 5], f12_starts  # P=5 的两个实例

    print("  用既有 replay() 逐链路重放合成门表:")
    total_instances = len(result.transmissions)
    checked = 0
    for lr in replay_synthesis(result):
        rr = lr.result
        print(f"    链路{lr.link_id}: {rr.sent_count} 个实例全部 SENT,"
              f"永不可发 {rr.never_count}")
        assert rr.never_count == 0
        checked += rr.sent_count
    assert checked == total_instances


def show_synthesis_boundary() -> None:
    print()
    print("=" * 68)
    print("三、边界:deadline 恰好满足;发送+guard 恰好顶满周期")
    print("=" * 68)
    # 两跳各 2ns,makespan=4:deadline=4 恰好可行,=3 则无解
    l0, l1 = Link(0), Link(1)
    r = synthesize([FlowSpec(0, (l0, l1), 10, 2, 4)])
    ends = [h.end_offset_ns for h in r.flow_plans[0].hops]
    print(f"  deadline=4:逐跳结束偏移 {ends}(末跳={ends[-1]} 恰好 = deadline)")
    assert ends[-1] == 4
    try:
        synthesize([FlowSpec(0, (l0, l1), 10, 2, 3)])
    except InfeasibleScheduleError as exc:
        print(f"  deadline=3:{exc}")

    # d=3, guard=1, P=4:占用恰好 4,窗口 [0,4) 顶到周期终点(端点相接允许)
    g = Link(7, guard_ns=1)
    r = synthesize([FlowSpec(7, (g,), 4, 3, 4)])
    w = r.link_gates[0].gate.windows[0]
    print(f"  d=3+guard=1 恰好=周期 4:窗口 [{w.start_ns},{w.end_ns}),"
          f"重放发送数={replay_synthesis(r)[0].result.sent_count}")
    assert (w.start_ns, w.end_ns) == (0, 4)


def show_synthesis_errors() -> None:
    print()
    print("=" * 68)
    print("四、实际触发的错误:无解 / 预算不足 / 非法输入(三者分开)")
    print("=" * 68)
    l0 = Link(0)

    # 1) 已证明无解:占用率各 1/2 但任何相位都冲突
    try:
        synthesize([
            FlowSpec(0, (l0,), 4, 2, 4),
            FlowSpec(1, (l0,), 6, 3, 6),
        ])
    except InfeasibleScheduleError as exc:
        print(f"  [无解] {exc}")

    # 2) 预算耗尽:输入本身平凡可行,截断后既非可行也非无解
    try:
        synthesize([FlowSpec(0, (l0,), 100, 1, 100)], max_nodes=1)
    except SynthesisBudgetExhaustedError as exc:
        print(f"  [预算] {exc}")
    same = synthesize([FlowSpec(0, (l0,), 100, 1, 100)])
    assert same.status.value == "feasible"
    print("         同一输入在默认预算下可行 -> 预算不足不等于无解")

    # 3) 非法输入:可定位字段,整体拒绝、无部分结果
    bad_calls = [
        ("5 条流(上限 4)",
         [FlowSpec(i, (l0,), 4, 1, 4) for i in range(5)]),
        ("deadline 大于周期",
         [FlowSpec(0, (l0,), 4, 1, 5)]),
        ("size_bytes 为 0",
         [FlowSpec(0, (l0,), 4, 0, 4)]),
    ]
    for label, flows in bad_calls:
        try:
            synthesize(flows)
        except TSNGateError as exc:
            print(f"  [非法] {label}:{exc}")
        else:
            raise AssertionError(f"{label}: 未被拒绝")


def main() -> None:
    show_legacy_replay()
    show_synthesis_normal()
    show_synthesis_boundary()
    show_synthesis_errors()
    print()
    print("全部演示计算完成。")


if __name__ == "__main__":
    main()
