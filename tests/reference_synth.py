"""独立参考:多跳排程可行性的朴素暴力判定(故意不导入 tsn_synth)。

用最直白的方式重述题面约束:
- 枚举每条流每跳的周期内偏移(存储转发、单周期容量、截止期);
- 跨流冲突在超周期 [0,H) 内展开成具体整数区间两两判交;
- 任取一组互不冲突的偏移即视为可行;全部组合耗尽为不可行。

仅适用于小整数周期(2..6)的随机用例,供 tsn_synth.synthesize 交叉验证。
"""

from math import gcd, lcm


def _ceil_div(a, b):
    return (a + b - 1) // b


def reference_feasible(flows, links):
    """flows: [(fid, path_ids, period, size_bytes, deadline), ...]

    links: {link_id: (rate, guard)}
    返回 (feasible: bool, H)。
    """
    parsed = []
    periods = []
    for (fid, path, P, size, dl) in flows:
        durs = [_ceil_div(size * 8, links[lid][0]) for lid in path]
        guards = [links[lid][1] for lid in path]
        parsed.append((fid, path, P, dl, durs, guards))
        periods.append(P)
    H = periods[0]
    for p in periods[1:]:
        H = lcm(H, p)

    # 单流链枚举(朴素:逐跳范围 + 自经过同链路判交)
    def chains(rec):
        fid, path, P, dl, durs, guards = rec
        m = len(durs)
        out = []
        s = []

        def rec_h(h):
            if h == m:
                out.append(tuple(s))
                return
            lo = 0 if h == 0 else s[-1] + durs[h - 1]
            hi = P - durs[h] - guards[h]
            for v in range(lo, hi + 1):
                if h == m - 1 and v + durs[h] > dl:
                    break
                ok = True
                for h2 in range(h):
                    if path[h2] == path[h]:
                        if s[h2] < v + durs[h] + guards[h] and \
                                v < s[h2] + durs[h2] + guards[h2]:
                            ok = False
                            break
                if ok:
                    s.append(v)
                    rec_h(h + 1)
                    s.pop()

        rec_h(0)
        return out

    all_chains = [chains(r) for r in parsed]
    if any(not c for c in all_chains):
        return False, H

    # 展开一条流某条链在 [0,H) 的全部 guard 占用区间
    def intervals(rec, chain):
        fid, path, P, dl, durs, guards = rec
        res = []
        for k in range(H // P):
            for h, v in enumerate(chain):
                a = k * P + v
                res.append((path[h], a, a + durs[h] + guards[h]))
        return res

    chosen = []

    def dfs(fi):
        if fi == len(parsed):
            return True
        for chain in all_chains[fi]:
            cand = intervals(parsed[fi], chain)
            clash = False
            for (lid_b, iv_b) in chosen:
                for (lid_a, a, ae) in cand:
                    for (l, b, be) in iv_b:
                        if lid_a == l and a < be and b < ae:
                            clash = True
                            break
                    if clash:
                        break
                if clash:
                    break
            if not clash:
                chosen.append((parsed[fi][0], cand))
                if dfs(fi + 1):
                    return True
                chosen.pop()
        return False

    return dfs(0), H
