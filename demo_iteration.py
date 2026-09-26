"""帧抢占迭代演示:紧急帧延迟降低 vs 普通帧片段开销的取舍。

场景:周期 1000ns,队列 0 窗口 [0,600),链路 8 bit/ns(1 字节 = 1ns)。
普通帧 100B 在 0ns 到达;紧急帧 10B 在 55ns 到达,deadline 90ns。
  * 不启用抢占:紧急帧要等整个 100B 帧发完(100ns),错过 deadline。
  * 启用抢占(片段 20B + 每片 4B 开销):紧急帧在下一片段边界 72ns 发出,
    满足 deadline;代价是普通帧多出 5 个片段头共 20ns 线上开销。
另演示一项边界情况:门在帧发送中途关闭——已开始的片段不被截断,
但不能再开新片段,剩余字节等下一周期窗口续传。
每一步都用 tests/reference_preempt 的逐字节参照独立核对链路占用
(无重叠、无重复字节、无凭空空闲)。

直接运行: python demo_iteration.py   (纯计算,无 sleep、无网络、无预录结果)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tests"))

from reference_preempt import verify_occupancy  # noqa: E402

from tsn_gate import (  # noqa: E402
    Frame,
    GateSchedule,
    GateWindow,
    PreemptionConfig,
    TrafficClass,
    replay,
)

WINDOWS = (GateWindow(0, 600, {0}),)
ARRIVALS = [
    Frame(queue=0, arrive_ns=0, size_bytes=100),                              # 普通大帧
    Frame(queue=0, arrive_ns=55, size_bytes=10, deadline_ns=90,
          traffic_class=TrafficClass.EXPRESS),                                # 紧急帧
]
FRAMES_META = [(0, 0, 100, False), (0, 55, 10, True)]
WIN_META = [(0, 600, frozenset({0}))]


def show_frame(f):
    frags = " ".join(f"[{s},{e})" for s, e in f.fragments) or "-"
    dl = "-" if f.deadline_ns is None else (
        f"deadline {f.deadline_ns} " + ("违例!" if f.deadline_violated else "满足"))
    print(f"  seq{f.seq} 到达 {f.arrive_ns:>3}ns  开始 {f.start_ns:>4}  "
          f"结束 {f.end_ns:>4}  等待 {f.waited_ns:>3}ns  "
          f"片段开销 {f.overhead_ns:>2}ns  {dl}")
    print(f"       片段: {frags}")


def show_tradeoff():
    print("== 取舍演示:紧急帧延迟 vs 普通帧片段开销 ==")
    print("门表: 周期 1000ns, q0 窗 [0,600), 8 bit/ns; "
          "普通帧 100B@0ns, 紧急帧 10B@55ns (deadline 90ns)")

    old = replay(ARRIVALS, GateSchedule(
        cycle_ns=1000, windows=WINDOWS, link_rate_bits_per_ns=8))
    print("\n-- 不启用抢占(保留上一迭代原结果) --")
    for f in old.frames:
        show_frame(f)
    assert (old.frames[1].start_ns, old.frames[1].end_ns) == (100, 110)
    assert old.frames[1].deadline_violated

    new = replay(ARRIVALS, GateSchedule(
        cycle_ns=1000, windows=WINDOWS, link_rate_bits_per_ns=8,
        preemption=PreemptionConfig(fragment_bytes=20,
                                    fragment_overhead_bytes=4)))
    print("\n-- 启用抢占(片段 20B + 每片 4B 开销) --")
    for f in new.frames:
        show_frame(f)
    assert (new.frames[1].start_ns, new.frames[1].end_ns) == (72, 82)
    assert not new.frames[1].deadline_violated
    assert new.frames[0].preempted_count == 1
    assert new.frames[0].overhead_ns == 20

    verify_occupancy([f.fragments for f in new.frames], FRAMES_META, WIN_META,
                     cycle=1000, rate=8, guard=0, offset=0,
                     frag_bytes=20, overhead_bytes=4)
    print("\n逐字节参照核对:链路占用无重叠、无重复字节、无凭空空闲 [通过]")
    print(f"结论: 紧急帧等待 45ns -> 17ns(deadline 由违例变为满足);")
    print(f"      代价: 普通帧线上时长 100ns -> 120ns(+20ns 片段开销), "
          f"结束 100ns -> 130ns。")


def show_gate_close_boundary():
    print()
    print("== 边界情况:门在帧发送中途关闭 ==")
    print("门表: 周期 1000ns, q0 窗 [0,50), 片段 20B 无开销; 普通帧 60B@0ns")
    schedule = GateSchedule(
        cycle_ns=1000, windows=(GateWindow(0, 50, {0}),),
        link_rate_bits_per_ns=8,
        preemption=PreemptionConfig(fragment_bytes=20))
    result = replay([Frame(queue=0, arrive_ns=0, size_bytes=60)], schedule)
    f = result.frames[0]
    show_frame(f)
    # 片段 [20,40) 完整发出(不被截断);40ns 时剩余窗口 10ns 容不下 20ns 片段,
    # 不能再开新片段;最后 20B 等下一周期窗口 [1000,1050) 续传
    assert f.fragments == ((0, 20), (20, 40), (1000, 1020))
    verify_occupancy([f.fragments], [(0, 0, 60, False)],
                     [(0, 50, frozenset({0}))], cycle=1000, rate=8, guard=0,
                     offset=0, frag_bytes=20, overhead_bytes=0)
    print("逐字节参照核对:片段未跨门关闭点,续传等待合法窗口 [通过]")


def main():
    show_tradeoff()
    show_gate_close_boundary()


if __name__ == "__main__":
    main()

