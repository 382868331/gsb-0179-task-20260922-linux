"""帧抢占迭代演示:express/preemptable 两类队列的取舍与边界情况。

场景 1:同一组到达,分别用 旧 replay(不启用抢占) 与 replay_preemptive 重放,
         对比紧急帧延迟的降低与普通帧的额外片段开销。
场景 2(边界):同刻“片段完成 = 紧急帧到达”,且门关闭不截断已开始的片段。

直接运行: python demo_iteration.py   (纯计算,无 sleep、无网络、无预录结果)
"""

from tsn_gate import (
    Frame,
    GateSchedule,
    GateWindow,
    PreemptionConfig,
    replay,
    replay_preemptive,
)


def fmt_fragments(frags):
    return " ".join(f"[{s},{e})" for s, e in frags)


def show_tradeoff() -> None:
    # 周期 1000ns;q0(紧急)与 q1(可抢占)共享窗口 [0,800);1 bit/ns(1B=8ns)
    schedule = GateSchedule(
        cycle_ns=1000,
        windows=(GateWindow(0, 800, frozenset({0, 1})),),
        link_rate_bits_per_ns=1,
        guard_ns=0,
    )
    arrivals = [
        Frame(queue=1, arrive_ns=0, size_bytes=90),    # 普通大帧 720ns
        Frame(queue=0, arrive_ns=100, size_bytes=20),  # 紧急帧 160ns
    ]
    config = PreemptionConfig(express_queues=frozenset({0}),
                              fragment_size_bytes=30,      # 片段 30B = 240ns
                              fragment_overhead_bytes=6)   # 每次恢复 +6B

    old = replay(arrivals, schedule)                      # 不启用抢占:原结果
    new = replay_preemptive(arrivals, schedule, config)   # 启用抢占

    print("== 场景 1:紧急帧延迟降低 vs 普通帧额外开销 ==")
    print("门表: 周期 1000ns, q0/q1 共享窗 [0,800), 1 bit/ns; 片段 30B, 恢复开销 6B/片")
    o0, o1 = old.frames
    n0, n1 = new.frames
    print(f"不启用抢占(旧接口 replay): 普通帧 [{o0.start_ns},{o0.end_ns}), "
          f"紧急帧 [{o1.start_ns},{o1.end_ns}) 等待 {o1.waited_ns}ns")
    print("启用抢占(replay_preemptive):")
    print(f"  普通帧片段: {fmt_fragments(n0.fragments)}  结束 {n0.end_ns}ns")
    print(f"  紧急帧 [{n1.start_ns},{n1.end_ns}) 等待 {n1.waited_ns}ns")
    print(f"取舍: 紧急帧等待 {o1.waited_ns}ns -> {n1.waited_ns}ns; "
          f"普通帧被抢占 {n0.preempted_count} 次, 额外开销 {n0.overhead_bytes}B, "
          f"结束 {o0.end_ns}ns -> {n0.end_ns}ns")
    assert n1.waited_ns < o1.waited_ns
    assert n0.overhead_bytes == 12 and n0.preempted_count == 1
    # 旧接口结果不受新功能影响
    assert (o0.start_ns, o0.end_ns, o1.start_ns, o1.end_ns) == (0, 720, 1000, 1160)


def show_boundary_case() -> None:
    # 边界:紧急帧到达时刻恰好是片段完成时刻(同刻次序:完成->到达->决策),
    # 且门关闭后不能再开新片段(第 3 片等下一周期)。
    schedule = GateSchedule(
        cycle_ns=1000,
        windows=(GateWindow(0, 600, frozenset({0, 1})),),
        link_rate_bits_per_ns=1,
        guard_ns=0,
    )
    config = PreemptionConfig(express_queues=frozenset({0}),
                              fragment_size_bytes=30,
                              fragment_overhead_bytes=0)
    arrivals = [
        Frame(queue=1, arrive_ns=0, size_bytes=90),
        Frame(queue=0, arrive_ns=240, size_bytes=10),  # 恰在片段边界到达
    ]
    r = replay_preemptive(arrivals, schedule, config)
    f0, f1 = r.frames
    print()
    print("== 场景 2(边界):同刻 片段完成=紧急帧到达;门关闭后不可开新片段 ==")
    print("门表: 周期 1000ns, 共享窗 [0,600); 片段 30B, 无恢复开销")
    print(f"紧急帧 t=240 到达(恰为首片结束时刻): 立即于 {f1.start_ns}ns 发出, "
          f"等待 {f1.waited_ns}ns")
    print(f"普通帧片段: {fmt_fragments(f0.fragments)}")
    print("第 3 片 [560,800) 跨过门关闭点 600:已开始片段不被截断,跑到完成;")
    print("若还有第 4 片,600 后不能开新片,须等下一周期窗口")
    assert f1.start_ns == 240 and f1.waited_ns == 0
    assert f0.fragments == ((0, 240), (320, 560), (560, 800))


def main() -> None:
    show_tradeoff()
    show_boundary_case()


if __name__ == "__main__":
    main()
