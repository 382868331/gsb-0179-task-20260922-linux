"""本次迭代演示:信用整形(credit shaping)叠加在门控之上的发送节奏控制。

直接运行: python demo_iteration.py
纯计算、确定性输出:无 sleep、无网络、无预录结果,实测远低于 90 秒。

场景一(新功能):同一突发流量在“无整形 / 有整形”下的发送节奏对比。
场景二(边界情况):负信用撞上关门 -> 关门期间信用冻结,重开后才继续恢复,
且信用归零时刻是精确分数(1/3 ns 量级),由事件跳跃一次算出。
"""

from fractions import Fraction

from tsn_gate import (
    CreditShaper,
    Frame,
    GateSchedule,
    GateWindow,
    replay,
)


def fmt(t) -> str:
    if isinstance(t, Fraction):
        return f"{float(t):>8.3f} ({t})"
    return f"{t:>8d}"


def show_shaping_effect() -> None:
    print("== 场景一:信用整形控制发送节奏(与无整形对比)==")
    # 队列 0 恒开门,链路 8 bit/ns(1 字节 = 1 ns)。
    # 整形参数:idle_slope=1/ns,send_slope=-3/ns(一正一负,调用方给定)。
    schedule = GateSchedule(
        cycle_ns=100,
        windows=(GateWindow(0, 100, frozenset({0})),),
        link_rate_bits_per_ns=8,
    )
    shapers = (CreditShaper(queue=0, idle_slope=1, send_slope=-3),)
    # 0 时刻突发 5 个 10 字节帧(各 10ns)
    arrivals = [Frame(queue=0, arrive_ns=0, size_bytes=10) for _ in range(5)]

    plain = replay(arrivals, schedule)
    shaped = replay(arrivals, schedule, shapers=shapers)

    print("门表: 周期 100ns, q0 窗 [0,100)(恒开); 整形: idle=+1/ns, send=-3/ns")
    print(f"{'seq':>3} {'到达':>4} {'无整形开始':>8} {'整形后开始':>8}  说明")
    for f0, f1 in zip(plain.frames, shaped.frames):
        note = ""
        if f1.start_ns != f0.start_ns:
            note = f"被信用推迟 {f1.start_ns - f0.start_ns}ns"
        print(f"{f0.seq:>3} {f0.arrive_ns:>4} {f0.start_ns:>8} "
              f"{f1.start_ns:>8}  {note}")
    starts_plain = [f.start_ns for f in plain.frames]
    starts_shaped = [f.start_ns for f in shaped.frames]
    assert starts_plain == [0, 10, 20, 30, 40], starts_plain
    # 每发 10ns 信用 -30,需 30ns 恢复:节奏被拉成每 40ns 一帧
    assert starts_shaped == [0, 40, 80, 120, 160], starts_shaped
    print("结论: 整形后发送节奏被信用拉疏(每 40ns 一帧),无整形则背靠背连发。")


def show_frozen_credit_edge() -> None:
    print()
    print("== 场景二(边界): 负信用撞上关门 -> 冻结,重开后精确恢复 ==")
    # 周期 100ns,q0 窗 [0,50);idle=+2/ns,send=-3/ns。
    schedule = GateSchedule(
        cycle_ns=100,
        windows=(GateWindow(0, 50, frozenset({0})),),
        link_rate_bits_per_ns=8,
    )
    shapers = (CreditShaper(queue=0, idle_slope=2, send_slope=-3),)
    arrivals = [
        Frame(queue=0, arrive_ns=0, size_bytes=10),  # [0,10) 发送 -> 信用 -30
        Frame(queue=0, arrive_ns=0, size_bytes=1),   # 等信用回 0
    ]
    result = replay(arrivals, schedule, shapers=shapers)
    f0, f1 = result.frames

    print("门表: 周期 100ns, q0 窗 [0,50); 整形: idle=+2/ns, send=-3/ns")
    print(f"  f0: 开始 {fmt(f0.start_ns)} 结束 {fmt(f0.end_ns)}  "
          f"-> 信用 0 - 3*10 = -30")
    print("  信用恢复时间线(关门期冻结):")
    print("    [10, 50) 开门: -30 + 2*40 = +50?  不,只需恢复到 0: 15ns 后(时刻 25)归零")
    print("    若 f1 在 25 前未发,则信用停在 0;但 f1 需要信用>=0 且门开:")
    print(f"  f1: 开始 {fmt(f1.start_ns)} 结束 {fmt(f1.end_ns)}  "
          f"等待原因: {[r.value for r in f1.wait_reasons]}")
    # f0 [0,10) -> 信用 -30;开门恢复 2/ns -> 25 时刻归零,f1 立即发
    assert (f0.start_ns, f0.end_ns) == (0, 10)
    assert f1.start_ns == 25 and f1.end_ns == 26, (f1.start_ns, f1.end_ns)

    # 变体 A: idle=1/3 ns -> 恢复需 90ns 开门,恰好跨一个完整关门期,
    # 且归零时刻(150)恰为窗口终点:门已关,须等到下一窗口 200
    print()
    print("  变体 A: idle=1/3 ns -> 恢复需 90ns 开门时间;[10,50) 40ns +")
    print("          [100,150) 50ns,归零恰在窗口终点 150(门已关)-> 等下一窗")
    shapers2 = (CreditShaper(queue=0, idle_slope=Fraction(1, 3), send_slope=-3),)
    r2 = replay(arrivals, schedule, shapers=shapers2)
    g1 = r2.frames[1]
    print(f"  f1: 开始 {fmt(g1.start_ns)} 结束 {fmt(g1.end_ns)}")
    assert g1.start_ns == 200, g1.start_ns

    # 变体 B: 真正的分数归零时刻。窗 [0,20),周期 100;idle=2/3,send=-3/2。
    # f0 [0,10) -> 信用 -15;[10,20) 恢复 20/3 -> -25/3;关门冻结;
    # [100,120) 内还需 (25/3)/(2/3) = 25/2 ns -> 225/2 = 112.5 归零
    print()
    print("  变体 B: 窗 [0,20), idle=2/3 ns, send=-3/2 ns -> 归零在 225/2")
    schedule_b = GateSchedule(
        cycle_ns=100,
        windows=(GateWindow(0, 20, frozenset({0})),),
        link_rate_bits_per_ns=8,
    )
    shapers3 = (CreditShaper(queue=0, idle_slope=Fraction(2, 3),
                             send_slope=Fraction(-3, 2)),)
    r3 = replay(arrivals, schedule_b, shapers=shapers3)
    h1 = r3.frames[1]
    print(f"  f1: 开始 {fmt(h1.start_ns)} 结束 {fmt(h1.end_ns)}")
    assert h1.start_ns == Fraction(225, 2), h1.start_ns
    assert h1.end_ns == Fraction(227, 2), h1.end_ns
    print("结论: 关门期间信用确实冻结;恢复只计开门时长,归零时刻精确到分数。")


def main() -> None:
    show_shaping_effect()
    show_frozen_credit_edge()
    print()
    print("demo_iteration: 全部断言通过。")


if __name__ == "__main__":
    main()
