"""独立参考实现:逐 tick(1 ns)朴素仿真。

故意不导入 tsn_gate,只用普通元组与逐纳秒循环重述题面语义,
用于对事件驱动核心 tsn_gate.replay 做交叉验证。仅适用于小整数时间。
"""


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


def reference_wait_segments(frames, windows, cycle, rate, guard, offset,
                            tick_limit=5_000_000):
    """逐纳秒参考的等待时间线。

    返回 list,下标即帧序号;每项为若干 (reason, start_ns, end_ns) 半开区间段,
    reason 取 "gate-closed" / "insufficient-window" / "link-busy"。
    每个 tick 内等待帧的原因按如下朴素规则判定:链路被占则 link-busy;
    否则该队列门开着但剩余窗口容不下 时长+guard 则 insufficient-window;
    否则 gate-closed。被选中发送的 tick 对其余已到达帧记 link-busy
    (与核心中“裁决瞬间后链路即被占满”的语义一致)。
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

    segs = [[] for _ in range(n)]

    def rec(i, reason, t0, t1):
        if t1 <= t0:
            return
        if segs[i] and segs[i][-1][0] == reason and segs[i][-1][2] == t0:
            segs[i][-1] = (reason, segs[i][-1][1], t1)
        else:
            segs[i].append((reason, t0, t1))

    remaining = {i for i in range(n) if not never[i]}
    busy_until = 0
    t = 0
    ticks = 0
    while remaining:
        ticks += 1
        if ticks > tick_limit:
            raise AssertionError("reference_wait_segments: tick 预算耗尽")
        if t < busy_until:
            for i in remaining:
                if frames[i][1] <= t:
                    rec(i, "link-busy", t, t + 1)
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
                    if best is None or q < frames[best][0]:
                        best = i
                    break
        if best is None:
            for i in remaining:
                if frames[i][1] > t:
                    continue
                q = frames[i][0]
                open_now = any(q in qs and s <= phase < e
                               for (s, e, qs) in windows)
                rec(i, "insufficient-window" if open_now else "gate-closed",
                    t, t + 1)
            t += 1
            continue
        # 选中后 t 直接跳到 busy_until(与事件驱动核心一致,中间 tick 不再进入
        # busy 分支),因此其余已到达帧记整段;占用期间到达的帧从其到达时刻起记
        end = t + dur[best]
        for i in remaining:
            if i != best:
                lo = max(t, frames[i][1])
                if lo < end:
                    rec(i, "link-busy", lo, end)
        busy_until = end
        remaining.discard(best)
        t = busy_until
    return segs
