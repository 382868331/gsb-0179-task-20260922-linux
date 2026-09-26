"""独立参考实现:逐 tick(1 ns)朴素仿真。

故意不导入 tsn_gate,只用普通元组与逐纳秒循环重述题面语义,
用于对事件驱动核心 tsn_gate.replay 做交叉验证。仅适用于小整数时间。
"""

import math
from fractions import Fraction


def reference_replay(frames, windows, cycle, rate, guard, offset, tick_limit=5_000_000):
    """逐纳秒推进的参考重放。

    frames:  [(queue, arrive_ns, size_bytes, deadline_ns|None), ...]  下标即到达序号
    windows: [(start_ns, end_ns, frozenset[int]), ...]  周期内半开区间
    cycle/rate/guard/offset: 与 tsn_gate.GateSchedule 同义

    返回 list,下标即帧序号;每项为 (start_ns, end_ns),
    永不可发的帧为 (None, None)。
    """
    n = len(frames)
    dur = [(f[2] * 8 + rate - 1) // rate for f in frames]  # ceil(比特数/速率)
    need = [d + guard for d in dur]

    maxwin = [0] * 4
    for (s, e, qs) in windows:
        for q in qs:
            if e - s > maxwin[q]:
                maxwin[q] = e - s
    never = [need[i] > maxwin[frames[i][0]] for i in range(n)]

    start = [None] * n
    end = [None] * n
    remaining = {i for i in range(n) if not never[i]}
    busy_until = 0
    t = 0
    ticks = 0
    while remaining:
        ticks += 1
        if ticks > tick_limit:
            raise AssertionError("reference_replay: tick 预算耗尽,疑似不死循环")
        if t < busy_until:
            t += 1
            continue
        phase = (t + offset) % cycle
        best = None
        for i in sorted(remaining):
            q, arrive = frames[i][0], frames[i][1]
            if arrive > t:
                continue
            for (s, e, qs) in windows:
                if q in qs and s <= phase < e and e - phase >= need[i]:
                    # 已按序号升序扫描,仅当队列号更小才替换,保证 (队列, 序号) 优先
                    if best is None or q < frames[best][0]:
                        best = i
                    break
        if best is None:
            t += 1
            continue
        start[best] = t
        end[best] = t + dur[best]
        busy_until = end[best]
        remaining.discard(best)
        t = busy_until
    return [(start[i], end[i]) if not never[i] else (None, None) for i in range(n)]


def reference_replay_shaped(frames, windows, cycle, rate, guard, offset, shapers,
                            tick_limit=20_000_000):
    """细时间格参考:带信用整形的朴素逐步仿真,与 tsn_gate 无任何共享代码。

    frames/windows/cycle/rate/guard/offset: 与 reference_replay 同义。
    shapers: {queue: (idle_slope, send_slope, hi_credit|None, lo_credit|None)},
             斜率/上下限为 int 或 Fraction,idle>0>send。

    基准时间格为 1ns;在每个格内若出现信用归零、门边沿(整数时刻)、
    链路空闲等状态变化点,则用精确分数把该格截短到变化点(子步),
    步内信用按斜率解析积分并处理上下限钳制,不做任何浮点近似。

    返回 list,下标即帧序号;每项为 (start, end)(int 或 Fraction),
    永不可发的帧为 (None, None)。
    """
    n = len(frames)
    dur = [(f[2] * 8 + rate - 1) // rate for f in frames]
    need = [d + guard for d in dur]

    maxwin = [0] * 4
    for (s, e, qs) in windows:
        for q in qs:
            if e - s > maxwin[q]:
                maxwin[q] = e - s
    never = [need[i] > maxwin[frames[i][0]] for i in range(n)]

    sh = {}
    for q, (idle, send, hi, lo) in shapers.items():
        idle, send = Fraction(idle), Fraction(send)
        hi = None if hi is None else Fraction(hi)
        lo = None if lo is None else Fraction(lo)
        sh[q] = (idle, send, hi, lo)

    credit = {q: Fraction(0) for q in sh}
    xmit_until = {q: Fraction(0) for q in sh}
    start = [None] * n
    end = [None] * n
    remaining = {i for i in range(n) if not never[i]}
    busy_until = Fraction(0)
    t = Fraction(0)
    ticks = 0
    while remaining:
        ticks += 1
        if ticks > tick_limit:
            raise AssertionError("reference_replay_shaped: 时间格预算耗尽,疑似不死循环")

        # 空队列正信用归 0:发送恰在本格点结束、且更早已无等待帧时立即清零
        # (与本格点恰好到达的新帧无关——到达在清零之后才进入积压)
        for q in sh:
            if xmit_until[q] == t and credit[q] > 0:
                if not any(frames[i][0] == q and frames[i][1] < t for i in remaining):
                    credit[q] = Fraction(0)

        # 决策(链路空闲时):到达 + 窗口容得下 + 信用非负,按 (队列, 序号) 选
        if t >= busy_until:
            phase = (t + offset) % cycle
            best = None
            for i in sorted(remaining):
                q, arrive = frames[i][0], frames[i][1]
                if arrive > t:
                    continue
                ok = any(q in qs and s <= phase < e and e - phase >= need[i]
                         for (s, e, qs) in windows)
                if not ok:
                    continue
                if q in sh and credit[q] < 0:
                    continue
                if best is None or q < frames[best][0]:
                    best = i
            if best is not None:
                start[best] = t
                end[best] = t + dur[best]
                busy_until = end[best]
                if frames[best][0] in sh:
                    xmit_until[frames[best][0]] = end[best]
                remaining.discard(best)

        # 本步长度:基准 1ns,截短到最近的状态变化点(整数边界/链路空闲/信用归零)
        dt = Fraction(1)
        nxt_int = math.floor(t) + 1
        if Fraction(nxt_int) > t:
            dt = min(dt, Fraction(nxt_int) - t)
        if busy_until > t:
            dt = min(dt, busy_until - t)
        for q, (idle, send, hi, lo) in sh.items():
            phase = (t + offset) % cycle
            open_now = any(q in qs and s <= phase < e for (s, e, qs) in windows)
            if open_now and credit[q] < 0 and xmit_until[q] <= t:
                cross = -credit[q] / idle
                if 0 < cross < dt:
                    dt = cross

        # 信用在 [t, t+dt) 上演化;门关闭段冻结(步内不跨整数边界,门状态恒定)
        for q, (idle, send, hi, lo) in sh.items():
            phase = (t + offset) % cycle
            if not any(q in qs and s <= phase < e for (s, e, qs) in windows):
                continue
            c = credit[q]
            if xmit_until[q] > t:
                c += send * dt
                if lo is not None and c < lo:
                    c = lo
            elif any(frames[i][0] == q and frames[i][1] <= t for i in remaining):
                c += idle * dt
                if hi is not None and c > hi:
                    c = hi
            elif c < 0:
                c += idle * dt
                if c > 0:
                    c = Fraction(0)
            credit[q] = c
        t += dt
    return [(start[i], end[i]) if not never[i] else (None, None) for i in range(n)]
