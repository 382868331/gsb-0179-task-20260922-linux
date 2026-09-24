"""演示:门控造成的错过窗口 + 多跳排程合成 + 实际触发的拒绝/异常边界。

直接运行: python demo.py   (纯计算,无 sleep、无网络、无预录结果)
"""

from tsn_gate import (
    BudgetExhaustedError,
    Frame,
    FrameStatus,
    GateSchedule,
    GateWindow,
    TSNGateError,
    replay,
)
from hop_schedule import (
    Flow,
    InfeasibleError,
    Link,
    Network,
    synthesize,
)


def show_normal_case() -> None:
    # 周期 1000ns;队列 0 窗口 [0,400),队列 1 窗口 [400,800);
    # 链路 1 bit/ns(1Gbps),guard 50ns。
    schedule = GateSchedule(
        cycle_ns=1000,
        windows=(
            GateWindow(0, 400, frozenset({0})),
            GateWindow(400, 800, frozenset({1})),
        ),
        link_rate_bits_per_ns=1,
        guard_ns=50,
        clock_offset_ns=0,
    )
    arrivals = [
        # 40B -> 320ns,+guard=370 <= 400:可发,但到达时 q1 门未开
        Frame(queue=1, arrive_ns=350, size_bytes=40, deadline_ns=None),
        # 同到一帧:被前一帧挡住,剩余窗口 80ns 不够,错过本周期窗口,
        # 只能等下一周期 [1400,1800),从而错过 deadline 1600
        Frame(queue=1, arrive_ns=350, size_bytes=40, deadline_ns=1600),
        # 100B -> 800ns,+guard=850 > 400:任何窗口都容不下 -> 永不可发
        Frame(queue=1, arrive_ns=360, size_bytes=100, deadline_ns=None),
    ]
    result = replay(arrivals, schedule)

    print("== 正常结果:门控造成的错过窗口 ==")
    print("门表: 周期 1000ns, q0 窗 [0,400), q1 窗 [400,800), guard 50ns, 1 bit/ns")
    print(f"{'seq':>3} {'队列':>4} {'到达':>6} {'状态':>22} "
          f"{'开始':>6} {'结束':>6} {'等待':>5}  等待原因 / deadline")
    for f in result.frames:
        reasons = ",".join(r.value for r in f.wait_reasons) or "-"
        dl = "-"
        if f.deadline_ns is not None:
            dl = f"deadline {f.deadline_ns} " + ("违例!" if f.deadline_violated else "满足")
        start = "-" if f.start_ns is None else str(f.start_ns)
        end = "-" if f.end_ns is None else str(f.end_ns)
        waited = "-" if f.waited_ns is None else str(f.waited_ns)
        print(f"{f.seq:>3} {f.queue:>4} {f.arrive_ns:>6} {f.status.value:>22} "
              f"{start:>6} {end:>6} {waited:>5}  {reasons} / {dl}")
    print(f"汇总: 发送 {result.sent_count} 帧, 永不可发 {result.never_count} 帧, "
          f"deadline 违例 seq={list(result.deadline_violations)}")
    assert result.frames[1].deadline_violated, "演示场景应触发 deadline 违例"
    assert result.frames[2].status is FrameStatus.NEVER_TRANSMITTABLE


def show_rejection_case() -> None:
    print()
    print("== 拒绝边界:非法输入被可定位地拒绝 ==")
    schedule = GateSchedule(
        cycle_ns=100,
        windows=(GateWindow(0, 10, frozenset({0})),),
        link_rate_bits_per_ns=8,
    )
    bad_calls = [
        ("队列号为 bool", [Frame(queue=True, arrive_ns=0, size_bytes=1)]),
        ("到达时间为 NaN", [Frame(queue=0, arrive_ns=float("nan"), size_bytes=1)]),
        ("队列号越界(>3)", [Frame(queue=4, arrive_ns=0, size_bytes=1)]),
    ]
    for label, arrivals in bad_calls:
        try:
            replay(arrivals, schedule)
        except TSNGateError as exc:
            print(f"  {label}: 已拒绝 -> {exc}")
        else:
            raise AssertionError(f"{label}: 未被拒绝,不符合契约")


def show_multihop_case() -> None:
    print()
    print("== 新能力:多跳存储转发排程合成(正常结果)==")
    # 速率 8 bit/ns => 1 字节恰好 1ns,便于手算核对。
    network = Network(
        links=(Link("A", "B"), Link("B", "C"), Link("C", "D")),
        default_rate_bits_per_ns=8,
    )
    flows = [
        # f0: A->B->C,周期 100,截止 60,每跳 10B(10ns)
        Flow(name="f0", path=("A", "B", "C"), period_ns=100,
             deadline_ns=60, sizes_bytes=10),
        # f1: B->C->D,周期 50,截止 40;B->C 与 f0 共享,需错开
        Flow(name="f1", path=("B", "C", "D"), period_ns=50,
             deadline_ns=40, sizes_bytes=10),
        # f2: A->B,周期 100,20B;A->B 与 f0 共享
        Flow(name="f2", path=("A", "B"), period_ns=100,
             deadline_ns=80, sizes_bytes=20),
    ]
    schedule = synthesize(flows, network)
    print(f"超周期 H=lcm(100,50,100)={schedule.hyperperiod_ns}ns, "
          f"链路速率 8 bit/ns(1B=1ns)")
    for fs in schedule.flows:
        print(f"流 {fs.name} 路径 {'-'.join(fs.path)} "
              f"周期 {fs.period_ns} 释放@{fs.release_ns} 截止<={fs.deadline_ns}")
        for h in fs.hops:
            print(f"    hop{h.hop_index} {h.link}: [{h.start_ns},{h.end_ns}) "
                  f"时长 {h.duration_ns}")
    # 手算核对:
    #   f0: A->B [0,10) -> B->C [10,20)   (存储转发,无空隙)
    #   f1: B->C [0,10) -> C->D [10,20)   (与 f0 在 B->C 上首尾相接)
    #   f2: A->B [10,30)                  (让过 f0,同链路不冲突)
    expected = {
        ("f0", 0): (0, 10), ("f0", 1): (10, 20),
        ("f1", 0): (0, 10), ("f1", 1): (10, 20),
        ("f2", 0): (10, 30),
    }
    for fs in schedule.flows:
        for h in fs.hops:
            assert (h.start_ns, h.end_ns) == expected[(fs.name, h.hop_index)], \
                f"演示手算核对失败: {fs.name} hop{h.hop_index}"
    print("手算核对通过:逐跳起点/结束与离线手算一致。")
    print("可重放核对:对每条链路用 tsn_gate.replay 重放合成门表 ...")
    for lk in schedule.links:
        lr = schedule.link_replays[lk]
        got = [(f.start_ns, f.end_ns) for f in lr.result.frames]
        assert tuple(got) == lr.expected
        wins = ", ".join(f"[{w.start_ns},{w.end_ns})" for w in lr.gate_schedule.windows)
        print(f"    链路 {lk}: 窗口 {wins} -> replay {got} 与排程一致")


def show_boundary_case() -> None:
    print()
    print("== 边界:周期相同的两条流在共享链路上首尾相接(半开区间,相切不冲突)==")
    network = Network(links=(Link("A", "B"),), default_rate_bits_per_ns=8)
    flows = [
        Flow(name="f0", path=("A", "B"), period_ns=20,
             deadline_ns=20, sizes_bytes=10),
        Flow(name="f1", path=("A", "B"), period_ns=20,
             deadline_ns=20, sizes_bytes=10),
    ]
    schedule = synthesize(flows, network)
    t0 = schedule.flows[0].hops[0].start_ns
    t1 = schedule.flows[1].hops[0].start_ns
    print(f"    f0 占 [0,10),f1 占 [{t1},{t1 + 10}):恰好拼成一个周期,合成成功")
    assert (t0, t1) == (0, 10)

    print()
    print("== 边界:两条流各需 11ns、周期同为 20ns(11+11>20),完整枚举穷尽 ==")
    tight = [
        Flow(name="f0", path=("A", "B"), period_ns=20,
             deadline_ns=20, sizes_bytes=11),
        Flow(name="f1", path=("A", "B"), period_ns=20,
             deadline_ns=20, sizes_bytes=11),
    ]
    try:
        synthesize(tight, network)
    except InfeasibleError as exc:
        print(f"    已判定无解(InfeasibleError): {exc}")
    else:
        raise AssertionError("应判定无解")


def show_schedule_failures() -> None:
    print()
    print("== 新能力的实际错误:预算不足 / 非法输入(与无解严格区分)==")
    network = Network(links=(Link("A", "B"),), default_rate_bits_per_ns=8)

    # 预算不足:lcm(100,60)=300,时间轴预算只给 99 -> 搜索前即拒绝
    flows = [
        Flow(name="f0", path=("A", "B"), period_ns=100,
             deadline_ns=50, sizes_bytes=1),
        Flow(name="f1", path=("A", "B"), period_ns=60,
             deadline_ns=50, sizes_bytes=1),
    ]
    try:
        synthesize(flows, network, max_search_ns=99)
    except BudgetExhaustedError as exc:
        print(f"  超周期超预算 -> BudgetExhaustedError: {exc}")
    else:
        raise AssertionError("应触发预算不足")
    # 同一输入预算充足即可行:预算不足不是“无解”
    s = synthesize(flows, network, max_search_ns=300)
    print(f"  同一输入预算给到 300ns 即合成成功(H={s.hyperperiod_ns}),二者不混淆")

    bad_calls = [
        ("路径使用了未声明的链路",
         [Flow(name="f", path=("A", "X"), period_ns=10,
               deadline_ns=5, sizes_bytes=1)]),
        ("流数超过上限 4",
         [Flow(name=f"f{i}", path=("A", "B"), period_ns=10,
               deadline_ns=5, sizes_bytes=1) for i in range(5)]),
        ("周期为 bool",
         [Flow(name="f", path=("A", "B"), period_ns=True,
               deadline_ns=5, sizes_bytes=1)]),
        ("跳长超过周期(自我重叠,直接无解)",
         [Flow(name="f", path=("A", "B"), period_ns=10,
               deadline_ns=50, sizes_bytes=11)]),
    ]
    for label, bad_flows in bad_calls:
        try:
            synthesize(bad_flows, network)
        except InfeasibleError as exc:
            print(f"  {label}: InfeasibleError -> {exc}")
        except TSNGateError as exc:
            print(f"  {label}: TSNGateError -> {exc}")
        else:
            raise AssertionError(f"{label}: 未被拒绝,不符合契约")


def main() -> None:
    show_normal_case()
    show_rejection_case()
    show_multihop_case()
    show_boundary_case()
    show_schedule_failures()


if __name__ == "__main__":
    main()
