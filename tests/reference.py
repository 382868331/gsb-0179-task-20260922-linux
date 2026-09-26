"""独立参考实现:逐 tick(1 ns)朴素仿真。

故意不导入 tsn_gate,只用普通元组与逐纳秒循环重述题面语义,
用于对事件驱动核心 tsn_gate.replay 做交叉验证。仅适用于小整数时间。
"""

from fractions import Fraction as _Fraction


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


def reference_replay_credit(frames, windows, cycle, rate, guard, offset, shapers,
                            quantum=_Fraction(1, 12), step_limit=20_000_000):
    # 细时间格(固定量子)朴素仿真,用于交叉验证信用整形,不导入 tsn_gate。
    # 每格重述本题语义:积压未发增信用(idle)、本队列发送减信用(send)、
    # 空队列正信用归 0、负信用恢复到 0、关门冻结、信用非负且门开才可开始。
    # 仅适用于所有事件时刻都落在网格上的小整数配置(测试保证斜率整除 12)。
    # shapers: {queue: (idle_slope, send_slope, hi_credit, lo_credit)}
    # 返回 list[(start, end)],永不可发为 (None, None);时刻为精确 Fraction。
    n = len(frames)
    dur = [(f[2] * 8 + rate - 1) // rate for f in frames]
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
    credit = {q: _Fraction(0) for q in shapers}
    busy_until = _Fraction(0)
    active_q = None
    t = _Fraction(0)
    steps = 0

    def open_at(q, tt):
        ph = (tt + offset) % cycle
        return any(q in qs and s <= ph < e for (s, e, qs) in windows)

    while remaining:
        steps += 1
        if steps > step_limit:
            raise AssertionError("reference_replay_credit: 步数预算耗尽,疑似不死循环")
        if t >= busy_until:
            active_q = None
            ph = (t + offset) % cycle
            best = None
            for i in sorted(remaining):
                q, arrive = frames[i][0], frames[i][1]
                if arrive > t:
                    continue
                if q in shapers and credit[q] < 0:
                    continue
                ok = any(q in qs and s <= ph < e and e - ph >= need[i]
                         for (s, e, qs) in windows)
                if ok and (best is None or q < frames[best][0]):
                    best = i
            if best is not None:
                start[best] = t
                end[best] = t + dur[best]
                busy_until = end[best]
                active_q = frames[best][0]
                remaining.discard(best)
        for q, (idle, send, hi, lo) in shapers.items():
            if not open_at(q, t):
                continue  # 关门冻结
            c = credit[q]
            if active_q == q and t < busy_until:
                c = max(_Fraction(lo), c + send * quantum)
            elif any(frames[i][0] == q and frames[i][1] <= t for i in remaining):
                c = min(_Fraction(hi), c + idle * quantum)
            elif c > 0:
                c = _Fraction(0)
            elif c < 0:
                c = min(_Fraction(0), c + idle * quantum)
            credit[q] = c
        t += quantum
    return [(start[i], end[i]) if not never[i] else (None, None) for i in range(n)]
