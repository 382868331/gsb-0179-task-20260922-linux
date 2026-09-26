"""演示本次迭代:信用整形(CreditShaper)+ 一项边界情况(负信用遇关门冻结)。

直接运行: python demo_iteration.py   (纯计算,无 sleep、无网络、无预录结果)
"""

from tsn_gate import (
    CreditShaper,
    Frame,
    GateSchedule,
    GateWindow,
    TSNGateError,
    replay,
)


def show(shaped_result, plain_result, title):
    print(title)
    print(f"{'seq':>3} {'队列':>4} {'到达':>6} {'整形开始':>8} {'整形结束':>8} "
          f"{'无整形开始':>9} {'等待原因(整形)'}")
    for f, p in zip(shaped_result.frames, plain_result.frames):
        reasons = ",".join(w.value for w in f.wait_reasons) or "-"
        print(f"{f.seq:>3} {f.queue:>4} {f.arrive_ns:>6} "
              f"{str(f.start_ns):>8} {str(f.end_ns):>8} "
              f"{str(p.start_ns):>9}  {reasons}")


def show_credit_shaping() -> None:
    # 门常开;队列 0 整形:idle=1, send=-2, 上下限 +/-20;1 字节 = 1ns
    windows = (GateWindow(0, 100, frozenset({0})),)
    shaper = {0: CreditShaper(idle_slope=1, send_slope=-2,
                              hi_credit=20, lo_credit=-20)}
    shaped = GateSchedule(cycle_ns=100, windows=windows, link_rate_bits_per_ns=8,
                          credit_shapers=shaper)
    plain = GateSchedule(cycle_ns=100, windows=windows, link_rate_bits_per_ns=8)
    # 连续 4 帧各 4 字节:无整形时背靠背发完;整形后每发 4ns 信用 -8,
    # 必须等信用恢复到 0 才能发下一帧 -> 发送节奏被拉开
    arrivals = [Frame(queue=0, arrive_ns=0, size_bytes=4) for _ in range(4)]
    rs = replay(arrivals, shaped)
    rp = replay(arrivals, plain)
    show(rs, rp, "== 新功能:信用整形控制发送节奏(门常开,idle=1 send=-2) ==")
    starts = [f.start_ns for f in rs.frames]
    assert starts == [0, 12, 24, 36], starts
    assert [f.start_ns for f in rp.frames] == [0, 4, 8, 12]
    print("解读: 每帧发 4ns 信用降 8,需 8ns 恢复;整形把发送间隔从 4ns 拉到 12ns")


def show_freeze_boundary() -> None:
    # 边界:负信用碰上关门 -> 本题规定信用冻结,开门后才继续恢复
    windows = (GateWindow(0, 10, frozenset({0})),)
    shaped = GateSchedule(cycle_ns=20, windows=windows, link_rate_bits_per_ns=8,
                          credit_shapers={0: CreditShaper(1, -2, 10, -30)})
    plain = GateSchedule(cycle_ns=20, windows=windows, link_rate_bits_per_ns=8)
    arrivals = [Frame(queue=0, arrive_ns=0, size_bytes=6),
                Frame(queue=0, arrive_ns=5, size_bytes=1)]
    rs = replay(arrivals, shaped)
    rp = replay(arrivals, plain)
    show(rs, rp, "\n== 边界:负信用遇关门冻结(窗口 [0,10) 周期 20) ==")
    f = rs.frames[1]
    assert f.start_ns == 28, f.start_ns
    print("解读: A 发 [0,6) 后信用 -12;[6,10) 积压恢复到 -8;关门 [10,20) 冻结;")
    print("      开门后再恢复 8ns -> B 在 t=28 开始(无整形时 t=6 即可发)")


def show_rejection() -> None:
    print("\n== 拒绝边界:斜率不是一正一负 ==")
    try:
        CreditShaper(idle_slope=1, send_slope=1, hi_credit=5, lo_credit=-5)
    except TSNGateError as exc:
        print(f"  已拒绝 -> {exc}")
    else:
        raise AssertionError("同号斜率未被拒绝,不符合契约")


def main() -> None:
    show_credit_shaping()
    show_freeze_boundary()
    show_rejection()


if __name__ == "__main__":
    main()
