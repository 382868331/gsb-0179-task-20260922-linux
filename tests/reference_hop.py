"""hop_schedule 的独立参考枚举器:故意不导入 hop_schedule,只重述题面语义。

与被测实现的差异:
- 不用模运算冲突判据,而是把周期实例在足够大的窗口内**逐条展开**后两两比较;
- 不用 DFS + 后缀和剪枝,而是按流递归枚举全部 (首跳..末跳) 起点组合,
  再逐约束检查(存储转发、截止期、链路不冲突)。

仅适用于小整数用例(测试生成器保证超周期、时刻都很小)。
"""

from itertools import product
from math import gcd


def _lcm(a, b):
    return a * b // gcd(a, b)


def expand_instances(t, period, dur, lo, hi):
    """[t + k*period, +dur) 中所有与 [lo,hi) 相交的实例(k 为任意整数)。"""
    out = []
    k = (lo - dur - t) // period - 1
    while True:
        s = t + k * period
        if s >= hi:
            break
        if s + dur > lo:
            out.append((s, s + dur))
        k += 1
    return out


def trains_conflict(t1, p1, d1, t2, p2, d2, horizon):
    """两条周期区间列是否存在相交实例(双无限展开,窗口取得足够大)。"""
    a = expand_instances(t1, p1, d1, -2 * horizon, 3 * horizon)
    b = expand_instances(t2, p2, d2, -2 * horizon, 3 * horizon)
    return any(s1 < e2 and s2 < e1 for (s1, e1) in a for (s2, e2) in b)


def reference_verdict(flows):
    """独立判定可行性。flows: [(period, offset, deadline, [(link, dur), ...]), ...]

    返回 (feasible: bool, witness: tuple|None);witness 为每跳起点组成的元组。
    """
    H = 1
    for (period, _off, _dl, _hops) in flows:
        H = _lcm(H, period)

    # 任何实例起点都 < offset + deadline;展开窗口上界取所有 t + H 足够
    horizon = max(off + dl for (_p, off, dl, _h) in flows) + H

    # 每条流独立的候选:满足存储转发与截止期的逐跳起点组合
    per_flow_options = []
    for (period, offset, deadline, hops) in flows:
        abs_dl = offset + deadline
        durs = [d for (_lk, d) in hops]
        nd = len(durs)
        suffix = [0] * (nd + 1)
        for h in range(nd - 1, -1, -1):
            suffix[h] = suffix[h + 1] + durs[h]
        opts = []

        def rec(h, earliest, acc):
            if h == nd:
                opts.append(tuple(acc))
                return
            for t in range(earliest, abs_dl - suffix[h] + 1):
                acc.append(t)
                rec(h + 1, t + durs[h], acc)
                acc.pop()

        rec(0, offset, [])
        if not opts:
            return False, None
        per_flow_options.append(opts)

    # 逐流组合,检查链路冲突(独立展开法)
    for combo in product(*per_flow_options):
        placements = []     # (link, t, period, dur)
        for fi, starts in enumerate(combo):
            period, _off, _dl, hops = flows[fi]
            for h, (lk, d) in enumerate(hops):
                placements.append((lk, starts[h], period, d))
        ok = True
        for i in range(len(placements)):
            for j in range(i + 1, len(placements)):
                li, ti, pi, di = placements[i]
                lj, tj, pj, dj = placements[j]
                if li != lj:
                    continue
                if trains_conflict(ti, pi, di, tj, pj, dj, horizon):
                    ok = False
                    break
            if not ok:
                break
        if ok:
            witness = tuple(t for (_lk, t, _p, _d) in placements)
            return True, witness
    return False, None


def check_schedule(flows, starts):
    """独立校验一个具体排程是否满足全部题面约束(用于核对被测实现的输出)。

    flows: [(period, offset, deadline, [(link, dur), ...]), ...]
    starts: 与 flows 对齐的每跳起点元组的列表。
    返回违规说明字符串列表(空 = 完全合法)。
    """
    problems = []
    H = 1
    for (period, _off, _dl, _hops) in flows:
        H = _lcm(H, period)
    horizon = max(off + dl for (_p, off, dl, _h) in flows) + H

    placements = []
    for fi, ((period, offset, deadline, hops), st) in enumerate(zip(flows, starts)):
        abs_dl = offset + deadline
        if len(st) != len(hops):
            problems.append(f"flow{fi}: 起点数量 {len(st)} != 跳数 {len(hops)}")
            continue
        for h, (lk, d) in enumerate(hops):
            t = st[h]
            if h == 0:
                if t < offset:
                    problems.append(f"flow{fi} hop{h}: 起点 {t} 早于释放 {offset}")
            else:
                prev_end = st[h - 1] + hops[h - 1][1]
                if t < prev_end:
                    problems.append(
                        f"flow{fi} hop{h}: 起点 {t} 早于上一跳结束 {prev_end}")
            if h == len(hops) - 1 and t + d > abs_dl:
                problems.append(
                    f"flow{fi} hop{h}: 结束 {t + d} 超过截止 {abs_dl}")
            if d > period:
                problems.append(f"flow{fi} hop{h}: 时长 {d} 超过周期 {period}")
            placements.append((fi, h, lk, t, period, d))

    for i in range(len(placements)):
        for j in range(i + 1, len(placements)):
            fi, hi, li, ti, pi, di = placements[i]
            fj, hj, lj, tj, pj, dj = placements[j]
            if li != lj:
                continue
            if trains_conflict(ti, pi, di, tj, pj, dj, horizon):
                problems.append(
                    f"链路 {li}: flow{fi}.hop{hi}@{ti} 与 flow{fj}.hop{hj}@{tj} 冲突")
    return problems
