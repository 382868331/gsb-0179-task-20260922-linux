"""演示:门控错过窗口、预定门表切换、以及实际触发的非法输入拒绝。

直接运行: python demo.py   (纯计算,无 sleep、无网络、无预录结果,秒级完成)
"""

from tsn_gate import (
    Frame,
    FrameStatus,
    GateSchedule,
    GateWindow,
    ScheduledUpdate,
    TSNGateError,
    replay,
)


def _print_frames(result) -> None:
    print(f"{'seq':>3} {'队列':>4} {'到达':>6} {'状态':>22} "
          f"{'开始':>6} {'结束':>6} {'表':>3} {'等待':>5}  等待原因 / deadline")
    for f in result.frames:
        reasons = ",".join(r.value for r in f.wait_reasons) or "-"
        dl = "-"
        if f.deadline_ns is not None:
            dl = f"deadline {f.deadline_ns} " + ("违例!" if f.deadline_violated else "满足")
        start = "-" if f.start_ns is None else str(f.start_ns)
        end = "-" if f.end_ns is None else str(f.end_ns)
        epoch = "-" if f.config_epoch is None else str(f.config_epoch)
        waited = "-" if f.waited_ns is None else str(f.waited_ns)
        print(f"{f.seq:>3} {f.queue:>4} {f.arrive_ns:>6} {f.status.value:>22} "
              f"{start:>6} {end:>6} {epoch:>3} {waited:>5}  {reasons} / {dl}")
    print(f"汇总: 发送 {result.sent_count} 帧, 永不可发 {result.never_count} 帧, "
          f"deadline 违例 seq={list(result.deadline_violations)}")


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

    print("== 场景一(无切换):门控造成的错过窗口 ==")
    print("门表: 周期 1000ns, q0 窗 [0,400), q1 窗 [400,800), guard 50ns, 1 bit/ns")
    _print_frames(result)
    assert result.frames[1].deadline_violated, "演示场景应触发 deadline 违例"
    assert result.frames[2].status is FrameStatus.NEVER_TRANSMITTABLE


def show_switch_case() -> None:
    print()
    print("== 场景二:预定门表切换(effective_ns=200,恰为旧表周期边界) ==")
    # rate=8 bit/ns => 1 字节恰好 1ns;周期 100ns;guard=5ns;无时钟偏差。
    # 旧表:q0 窗 [0,40),  q1 窗 [60,100)
    # 新表:q0 窗 [0,80),  q1 窗 [80,100)
    old_schedule = GateSchedule(
        cycle_ns=100,
        windows=(GateWindow(0, 40, frozenset({0})),
                 GateWindow(60, 100, frozenset({1}))),
        link_rate_bits_per_ns=8,
        guard_ns=5,
        clock_offset_ns=0,
    )
    new_schedule = GateSchedule(
        cycle_ns=100,
        windows=(GateWindow(0, 80, frozenset({0})),
                 GateWindow(80, 100, frozenset({1}))),
        link_rate_bits_per_ns=8,
        guard_ns=5,
        clock_offset_ns=0,
    )
    arrivals = [
        # seq0 q0 50B:旧窗 40ns 容不下(need=55),旧表 alone 会判永不可发;
        # 新窗 80ns 容得下 -> 旧结论重算,不丢帧,200 起按新表发送 [200,250)
        Frame(queue=0, arrive_ns=100, size_bytes=50),
        # seq1 q1 35B:150 到达,旧 q1 窗 [160,200),35+guard5=40 恰好顶到 200,
        # 允许在切换前连同 guard 完成 -> [160,195),epoch 0
        Frame(queue=1, arrive_ns=150, size_bytes=35),
        # seq2 q0 10B:恰在切换边界 200 到达,按新表判断;但同队列 seq0
        # 排队在前先占用 [200,250),seq2 在 [250,260) 发送(排队顺序不变)
        Frame(queue=0, arrive_ns=200, size_bytes=10),
        # seq3 q0 90B:两张表的任何窗口都容不下 -> 永不可发
        Frame(queue=0, arrive_ns=0, size_bytes=90),
        # seq4 q1 30B:与 seq1 同到,竞争失败;195 时旧窗只剩 5ns,
        # 不能借旧表预约 [260,300) 那个切换后的旧实例;新 q1 窗仅 20ns
        # 也容不下 need=35 -> 切换发生后才被证明永不可发(等过,不是预丢)
        Frame(queue=1, arrive_ns=150, size_bytes=30),
    ]
    result = replay(
        arrivals, old_schedule,
        scheduled_update=ScheduledUpdate(new_schedule, effective_ns=200),
    )
    print("旧表: q0 [0,40) q1 [60,100)  ->  新表(200起): q0 [0,80) q1 [80,100),"
          " guard 5ns, 8 bit/ns")
    _print_frames(result)

    f = result.frames
    assert (f[0].start_ns, f[0].end_ns, f[0].config_epoch) == (200, 250, 1)
    assert (f[1].start_ns, f[1].end_ns, f[1].config_epoch) == (160, 195, 0)
    assert (f[2].start_ns, f[2].end_ns, f[2].config_epoch) == (250, 260, 1)
    assert f[3].status is FrameStatus.NEVER_TRANSMITTABLE
    assert f[4].status is FrameStatus.NEVER_TRANSMITTABLE
    assert f[4].wait_reasons  # 动态证明:确实等过(竞争/窗不足),而非预先丢弃
    print("断言全部成立:无重叠发送、边界帧按新表、旧'永不可发'结论已重算、"
          "在途帧旧表完成、未借旧表预约切换后时段。")


def show_rejection_case() -> None:
    print()
    print("== 拒绝边界:非法输入被可定位地拒绝 ==")
    schedule = GateSchedule(
        cycle_ns=100,
        windows=(GateWindow(0, 10, frozenset({0})),),
        link_rate_bits_per_ns=8,
    )
    new_schedule = GateSchedule(
        cycle_ns=100,
        windows=(GateWindow(0, 20, frozenset({0})),),
        link_rate_bits_per_ns=8,
    )
    bad_calls = [
        ("队列号为 bool", lambda: replay(
            [Frame(queue=True, arrive_ns=0, size_bytes=1)], schedule)),
        ("到达时间为 NaN", lambda: replay(
            [Frame(queue=0, arrive_ns=float("nan"), size_bytes=1)], schedule)),
        ("队列号越界(>3)", lambda: replay(
            [Frame(queue=4, arrive_ns=0, size_bytes=1)], schedule)),
        ("切换时刻未落在旧表周期边界", lambda: replay(
            [], schedule, scheduled_update=ScheduledUpdate(new_schedule, 50))),
    ]
    for label, call in bad_calls:
        try:
            call()
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
