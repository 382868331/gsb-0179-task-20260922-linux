"""独立参考实现:逐 tick(1 ns)朴素仿真。

故意不导入 tsn_gate,只用普通元组与逐纳秒循环重述题面语义,
用于对事件驱动核心 tsn_gate.replay 做交叉验证。仅适用于小整数时间。
"""


def _dur(size, rate):
    return (size * 8 + rate - 1) // rate  # ceil(比特数/速率)


def _maxwin(windows):
    m = [0] * 4
    for (s, e, qs) in windows:
        for q in qs:
            if e - s > m[q]:
                m[q] = e - s
    return m


def reference_replay(frames, windows, cycle, rate, guard, offset, tick_limit=5_000_000,
                     update=None):
    """逐纳秒推进的参考重放。

    frames:  [(queue, arrive_ns, size_bytes, deadline_ns|None), ...]  下标即到达序号
    windows: [(start_ns, end_ns, frozenset[int]), ...]  旧表周期内半开区间
    cycle/rate/guard/offset: 与 tsn_gate.GateSchedule 同义(旧表)
    update: None,或 (new_windows, new_cycle, new_rate, new_guard,
                     new_offset, effective_ns)
            逐 tick 语义:t < effective_ns 用旧表,t >= effective_ns 用新表;
            旧表下开始要求 t+时长+guard <= effective_ns;恰在边界到达按新表;
            两张表几何上都永不可发的帧为 (None,None);只在旧前缀可发、
            新表永不可发且切换时仍未开始的帧,在 t>=effective_ns 后定论为
            (None, None)(动态结论)。

    返回 list,下标即帧序号;每项为 (start_ns, end_ns),永不可发为 (None, None)。
    """
    n = len(frames)
    if update is None:
        new_windows, new_cycle, new_rate, new_guard, new_off, eff = (
            windows, cycle, rate, guard, offset, None)
    else:
        new_windows, new_cycle, new_rate, new_guard, new_off, eff = update

    dur = [_dur(f[2], rate) for f in frames]
    need = [d + guard for d in dur]
    ndur = [_dur(f[2], new_rate) for f in frames]
    nneed = [d + new_guard for d in ndur]

    maxwin = _maxwin(windows)
    nmaxwin = _maxwin(new_windows)
    new_fits_geom = [nneed[i] <= nmaxwin[frames[i][0]] for i in range(n)]

    def old_prefix_geom(i):
        """旧表 [0, eff) 有限前缀里是否存在几何合法开始时刻。

        独立于核心的写法:逐周期实例枚举(小整数时间,实例数很少),
        每个实例只做区间非空判断,不使用核心那套闭式 k 上下界。
        """
        if eff is None:
            return need[i] <= maxwin[frames[i][0]]
        a = max(frames[i][1], 0)
        c, off = cycle, offset
        # 与 [0, eff) 可能有交集的实例 k(宽松边界,小整数下枚举量很小)
        klo = -abs(off) // c - 2
        khi = (eff + abs(off)) // c + 2
        for k in range(klo, khi + 1):
            for (s, e, qs) in windows:
                if frames[i][0] not in qs or e - s < need[i]:
                    continue
                ws = k * c + s - off          # 实例在参考时钟上的 [ws, we)
                we = k * c + e - off
                lo = max(a, ws, 0)
                hi = min(we, eff)             # 含 guard 必须在切换前完成
                if lo + need[i] <= hi:
                    return True
        return False

    if eff is None:
        geom = [need[i] <= maxwin[frames[i][0]] for i in range(n)]
    else:
        geom = [new_fits_geom[i] or old_prefix_geom(i) for i in range(n)]

    start = [None] * n
    end = [None] * n
    decided_never = [not g for g in geom]
    remaining = {i for i in range(n) if geom[i]}
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
        use_new = eff is not None and t >= eff
        # 恰在边界(及之后):新表容不下的帧在此刻被证明永不可发,离开等待集
        if use_new:
            drop = {i for i in remaining if not new_fits_geom[i]}
            for i in drop:
                decided_never[i] = True
            remaining -= drop
            if not remaining:
                break
        wins = new_windows if use_new else windows
        cyc = new_cycle if use_new else cycle
        off = new_off if use_new else offset
        nd = nneed if use_new else need
        phase = (t + off) % cyc
        best = None
        for i in sorted(remaining):
            q, arrive = frames[i][0], frames[i][1]
            if arrive > t:
                continue
            fits = False
            for (s, e, qs) in wins:
                if q in qs and s <= phase < e and e - phase >= nd[i]:
                    fits = True
                    break
            if fits and not use_new and eff is not None and t + nd[i] > eff:
                fits = False  # 旧表不得预约切换点之后的时段
            if fits and (best is None or q < frames[best][0]):
                best = i  # 已按序号升序扫描,只在队列号更小时替换
        if best is None:
            t += 1
            continue
        d = ndur[best] if use_new else dur[best]
        start[best] = t
        end[best] = t + d
        busy_until = end[best]
        remaining.discard(best)
        t = busy_until
    return [(None, None) if decided_never[i] else (start[i], end[i])
            for i in range(n)]
