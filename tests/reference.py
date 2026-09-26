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


def reference_preemptive(frames, windows, cycle, rate, guard, offset,
                         express_queues, frag_size, frag_overhead,
                         tick_limit=5_000_000):
    """逐纳秒推进的抢占模式参考重放(不导入 tsn_gate,独立重述语义)。

    frames:  [(queue, arrive_ns, size_bytes, deadline_ns|None), ...]
    windows: [(start_ns, end_ns, frozenset[int]), ...]

    返回 (result, occupancy):
    - result[i] = (first_start, last_end, [(frag_start, frag_end), ...]),
      永不可发为 (None, None, [])。
    - occupancy: dict[t] = (frame_idx, kind, byte_index),kind 为
      "payload" 或 "overhead",byte_index 为该片内字节下标。
    """
    n = len(frames)
    q_of = [f[0] for f in frames]
    arrive = [f[1] for f in frames]
    size = [f[2] for f in frames]
    is_exp = [q in express_queues for q in q_of]

    qwin = [[] for _ in range(4)]
    for (s, e, qs) in windows:
        for q in qs:
            qwin[q].append((s, e))
    maxwin = [max((e - s for s, e in ws), default=0) for ws in qwin]
    never = [maxwin[q] <= guard for q in q_of]

    remaining = list(size)
    frags = [[] for _ in range(n)]
    first = [None] * n
    last = [None] * n
    done = [False] * n
    paused = []
    exp_ready = []
    new_ready = []
    order = sorted(range(n), key=lambda i: (arrive[i], i))
    pi = 0
    active = -1
    a_start = a_end = a_ov = 0
    occ = {}
    t = 0
    ticks = 0
    while not all(done[i] or never[i] for i in range(n)):
        ticks += 1
        if ticks > tick_limit:
            raise AssertionError("reference_preemptive: tick 预算耗尽,疑似不死循环")
        # 1) 片段/帧完成
        if active >= 0 and t >= a_end:
            i = active
            active = -1
            if is_exp[i] or remaining[i] == 0:
                done[i] = True
            elif i not in paused:
                paused.append(i)
        # 2) 到达
        while pi < n and arrive[order[pi]] <= t:
            i = order[pi]
            pi += 1
            if not never[i]:
                (exp_ready if is_exp[i] else new_ready).append(i)
        # 链路忙:逐纳秒传输当前片段的一个字节区间
        if active >= 0:
            byte_idx = ((t - a_start) * rate) // 8
            kind = "overhead" if byte_idx < a_ov else "payload"
            occ[t] = (active, kind, byte_idx)
            t += 1
            continue
        if not exp_ready and not paused and not new_ready:
            if pi >= n:
                break
            t = max(t, arrive[order[pi]])
            continue
        phase = (t + offset) % cycle

        def startable(q):
            return any(s <= phase < e and e - phase > guard for s, e in qwin[q])

        chosen = -1
        cands = [i for i in exp_ready if startable(q_of[i])]
        if cands:
            chosen = min(cands, key=lambda i: (arrive[i], i))
            exp_ready.remove(chosen)
        else:
            ps = [i for i in paused if startable(q_of[i])]
            if ps:
                chosen = min(ps, key=lambda i: first[i])
                paused.remove(chosen)
            else:
                ns_ = [i for i in new_ready if startable(q_of[i])]
                if ns_:
                    chosen = min(ns_, key=lambda i: (q_of[i], i))
                    new_ready.remove(chosen)
        if chosen < 0:
            t += 1
            continue
        i = chosen
        if is_exp[i]:
            ov = 0
            unit = size[i]
        else:
            ov = frag_overhead if frags[i] else 0
            chunk = min(frag_size, remaining[i])
            unit = chunk + ov
            remaining[i] -= chunk
        d = (unit * 8 + rate - 1) // rate
        frags[i].append((t, t + d))
        if first[i] is None:
            first[i] = t
        last[i] = t + d
        active = i
        a_start = t
        a_end = t + d
        a_ov = ov
    result = [
        (None, None, []) if never[i] else (first[i], last[i], frags[i])
        for i in range(n)
    ]
    return result, occ


def verify_occupancy(frames, frags_per_frame, windows, cycle, rate, guard,
                     offset, express_queues, frag_size, frag_overhead):
    """核验核心输出的片段时间线:链路占用无重叠、无重复/丢失字节、无凭空空闲。

    frames: [(queue, arrive_ns, size_bytes, deadline|None), ...]
    frags_per_frame: 核心给出的每帧 [(start,end), ...](永不可发帧为 None)。
    不调用 tsn_gate;发现问题抛 AssertionError。
    """
    occ = {}
    for i, fr in enumerate(frags_per_frame):
        if fr is None:
            continue
        q, _a, size = frames[i][0], frames[i][1], frames[i][2]
        for (s, e) in fr:
            assert e > s, f"帧 {i}: 片段时长非正 ({s},{e})"
            for t in range(s, e):
                if t in occ:
                    raise AssertionError(
                        f"重叠发送: t={t} 同时属于帧 {occ[t]} 与帧 {i}")
                occ[t] = i
        if q in express_queues:
            assert len(fr) == 1, f"紧急帧 {i} 不应被分片"
            want = (size * 8 + rate - 1) // rate
            s, e = fr[0]
            assert e - s == want, f"紧急帧 {i} 时长 {e - s} != {want}"
        else:
            remaining = size
            for k, (s, e) in enumerate(fr):
                chunk = min(frag_size, remaining)
                ov = frag_overhead if k > 0 else 0
                want = ((chunk + ov) * 8 + rate - 1) // rate
                assert e - s == want, (
                    f"帧 {i} 第 {k + 1} 片时长 {e - s} != {want}"
                    f"(载荷 {chunk}B + 开销 {ov}B)")
                remaining -= chunk
            assert remaining == 0, (
                f"帧 {i}: 字节丢失或重复,剩余 {remaining} 字节未发送")
        # 每片启动都必须满足自己的门(门开且剩余窗口 > guard)
        for (s, _e) in fr:
            phase = (s + offset) % cycle
            ok = any(q in qs and ws <= phase < we and we - phase > guard
                     for (ws, we, qs) in windows)
            assert ok, f"帧 {i} 在 t={s} 启动时门不合法(phase={phase})"
    if not occ:
        return
    lo = min(occ)
    hi = max(occ)
    for t in range(lo, hi + 1):
        if t in occ:
            continue
        # 空闲 ns:必须不存在任何已到达、未完成且门合法的可发帧
        for i, fr in enumerate(frags_per_frame):
            if fr is None or frames[i][1] > t:
                continue
            q, size = frames[i][0], frames[i][2]
            if q in express_queues:
                finished = any(e <= t for (_s, e) in fr)
            else:
                remaining = size
                done_bytes = 0
                for (_s, e) in fr:
                    chunk = min(frag_size, remaining)
                    remaining -= chunk
                    if e <= t:
                        done_bytes += chunk
                finished = done_bytes >= size
            if finished:
                continue
            phase = (t + offset) % cycle
            if any(q in qs and ws <= phase < we and we - phase > guard
                   for (ws, we, qs) in windows):
                raise AssertionError(
                    f"凭空空闲: t={t} 帧 {i} 已到达未完成且门合法,链路却空闲")
