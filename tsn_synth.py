"""多跳存储转发排程合成:固定路径 + 整数周期,离线枚举出可重放的逐链路门表。

问题模型
--------
- 网络由若干有向链路 Link(link_id, rate_bits_per_ns, guard_ns) 组成。
- 每条流 FlowSpec 给定:固定路径(有向链路序列,1..3 跳)、周期 period_ns、
  帧大小 size_bytes、相对截止期 deadline_ns(相对于每次释放,且 <= 周期)。
- 流 f 在 k*period_ns(k=0,1,...) 释放一帧;跳 h 的序列化时长
  d[f,h] = ceil(size_bytes*8 / link.rate_bits_per_ns),精确整数。
- 存储转发:跳 h 最早开始时刻 = 上一跳结束时刻;不计传播时延。
- 超周期 H = lcm(所有周期)。排程在 [0, H) 内合成并按 H 循环重复。
- 决策变量为每流每跳相对释放时刻的整数偏移 s[f,h],满足:
    s[f,0] >= 0;
    s[f,h] >= s[f,h-1] + d[f,h-1]                (存储转发);
    s[f,h] + d[f,h] + guard[link] <= period_ns   (单实例含 guard 容得下);
    s[f,last] + d[f,last] <= deadline_ns         (端到端截止期)。
- 同一链路上任意两个发送实例(含 guard 占用区间)
  [k*P+s, k*P+s+d+guard) 不得重叠;guard 与发送一样占用链路。
- 每条流按输入下标分配固定队列 0..n-1;每个发送实例在其链路门表中得到
  一扇专属窗口 [start, start+d+guard)。

三种结论互不混淆:
- 可行:返回 SynthesisResult(status=FEASIBLE, ...),该结果已用 tsn_gate.replay
  逐链路重放校验通过,可直接重放;
- 已证明无解:抛 InfeasibleScheduleError;
- 搜索预算耗尽:抛 SynthesisBudgetExhaustedError(BudgetExhaustedError 的子类),
  既不是可行也不是无解。

本模块只实现题面限定的可枚举小子集(<=4 流、<=3 跳、短超周期),
不宣称兼容完整 IEEE 802.1Qbv / 在线调度能力。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction

from tsn_gate import (
    BudgetExhaustedError,
    Frame,
    GateSchedule,
    GateWindow,
    TSNGateError,
    replay,
)

__all__ = [
    "MAX_FLOWS",
    "MAX_HOPS",
    "MAX_HYPERPERIOD_NS",
    "MAX_TOTAL_INSTANCES",
    "DEFAULT_MAX_NODES",
    "SynthesisStatus",
    "InfeasibleScheduleError",
    "SynthesisBudgetExhaustedError",
    "Link",
    "FlowSpec",
    "HopPlan",
    "FlowPlan",
    "Transmission",
    "LinkGatePlan",
    "SynthesisResult",
    "LinkReplay",
    "synthesize",
    "replay_synthesis",
    "verify_synthesis",
]

MAX_FLOWS = 4
MAX_HOPS = 3
# “短超周期”可枚举范围的显式上限;超过即超出本题契约,按非法输入拒绝。
MAX_HYPERPERIOD_NS = 10_000
MAX_TOTAL_INSTANCES = 10_000
DEFAULT_MAX_NODES = 1_000_000


class SynthesisStatus(str, Enum):
    FEASIBLE = "feasible"


class InfeasibleScheduleError(TSNGateError):
    """已证明不存在满足全部约束的排程。"""


class SynthesisBudgetExhaustedError(BudgetExhaustedError):
    """枚举节点预算耗尽:搜索被截断,无结论(非可行,也非已证明无解)。"""


def _require_int(value, where: str, lo=None, hi=None) -> int:
    """与 tsn_gate 一致的整数校验:拒绝 bool、NaN/Infinity 及非 int。"""
    if isinstance(value, bool):
        raise TSNGateError(f"{where}: 需要 int,拒绝 bool({value!r})")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise TSNGateError(f"{where}: 拒绝 NaN/Infinity({value!r})")
        raise TSNGateError(f"{where}: 需要整数,拒绝 float({value!r})")
    if not isinstance(value, int):
        raise TSNGateError(f"{where}: 需要 int,得到 {type(value).__name__}({value!r})")
    if lo is not None and value < lo:
        raise TSNGateError(f"{where}: {value} 小于下限 {lo}")
    if hi is not None and value > hi:
        raise TSNGateError(f"{where}: {value} 大于上限 {hi}")
    return value


@dataclass(frozen=True)
class Link:
    """一条有向链路(逐跳路径的元素)。guard_ns 为该链路全局 guard。"""

    link_id: int
    rate_bits_per_ns: int = 8
    guard_ns: int = 0

    def __post_init__(self):
        _require_int(self.link_id, "Link.link_id", lo=0)
        _require_int(self.rate_bits_per_ns, "Link.rate_bits_per_ns", lo=1)
        _require_int(self.guard_ns, "Link.guard_ns", lo=0)


@dataclass(frozen=True)
class FlowSpec:
    """一条待调度流。字段在 synthesize() 中校验,以便错误消息带 flows[i] 定位。

    path:       Link 序列(固定路径,1..3 跳)
    period_ns:  释放周期,>= 1
    size_bytes: 每帧字节数,>= 1(各跳时长由所经链路速率分别决定)
    deadline_ns: 端到端相对截止期,1 <= deadline_ns <= period_ns
    """

    flow_id: int
    path: tuple
    period_ns: int
    size_bytes: int
    deadline_ns: int


@dataclass(frozen=True)
class HopPlan:
    """单跳模板:相对释放时刻的发送窗口偏移(每周期重复)。"""

    hop_index: int
    link_id: int
    duration_ns: int          # 序列化时长(不含 guard)
    start_offset_ns: int
    end_offset_ns: int


@dataclass(frozen=True)
class FlowPlan:
    """一条流的可重放模板:周期内逐跳偏移,超周期内按周期重复。"""

    flow_id: int
    queue: int
    period_ns: int
    size_bytes: int
    deadline_ns: int
    hops: tuple               # HopPlan,按跳序


@dataclass(frozen=True)
class Transmission:
    """超周期 [0, H) 内的一个具体发送实例。"""

    flow_id: int
    hop_index: int
    link_id: int
    queue: int
    release_ns: int           # 该帧在源端的释放时刻
    arrive_ns: int            # 该跳帧到达出口队列的时刻
    start_ns: int
    end_ns: int
    deadline_ns: int          # 该实例的绝对截止期(仅末跳判定)


@dataclass(frozen=True)
class LinkGatePlan:
    """一条链路上的合成门表与其重放入口(arrivals 与门表自洽,可直接重放)。"""

    link_id: int
    gate: GateSchedule
    arrivals: tuple           # tsn_gate.Frame,按 (arrive_ns, flow_id, hop, k) 排序


@dataclass(frozen=True)
class SynthesisResult:
    status: SynthesisStatus
    hyperperiod_ns: int
    flow_plans: tuple         # FlowPlan,按输入流顺序
    transmissions: tuple      # Transmission,按 (start_ns, link_id, flow_id, hop) 排序
    link_gates: tuple         # LinkGatePlan,按 link_id 排序


@dataclass(frozen=True)
class LinkReplay:
    link_id: int
    result: object            # tsn_gate.ReplayResult


def _ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b


def _crt_min(a1: int, m1: int, a2: int, m2: int):
    """求满足 x ≡ a1 (mod m1), x ≡ a2 (mod m2) 的最小非负 x;无解返回 None。"""
    g = math.gcd(m1, m2)
    diff = a2 - a1
    if diff % g:
        return None
    if m2 == 1:
        return a1 % m1   # 模 1 的同余式恒真
    if m1 == 1:
        return a2 % m2
    # x = a1 + m1*k;m1*k ≡ diff (mod m2);约去 g
    m1g, m2g = m1 // g, m2 // g
    k0 = (diff // g) * pow(m1g, -1, m2g) % m2g
    return (a1 + m1 * k0) % (m1 * m2g)  # 模 lcm(m1,m2) 的最小非负解


def _streams_conflict(P: int, s: int, length: int,
                      Q: int, t: int, other: int, H: int) -> bool:
    """两条周期发送流在 [0,H) 上是否存在 guard 区间重叠。

    区间为 [kP+s, kP+s+length) 与 [lQ+t, lQ+t+other),半开,端点相接允许。

    两种独立算法按估计代价择一:
    1. 实例较少时直接成对枚举;
    2. 否则用 CRT:冲突等价于存在实例起点 a、b 使 delta=a-b ∈ [1-length, other-1],
       a ≡ s (mod P)、b ≡ t (mod Q) 且二者都落在 [0,H) 的实例范围内。
    """
    nf, ng = H // P, H // Q
    if nf * ng <= length + other:
        # 直接枚举(选实例数较少的方向做外层并在超越后中断)
        if nf <= ng:
            for k in range(nf):
                a = k * P + s
                ae = a + length
                for l in range(ng):
                    b = l * Q + t
                    if b >= ae:
                        break
                    if a < b + other and b < ae:
                        return True
        else:
            for l in range(ng):
                b = l * Q + t
                be = b + other
                for k in range(nf):
                    a = k * P + s
                    if a >= be:
                        break
                    if a < be and b < a + length:
                        return True
        return False

    # CRT 相位法:a ∈ [s, H-P+s] 步长 P;b=a-delta ∈ [t, H-Q+t] 步长 Q
    lo_a = s
    hi_a = H - P + s
    lo_b, hi_b = t, H - Q + t
    for delta in range(1 - length, other):
        x0 = _crt_min(s, P, t + delta, Q)
        if x0 is None:
            continue
        lo = max(lo_a, lo_b + delta)
        hi = min(hi_a, hi_b + delta)
        if lo <= hi:
            mod = math.lcm(P, Q)
            x = x0 if x0 >= lo else x0 + ((lo - x0 + mod - 1) // mod) * mod
            if x <= hi:
                return True
    return False


def _iter_chains(durs, guards, link_ids, period, deadline, budget):
    """惰性生成单条流的全部可行偏移链 (s0, s1, ...),字典序升序。

    惰性生成使“宽松周期下存在平凡解”的情形立即返回,不必先枚举天文数字的链;
    只有证明无解时才必须耗尽生成器。
    """
    m = len(durs)
    starts = []

    def rec(h: int):
        budget.tick()
        if h == m:
            yield tuple(starts)
            return
        lo = 0 if h == 0 else starts[-1] + durs[h - 1]
        hi = period - durs[h] - guards[h]
        for s in range(lo, hi + 1):
            if h == m - 1 and s + durs[h] > deadline:
                break  # s 升序,之后全部超 deadline
            ok = True
            for h2 in range(h):
                # 同一路径重复经过同一链路:该流的这两跳周期相同,
                # 单周期内两区间不相交即所有实例永不相交
                if link_ids[h2] == link_ids[h]:
                    e2 = starts[h2] + durs[h2] + guards[h2]
                    if starts[h2] < s + durs[h] + guards[h] and s < e2:
                        ok = False
                        break
            if ok:
                starts.append(s)
                yield from rec(h + 1)
                starts.pop()

    yield from rec(0)


class _Budget:
    def __init__(self, limit: int):
        self.used = 0
        self.limit = limit

    def tick(self) -> None:
        self.used += 1
        if self.used > self.limit:
            raise SynthesisBudgetExhaustedError(
                f"枚举节点预算 max_nodes={self.limit} 耗尽:排程搜索被截断,"
                f"这既不是可行排程,也不是“已证明无解”"
            )


def synthesize(flows, *, max_nodes: int = DEFAULT_MAX_NODES) -> SynthesisResult:
    """合成多跳存储转发排程。纯函数:不修改入参,失败只抛异常,无部分结果。"""
    _require_int(max_nodes, "max_nodes", lo=1)
    try:
        flows = tuple(flows)
    except TypeError as exc:
        raise TSNGateError(f"flows: 不可迭代({exc})") from exc
    if not flows:
        raise TSNGateError("flows: 至少提供 1 条流")
    if len(flows) > MAX_FLOWS:
        raise TSNGateError(f"flows: 流数量 {len(flows)} 超过上限 {MAX_FLOWS}")

    # ---- 输入校验与规范化(全部完成前不产出任何结果) ----
    link_specs = {}        # link_id -> (rate, guard, 首次提名位置)
    flow_ids = {}
    parsed = []            # (fid, path_ids, period, size, deadline, durs, guards)
    periods = []
    for i, f in enumerate(flows):
        where = f"flows[{i}]"
        if not isinstance(f, FlowSpec):
            raise TSNGateError(f"{where}: 需要 FlowSpec,得到 {type(f).__name__}")
        fid = _require_int(f.flow_id, f"{where}.flow_id", lo=0)
        if fid in flow_ids:
            raise TSNGateError(
                f"{where}.flow_id: {fid} 与 flows[{flow_ids[fid]}] 重复"
            )
        flow_ids[fid] = i
        try:
            path = tuple(f.path)
        except TypeError as exc:
            raise TSNGateError(f"{where}.path: 不可迭代({exc})") from exc
        if not path:
            raise TSNGateError(f"{where}.path: 路径为空,至少 1 跳")
        if len(path) > MAX_HOPS:
            raise TSNGateError(
                f"{where}.path: 跳数 {len(path)} 超过上限 {MAX_HOPS}"
            )
        for j, link in enumerate(path):
            if not isinstance(link, Link):
                raise TSNGateError(
                    f"{where}.path[{j}]: 需要 Link,得到 {type(link).__name__}"
                )
            prev = link_specs.get(link.link_id)
            if prev is None:
                link_specs[link.link_id] = (
                    link.rate_bits_per_ns, link.guard_ns, f"{where}.path[{j}]"
                )
            elif (prev[0], prev[1]) != (link.rate_bits_per_ns, link.guard_ns):
                raise TSNGateError(
                    f"{where}.path[{j}].link_id: 链路 {link.link_id} 的 "
                    f"rate/guard 与首次提名 {prev[2]}("
                    f"rate={prev[0]}, guard={prev[1]})不一致"
                )
        period = _require_int(f.period_ns, f"{where}.period_ns", lo=1)
        size = _require_int(f.size_bytes, f"{where}.size_bytes", lo=1)
        deadline = _require_int(f.deadline_ns, f"{where}.deadline_ns", lo=1)
        if deadline > period:
            raise TSNGateError(
                f"{where}.deadline_ns: {deadline} 大于 period_ns {period}"
                f"(本契约只调度 deadline <= 周期的约束截止期流)"
            )
        path_ids = tuple(l.link_id for l in path)
        durs = tuple(_ceil_div(size * 8, link_specs[lid][0]) for lid in path_ids)
        guards = tuple(link_specs[lid][1] for lid in path_ids)
        parsed.append((fid, path_ids, period, size, deadline, durs, guards))
        periods.append(period)

    # 超周期
    H = periods[0]
    for p in periods[1:]:
        H = math.lcm(H, p)
    if H > MAX_HYPERPERIOD_NS:
        raise TSNGateError(
            f"flows: 超周期 lcm{tuple(periods)} = {H}ns 超过短超周期上限 "
            f"{MAX_HYPERPERIOD_NS}ns(超出本题可枚举范围)"
        )
    total_hop_instances = sum(H // rec[2] * len(rec[1]) for rec in parsed)
    if total_hop_instances > MAX_TOTAL_INSTANCES:
        raise TSNGateError(
            f"flows: 超周期内发送实例总数 {total_hop_instances} 超过上限 "
            f"{MAX_TOTAL_INSTANCES}"
        )

    # ---- 无解的结构性充分证明(搜索前判定) ----
    for (fid, path_ids, period, size, deadline, durs, guards) in parsed:
        makespan = sum(durs)
        if makespan > deadline:
            raise InfeasibleScheduleError(
                f"无可行排程:flow {fid} 逐跳存储转发最小时长 {makespan}ns "
                f"> deadline {deadline}ns"
            )
        for h, (lid, d, g) in enumerate(zip(path_ids, durs, guards)):
            if d + g > period:
                raise InfeasibleScheduleError(
                    f"无可行排程:flow {fid} 第 {h} 跳(链路 {lid})发送 {d}ns "
                    f"+ guard {g}ns > 周期 {period}ns,单周期窗口容不下"
                )

    # 链路利用率(含 guard)> 100%:周期实例必然互撞
    load = {}
    for (fid, path_ids, period, size, deadline, durs, guards) in parsed:
        for lid, d, g in zip(path_ids, durs, guards):
            load[lid] = load.get(lid, Fraction(0)) + Fraction(d + g, period)
    for lid, u in load.items():
        if u > 1:
            raise InfeasibleScheduleError(
                f"无可行排程:链路 {lid} 的占用率(含 guard)= {u} > 1,"
                f"周期实例在任何相位下都会冲突"
            )

    # ---- 跨流组合 DFS(链路上的 guard 区间两两不相交) ----
    # 每条流的偏移链在进入该层时惰性生成:可行解通常在字典序最前面几条链即命中,
    # 不必先把单流的全部链枚举完;只有回溯/证无时才付出完整枚举代价。
    # occupied[link_id] = [(period, offset, length), ...] 已选流的发送流
    occupied = {lid: [] for lid in link_specs}
    budget = _Budget(max_nodes)

    def dfs(fi: int, chosen):
        if fi == len(parsed):
            return chosen
        (fid, path_ids, period, _size, dl, durs, guards) = parsed[fi]
        saw_any = False
        for chain in _iter_chains(durs, guards, path_ids, period, dl, budget):
            saw_any = True
            candidate = []   # (link_id, period, s, length)
            ok = True
            for h, s in enumerate(chain):
                lid = path_ids[h]
                length = durs[h] + guards[h]
                for (P2, s2, l2) in occupied[lid]:
                    if _streams_conflict(period, s, length, P2, s2, l2, H):
                        ok = False
                        break
                if not ok:
                    break
                candidate.append((lid, period, s, length))
            if not ok:
                continue
            for (lid, P2, s2, l2) in candidate:
                occupied[lid].append((P2, s2, l2))
            chosen.append(chain)
            found = dfs(fi + 1, chosen)
            if found is not None:
                return found
            chosen.pop()
            for (lid, P2, s2, l2) in candidate:
                occupied[lid].pop()
        if not saw_any:
            raise InfeasibleScheduleError(
                f"无可行排程:flow {fid} 不存在满足存储转发/截止期/"
                f"周期容量的逐跳偏移(枚举其偏移空间为空)"
            )
        return None

    chains = dfs(0, [])
    if chains is None:
        raise InfeasibleScheduleError(
            "无可行排程:枚举完全部偏移组合,每条链路总有周期发送实例冲突"
            "(利用率未超限也可能因相位不可分而无解)"
        )

    # ---- 展开为超周期实例与逐链路门表 ----
    flow_plans = []
    transmissions = []
    for fi, ((fid, path_ids, period, size, deadline, durs, guards), chain) in \
            enumerate(zip(parsed, chains)):
        queue = fi
        hops = [
            HopPlan(hop_index=h, link_id=path_ids[h], duration_ns=durs[h],
                    start_offset_ns=chain[h],
                    end_offset_ns=chain[h] + durs[h])
            for h in range(len(chain))
        ]
        flow_plans.append(FlowPlan(
            flow_id=fid, queue=queue, period_ns=period, size_bytes=size,
            deadline_ns=deadline, hops=tuple(hops),
        ))
        end_of = {}  # (release_index, hop) -> end_ns
        for k in range(H // period):
            release = k * period
            for h, s in enumerate(chain):
                start = release + s
                end = start + durs[h]
                end_of[(k, h)] = end
                arrive = release if h == 0 else end_of[(k, h - 1)]
                transmissions.append(Transmission(
                    flow_id=fid, hop_index=h, link_id=path_ids[h], queue=queue,
                    release_ns=release, arrive_ns=arrive,
                    start_ns=start, end_ns=end,
                    deadline_ns=release + deadline,
                ))

    transmissions.sort(key=lambda x: (x.start_ns, x.link_id, x.flow_id, x.hop_index))

    link_gates = []
    for lid in sorted(link_specs):
        rate, guard, _where = link_specs[lid]
        trs = [tr for tr in transmissions if tr.link_id == lid]
        windows = tuple(
            GateWindow(tr.start_ns, tr.end_ns + guard, frozenset({tr.queue}))
            for tr in sorted(trs, key=lambda x: (x.start_ns, x.flow_id, x.hop_index))
        )
        gate = GateSchedule(
            cycle_ns=H, windows=windows,
            link_rate_bits_per_ns=rate, guard_ns=guard, clock_offset_ns=0,
        )
        # 重放输入:到达时刻 = 源释放或上一跳结束,与合成决策无关地给出
        arr_specs = sorted(
            trs, key=lambda x: (x.arrive_ns, x.flow_id, x.hop_index, x.release_ns)
        )
        arrivals = tuple(
            Frame(queue=tr.queue, arrive_ns=tr.arrive_ns,
                  size_bytes=plan_size(flow_plans, tr.flow_id))
            for tr in arr_specs
        )
        link_gates.append(LinkGatePlan(link_id=lid, gate=gate, arrivals=arrivals))

    result = SynthesisResult(
        status=SynthesisStatus.FEASIBLE,
        hyperperiod_ns=H,
        flow_plans=tuple(flow_plans),
        transmissions=tuple(transmissions),
        link_gates=tuple(link_gates),
    )
    # 用既有公开入口 replay() 逐链路重放,逐实例核对后才交付
    verify_synthesis(result)
    return result


def plan_size(flow_plans, fid):
    for p in flow_plans:
        if p.flow_id == fid:
            return p.size_bytes
    raise TSNGateError(f"内部错误:未知 flow_id {fid}")


def replay_synthesis(result: SynthesisResult, *,
                     max_events=1_000_000) -> tuple:
    """用 tsn_gate.replay 逐链路重放合成结果,返回每条链路的 LinkReplay。"""
    if not isinstance(result, SynthesisResult):
        raise TSNGateError(
            f"result: 需要 SynthesisResult,得到 {type(result).__name__}"
        )
    out = []
    for lg in result.link_gates:
        rr = replay(list(lg.arrivals), lg.gate, max_events=max_events)
        out.append(LinkReplay(link_id=lg.link_id, result=rr))
    return tuple(out)


def verify_synthesis(result: SynthesisResult, *, max_events=1_000_000) -> tuple:
    """重放并逐实例核对开始/结束时刻;不一致即报错(交付前 synthesize 已调用)。"""
    replays = {lr.link_id: lr.result for lr in replay_synthesis(result,
                                                                max_events=max_events)}
    # 逐链路:重放输入与合成实例一一对应,按到达顺序比对
    for lg in result.link_gates:
        rr = replays[lg.link_id]
        if len(rr.frames) != len(lg.arrivals):
            raise TSNGateError(
                f"内部校验失败:链路 {lg.link_id} 重放帧数 "
                f"{len(rr.frames)} != 合成实例数 {len(lg.arrivals)}"
            )
        expected = sorted(
            (tr for tr in result.transmissions if tr.link_id == lg.link_id),
            key=lambda x: (x.arrive_ns, x.flow_id, x.hop_index, x.release_ns),
        )
        for frame, tr in zip(rr.frames, expected):
            if frame.start_ns != tr.start_ns or frame.end_ns != tr.end_ns:
                raise TSNGateError(
                    f"内部校验失败:链路 {lg.link_id} flow {tr.flow_id} 第 "
                    f"{tr.hop_index} 跳 release={tr.release_ns}: "
                    f"重放 ({frame.start_ns},{frame.end_ns}) != "
                    f"合成 ({tr.start_ns},{tr.end_ns})"
                )

    # 独立于重放的结构核对:存储转发、截止期、同链路 guard 区间不相交
    H = result.hyperperiod_ns
    for p in result.flow_plans:
        for k in range(H // p.period_ns):
            release = k * p.period_ns
            hops = sorted(
                (tr for tr in result.transmissions
                 if tr.flow_id == p.flow_id and tr.release_ns == release),
                key=lambda x: x.hop_index,
            )
            if len(hops) != len(p.hops):
                raise TSNGateError("内部校验失败:超周期内实例跳数与模板不一致")
            for h, tr in enumerate(hops):
                want_arrive = release if h == 0 else hops[h - 1].end_ns
                if tr.arrive_ns != want_arrive:
                    raise TSNGateError("内部校验失败:逐跳到达时刻与存储转发不一致")
            if hops[-1].end_ns > release + p.deadline_ns:
                raise TSNGateError("内部校验失败:端到端截止期被违反")

    by_link = {}
    for tr in result.transmissions:
        gate = next(lg.gate for lg in result.link_gates if lg.link_id == tr.link_id)
        length = (tr.end_ns - tr.start_ns) + gate.guard_ns
        by_link.setdefault(tr.link_id, []).append((tr.start_ns, tr.start_ns + length))
    for lid, intervals in by_link.items():
        intervals.sort()
        for (s1, e1), (s2, e2) in zip(intervals, intervals[1:]):
            if s2 < e1:
                raise TSNGateError(
                    f"内部校验失败:链路 {lid} 实例 [{s1},{e1}) 与 [{s2},{e2}) 冲突"
                )
    return tuple(replays[lid] for lid in sorted(replays))
