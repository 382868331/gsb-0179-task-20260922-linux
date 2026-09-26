"""独立参考实现(抢占模式):逐纳秒推进、逐字节记账的朴素仿真。

故意不导入 tsn_gate,只用普通元组重述抢占语义,用于对事件驱动核心
tsn_gate.replay(GateSchedule.preemption=...) 做交叉验证。仅适用于小整数时间。

另提供 verify_occupancy:从核心输出的片段序列独立重建链路占用,
核对没有重叠发送、没有重复/丢失字节、没有凭空多出的空闲。
"""


def _ceil_div(a, b):
    return (a + b - 1) // b


def reference_preempt_replay(frames, windows, cycle, rate, guard, offset,
                             frag_bytes, overhead_bytes, tick_limit=2_000_000):
    """逐纳秒推进的参考重放(抢占模式)。

    frames:  [(queue, arrive_ns, size_bytes, is_express), ...]  下标即到达序号
    windows: [(start_ns, end_ns, frozenset[int]), ...]  周期内半开区间

    返回 list,下标即帧序号;每项为 ((frag_start, frag_end), ...) 片段元组,
    永不可发的帧为 None。
    """
    n = len(frames)
    dur = [_ceil_div(f[2] * 8, rate) for f in frames]

    maxwin = [0] * 4
    for (s, e, qs) in windows:
        for q in qs:
            if e - s > maxwin[q]:
                maxwin[q] = e - s

    def frag_dur(rem):
        payload = min(rem, frag_bytes)
        return _ceil_div((payload + overhead_bytes) * 8, rate)

    never = []
    for i in range(n):
        q, _a, size, is_exp = frames[i]
        need = dur[i] + guard if is_exp else frag_dur(size) + guard
        never.append(need > maxwin[q])

    remaining = [f[2] for f in frames]
    first_start = [None] * n
    fragments = [[] for _ in range(n)]
    ready_exp, ready_pre, paused = [], [], []
    arrived = [False] * n
    current = None
    busy_until = 0
    t = 0
    ticks = 0

    def fits(q, phase, need):
        return any(q in qs and s <= phase < e and e - phase >= need
                   for (s, e, qs) in windows)

    while True:
        if all(never[i] or remaining[i] == 0 for i in range(n)):
            break
        ticks += 1
        if ticks > tick_limit:
            raise AssertionError("reference_preempt_replay: tick 预算耗尽")
        if t < busy_until:
            t += 1
            continue
        # 链路空闲的这一纳秒:先完成上一片段,再纳入到达,再做决策
        if current is not None:
            if remaining[current] > 0:
                paused.append(current)
            current = None
        for i in range(n):
            if not arrived[i] and frames[i][1] <= t and not never[i]:
                arrived[i] = True
                (ready_exp if frames[i][3] else ready_pre).append(i)
        phase = (t + offset) % cycle
        chosen = None
        for i in sorted(ready_exp, key=lambda i: (frames[i][0], i)):
            if fits(frames[i][0], phase, dur[i] + guard):
                chosen = i
                ready_exp.remove(i)
                remaining[i] = 0
                d = dur[i]
                break
        if chosen is None:
            paused.sort(key=lambda i: (first_start[i], i))
            for i in list(paused):
                if fits(frames[i][0], phase, frag_dur(remaining[i]) + guard):
                    chosen = i
                    paused.remove(i)
                    d = frag_dur(remaining[i])
                    remaining[i] -= min(remaining[i], frag_bytes)
                    break
        if chosen is None:
            for i in sorted(ready_pre, key=lambda i: (frames[i][0], i)):
                if fits(frames[i][0], phase, frag_dur(remaining[i]) + guard):
                    chosen = i
                    ready_pre.remove(i)
                    first_start[i] = t
                    d = frag_dur(remaining[i])
                    remaining[i] -= min(remaining[i], frag_bytes)
                    break
        if chosen is None:
            t += 1
            continue
        fragments[chosen].append((t, t + d))
        if remaining[chosen] > 0:
            current = chosen
        busy_until = t + d
        t += 1
    return [tuple(fragments[i]) if not never[i] else None for i in range(n)]


def verify_occupancy(fragments_per_frame, frames, windows, cycle, rate, guard,
                     offset, frag_bytes, overhead_bytes):
    """独立核对链路占用:无重叠、无重复/丢失字节、无凭空空闲。

    fragments_per_frame: 每帧的 ((start, end), ...) 片段序列(被测核心的输出)
    frames/windows/...: 与 reference_preempt_replay 同义

    任何不一致都抛 AssertionError。
    """
    n = len(frames)
    # 1) 片段不重叠:逐纳秒占用图上每格至多一个发送者
    occupancy = {}
    last_end = 0
    for i in range(n):
        for (s, e) in fragments_per_frame[i]:
            assert 0 <= s < e, f"帧 {i}: 非法片段 [{s},{e})"
            for t in range(s, e):
                assert t not in occupancy, (
                    f"时刻 {t}: 帧 {i} 与帧 {occupancy[t]} 重叠发送")
                occupancy[t] = i
            last_end = max(last_end, e)

    # 2) 逐字节记账:每帧的字节 0..size-1 按序各发送恰好一次;
    #    普通帧除最后一片外每片载荷恰好 frag_bytes;紧急帧必须单片原子发送
    for i in range(n):
        q, _a, size, is_exp = frames[i]
        sent = 0
        for (s, e) in fragments_per_frame[i]:
            payload = min(size - sent, frag_bytes) if not is_exp else size
            expect = _ceil_div((payload + (0 if is_exp else overhead_bytes)) * 8,
                               rate)
            assert e - s == expect, (
                f"帧 {i} 片段 [{s},{e}): 线上时长 {e - s} != 期望 {expect}")
            sent += payload
            assert sent <= size, f"帧 {i}: 重复发送字节(累计 {sent} > {size})"
        assert sent == size, f"帧 {i}: 字节丢失(发送 {sent} / {size})"
        if is_exp:
            assert len(fragments_per_frame[i]) == 1, f"紧急帧 {i} 被分片"

    # 3) 无凭空空闲:链路空闲的每一纳秒,不存在任何可合法开始的帧
    remaining = [f[2] for f in frames]
    first_start = [None] * n
    frags = [list(fragments_per_frame[i]) for i in range(n)]
    for i in range(n):
        for (s, _e) in frags[i]:
            if first_start[i] is None or s < first_start[i]:
                first_start[i] = s

    def fits(q, phase, need):
        return any(q in qs and s <= phase < e and e - phase >= need
                   for (s, e, qs) in windows)

    for t in range(last_end):
        # 推进到 t:结算所有在 t 或之前结束的片段的字节
        for i in range(n):
            done_payload = 0
            for (s, e) in frags[i]:
                if e <= t:
                    done_payload += min(frames[i][2] - done_payload, frag_bytes) \
                        if not frames[i][3] else frames[i][2]
            remaining[i] = frames[i][2] - done_payload
        if t in occupancy:
            continue  # 链路被占,非空闲
        phase = (t + offset) % cycle
        for i in range(n):
            q, arrive, _size, is_exp = frames[i]
            if arrive > t or remaining[i] <= 0:
                continue
            if is_exp:
                need = _ceil_div(frames[i][2] * 8, rate) + guard
            else:
                payload = min(remaining[i], frag_bytes)
                need = _ceil_div((payload + overhead_bytes) * 8, rate) + guard
            assert not fits(q, phase, need), (
                f"时刻 {t}: 链路空闲但帧 {i} 可以开始(凭空多出的空闲)")

