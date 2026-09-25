"""演示:单表门控错过窗口、预定门表切换、非法输入拒绝。

直接运行: python demo.py   (纯计算,无 sleep、无网络、无预录结果)
"""

from tsn_gate import (
    ConfigChange,
    Frame,
    FrameStatus,
    GateSchedule,
    GateWindow,
    TSNGateError,
    replay,
)


def print_frames(result) -> None:
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
    print_frames(result)
    print(f"汇总: 发送 {result.sent_count} 帧, 永不可发 {result.never_count} 帧, "
          f"deadline 违例 seq={list(result.deadline_violations)}")
    assert result.frames[1].deadline_violated, "演示场景应触发 deadline 违例"
    assert result.frames[2].status is FrameStatus.NEVER_TRANSMITTABLE


def show_switch_case() -> None:
    # 旧表:q0 [0,400), q1 [400,800);新表:q0 [0,200), q1 [500,1000);
    # 周期均为 1000ns,guard 50ns,1 bit/ns;生效时刻 2000(旧周期边界),
    # 请求在 1200 被接受。
    old = GateSchedule(
        cycle_ns=1000,
        windows=(
            GateWindow(0, 400, frozenset({0})),
            GateWindow(400, 800, frozenset({1})),
        ),
        link_rate_bits_per_ns=1,
        guard_ns=50,
        clock_offset_ns=0,
    )
    new = GateSchedule(
        cycle_ns=1000,
        windows=(
            GateWindow(0, 200, frozenset({0})),
            GateWindow(500, 1000, frozenset({1})),
        ),
        link_rate_bits_per_ns=1,
        guard_ns=50,
        clock_offset_ns=0,
    )
    sw = ConfigChange(schedule=new, effective_ns=2000, accepted_ns=1200)
    arrivals = [
        Frame(queue=0, arrive_ns=1100, size_bytes=20),  # seq0: 在途帧,继续完成
        Frame(queue=0, arrive_ns=1150, size_bytes=5),   # seq1: 接受后旧表可发
        Frame(queue=0, arrive_ns=1300, size_bytes=30),  # seq2: 仅旧表容得下,已无可行开始
        Frame(queue=1, arrive_ns=300, size_bytes=55),   # seq3: 旧表永不可发,新表救回
        Frame(queue=1, arrive_ns=2000, size_bytes=10),  # seq4: 恰在边界到达,按新表
        Frame(queue=0, arrive_ns=1950, size_bytes=20),  # seq5: 旧表来不及,新表容不下
    ]
    result = replay(arrivals, old, config_change=sw)

    print()
    print("== 预定门表切换:生效时刻 2000(旧周期边界),请求 1200 被接受 ==")
    print("旧表: q0 [0,400), q1 [400,800)   新表: q0 [0,200), q1 [500,1000)")
    print_frames(result)
    print(f"汇总: 发送 {result.sent_count} 帧, 永不可发 {result.never_count} 帧")
    notes = [
        "seq0: 1100 开始(请求接受前),在途跨越接受时刻,继续发送到 1260 完成",
        "seq1: 接受后按旧表发送,end+guard=1350 不超过生效时刻,允许",
        "seq2: 仅旧表容得下(need 290 > 新窗 200),最晚可行旧表开始 1110 已错过 -> 永不可发",
        "seq3: 旧表任何窗口容不下(need 490 > 400),重算后新表可发 -> 2500 发送,未丢帧",
        "seq4: 恰在生效时刻 2000 到达,按新表判断(旧表本可 2400 发,新表 3500 才开门)",
        "seq5: 旧表最晚可行开始 1190 已错过,新表窗口 200 容不下 need 210 -> 永不可发",
    ]
    for line in notes:
        print("  " + line)
    f = result.frames
    assert (f[0].start_ns, f[0].end_ns) == (1100, 1260)
    assert (f[1].start_ns, f[1].end_ns) == (1260, 1300)
    assert f[2].status is FrameStatus.NEVER_TRANSMITTABLE
    assert (f[3].start_ns, f[3].end_ns) == (2500, 2940)
    assert (f[4].start_ns, f[4].end_ns) == (3500, 3580)
    assert f[5].status is FrameStatus.NEVER_TRANSMITTABLE


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

    new = GateSchedule(
        cycle_ns=100,
        windows=(GateWindow(0, 50, frozenset({0})),),
        link_rate_bits_per_ns=8,
    )
    bad_changes = [
        ("生效时刻未落在旧周期边界",
         ConfigChange(schedule=new, effective_ns=150)),
        ("新表时钟偏差与旧表不一致",
         ConfigChange(schedule=GateSchedule(
             cycle_ns=100, windows=(GateWindow(0, 50, frozenset({0})),),
             link_rate_bits_per_ns=8, clock_offset_ns=1),
             effective_ns=100)),
    ]
    for label, sw in bad_changes:
        try:
            replay([Frame(queue=0, arrive_ns=0, size_bytes=1)], schedule,
                   config_change=sw)
        except TSNGateError as exc:
            print(f"  {label}: 已拒绝 -> {exc}")
        else:
            raise AssertionError(f"{label}: 未被拒绝,不符合契约")


def main() -> None:
    show_normal_case()
    show_switch_case()
    show_rejection_case()


if __name__ == "__main__":
    main()
