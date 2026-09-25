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


def reference_replay_switch(frames, old_windows, old_cycle, old_guard,
                            new_windows, new_cycle, new_guard,
                            rate, offset, eff, acc, tick_limit=5_000_000):
    """逐纳秒推进的预定切换参考重放(不导入 tsn_gate)。

    frames:  [(queue, arrive_ns, size_bytes, deadline_ns|None), ...]  下标即到达序号
    old_windows/new_windows: [(start_ns, end_ns, frozenset[int]), ...]  周期内半开区间
    eff: 新表生效参考时刻;acc: 切换请求被接受的参考时刻

    返回 list,下标即帧序号;每项为 (start_ns, end_ns),
    永不可发的帧为 (None, None)。
    """
    n = len(frames)
    dur = [(f[2] * 8 + rate - 1) // rate for f in frames]
    need_old = [d + old_guard for d in dur]
    need_new = [d + new_guard for d in dur]

    maxwin_new = [0] * 4
    for (s, e, qs) in new_windows:
        for q in qs:
            if e - s > maxwin_new[q]:
                maxwin_new[q] = e - s
    fits_new = [need_new[i] <= maxwin_new[frames[i][0]] for i in range(n)]

    def old_ok(i, t):
        p = (t + offset) % old_cycle
        q = frames[i][0]
        return any(q in qs and s <= p and p + need_old[i] <= e
                   for (s, e, qs) in old_windows)

    # 逐纳秒回扫:每帧在旧表下的最晚可行开始时刻
    # (acc 之后开始的旧表发送不得让 end+guard 越过 eff)
    last_old = []
    for i in range(n):
        t_max = max(acc - 1, eff - need_old[i])
        found = None
        t = t_max
        while t >= 0:
            if old_ok(i, t):
                found = t
                break
            t -= 1
        last_old.append(found)

    never = set()
    for i in range(n):
        if not fits_new[i] and (last_old[i] is None or last_old[i] < frames[i][1]):
            never.add(i)

    start = [None] * n
    end = [None] * n
    remaining = {i for i in range(n) if i not in never}
    busy_until = 0
    t = 0
    ticks = 0
    while remaining:
        ticks += 1
        if ticks > tick_limit:
            raise AssertionError("reference_replay_switch: tick 预算耗尽,疑似不死循环")
        # 错过最后旧表开始时刻且容不下新表的帧,此刻证明永不可发
        drop = [i for i in remaining if not fits_new[i] and t > last_old[i]]
        for i in drop:
            never.add(i)
            remaining.discard(i)
        if not remaining:
            break
        if t < busy_until:
            t += 1
            continue
        use_new = t >= eff
        windows = new_windows if use_new else old_windows
        cycle = new_cycle if use_new else old_cycle
        need = need_new if use_new else need_old
        p = (t + offset) % cycle
        best = None
        for i in sorted(remaining):
            q, arrive = frames[i][0], frames[i][1]
            if arrive > t:
                continue
            ok = any(q in qs and s <= p and p + need[i] <= e
                     for (s, e, qs) in windows)
            if ok and not use_new and t >= acc and t + need[i] > eff:
                ok = False  # 不借旧表预约跨过生效时刻的时段
            if ok and (best is None or q < frames[best][0]):
                best = i
        if best is None:
            t += 1
            continue
        start[best] = t
        end[best] = t + dur[best]
        busy_until = end[best]
        remaining.discard(best)
        t = busy_until
    return [(start[i], end[i]) if i not in never else (None, None) for i in range(n)]


def verify_timeline(results, frames, old_windows, old_cycle, old_guard,
                    new_windows, new_cycle, new_guard,
                    rate, offset, eff, acc):
    """对重放输出做逐纳秒可行性扫描(不导入 tsn_gate)。

    校验:发送区间互不重叠;没有凭空提前开始(不早于到达);
    每次开始在其所属配置(旧表/新表)下门控合法;
    accepted 之后的旧表开始不越过生效时刻(含 guard)。
    全部通过返回 True,否则抛 AssertionError。
    """
    dur = [(f[2] * 8 + rate - 1) // rate for f in frames]
    occupied = set()
    for i, (s, e) in enumerate(results):
        if s is None:
            continue
        assert e == s + dur[i], f"帧 {i}: end-start 与序列化时长不符"
        assert s >= frames[i][1], (
            f"帧 {i}: 凭空提前开始(start {s} < arrive {frames[i][1]})"
        )
        for tt in range(s, e):
            assert tt not in occupied, f"帧 {i}: 发送区间在 t={tt} 与其他帧重叠"
            occupied.add(tt)
        q = frames[i][0]
        if s >= eff:
            p = (s + offset) % new_cycle
            ok = any(q in qs and st <= p and p + dur[i] + new_guard <= en
                     for (st, en, qs) in new_windows)
            assert ok, f"帧 {i}: 新表下 t={s} 开始不合法"
        else:
            p = (s + offset) % old_cycle
            ok = any(q in qs and st <= p and p + dur[i] + old_guard <= en
                     for (st, en, qs) in old_windows)
            assert ok, f"帧 {i}: 旧表下 t={s} 开始不合法"
            if s >= acc:
                assert s + dur[i] + old_guard <= eff, (
                    f"帧 {i}: 借旧表预约了跨过生效时刻的时段"
                )
    return True
