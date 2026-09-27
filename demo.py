"""演示:门控造成的错过窗口 + 一个实际触发的非法输入拒绝。

直接运行: python demo.py   (纯计算,无 sleep、无网络、无预录结果)
"""

from tsn_gate import (
    Frame,
    FrameStatus,
    GateSchedule,
    GateWindow,
    TSNGateError,
    replay,
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
        if f.wait_segments:
            line = "; ".join(
                f"{s.reason.value}[{s.start_ns},{s.end_ns})={s.duration_ns}ns"
                for s in f.wait_segments
            )
            print(f"      等待时间线(连续,无空档): {line}")
            print(f"      汇总: {dict((r.value, v) for r, v in f.wait_breakdown.items())}")
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


def main() -> None:
    show_normal_case()
    show_rejection_case()


if __name__ == "__main__":
    main()
