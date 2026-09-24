"""多跳存储转发排程合成:离线、确定性、纯整数纳秒(仅标准库)。

给定固定路径的周期流,为每条流的逐跳传输合成不冲突的发送时刻,
并生成可直接用 tsn_gate.replay 重放的单链路门表。

语义约定
--------
- 所有时间均为整数纳秒,参考时钟与时从 0 开始。
- 流 f:路径 path=(n0,n1,...,nk),逐跳链路 "n_{h}->n_{h+1}",k<=MAX_HOPS+1;
  周期 period_ns;首发释放时刻 release=offset_ns;相对截止期 deadline_ns,
  要求末跳结束时刻 <= offset_ns + deadline_ns。
- 存储转发:第 h 跳最早可在第 h-1 跳结束时刻开始(第 0 跳最早为 release)。
- 跳 h 在链路 l 上的序列化时长 = ceil(size_bytes[h]*8 / rate_l),精确整数;
  链路 l 的 guard_l 仅用于生成重放门表窗口(窗口长度 = 时长 + guard)。
- 周期实例:首跳(及每跳)在 t 发送后,后续周期实例在 t + j*period 发送。
  超周期 H = lcm(所有周期)。两条周期区间列是否冲突用模运算判据在 O(1)
  内判定(见 _patterns_conflict 的推导),该判据与“把 [0,H) 内全部实例
  展开后两两比较”等价,测试中与独立暴力展开交叉验证。
- 流自身相邻实例也不得重叠:任一跳时长 > 该流周期 => 直接判定无解。
- 枚举是完整的:每跳候选时刻稠密取遍 [最早(存储转发), 最晚(留足后续跳与
  截止期)] 内每个整数 ns;DFS 穷尽即“无解”的证明。预算(超周期/时间轴/
  搜索节点)耗尽抛 BudgetExhaustedError,与 InfeasibleError 严格区分。
- 可重放性:为每条链路生成 cycle=该链路最大窗口终点 的 GateSchedule,
  每个(流,链路)恰好一个窗口 [t, t+时长+guard),窗口在排程起点 t 才打开。
  按窗口起点归纳可证:tsn_gate.replay 中每帧恰在 t 发送(更早门未开,
  t 时刻链路必空),合成后立即用 replay 实测校验,不符则报内部错误。

本模块只实现本题契约的有限子集,不宣称兼容完整 IEEE 802.1Qbv/时间感知整形。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import MappingProxyType

from tsn_gate import (
    BudgetExhaustedError,
    Frame,
    GateSchedule,
    GateWindow,
    ReplayResult,
    TSNGateError,
    replay,
)

__all__ = [
    "MAX_FLOWS",
    "MAX_HOPS",
    "DEFAULT_MAX_SEARCH_NS",
    "DEFAULT_MAX_NODES",
    "InfeasibleError",
    "Flow",
    "Network",
    "Link",
    "HopSchedule",
    "FlowSchedule",
    "LinkReplay",
    "MultiHopSchedule",
    "synthesize",
    "replay_schedule",
]

MAX_FLOWS = 4
MAX_HOPS = 3                    # 每条流最多 3 个传输跳(路径最多 4 个节点)
DEFAULT_MAX_SEARCH_NS = 200_000
DEFAULT_MAX_NODES = 200_000


class InfeasibleError(TSNGateError):
    """完整枚举穷尽后证明无解:不是预算截断,也不是非法输入。"""


def _require_int(value, where: str, lo=None, hi=None) -> int:
    """与 tsn_gate 同口径的整数校验:拒绝 bool、NaN/Infinity、非 int。"""
    if isinstance(value, bool):
        raise TSNGateError(f"{where}: 需要 int,拒绝 bool({value!r})")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise TSNGateError(f"{where}: 拒绝 NaN/Infinity({value!r})")
        raise TSNGateError(f"{where}: 需要整数纳秒,拒绝 float({value!r})")
    if not isinstance(value, int):
        raise TSNGateError(f"{where}: 需要 int,得到 {type(value).__name__}({value!r})")
    if lo is not None and value < lo:
        raise TSNGateError(f"{where}: {value} 小于下限 {lo}")
    if hi is not None and value > hi:
        raise TSNGateError(f"{where}: {value} 大于上限 {hi}")
    return value


def _require_name(value, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise TSNGateError(f"{where}: 需要非空 str,得到 {value!r}")
    return value


def Link(src: str, dst: str) -> str:
    """链路名约定:有向 "源节点->目的节点"。"""
    return f"{_require_name(src, 'Link.src')}->{_require_name(dst, 'Link.dst')}"


@dataclass(frozen=True)
class Flow:
    """一条固定路径的周期流。

    name       唯一流名;path 节点名序列,相邻节点给出逐跳链路;
    period_ns  周期(>=1);offset_ns 首发释放时刻(>=0);
    deadline_ns 相对截止期(>=1):末跳结束 <= offset + deadline;
    size_bytes_h 每跳字节数:统一 int,或与跳数等长的 tuple/list(每项 >=1)。
    字段数值校验在 synthesize() 中进行,以便错误消息携带 flows[i] 定位。
    """

    name: str
    path: tuple
    period_ns: int
    deadline_ns: int
    sizes_bytes: object = 1
    offset_ns: int = 0

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise TSNGateError(f"Flow.name: 需要非空 str,得到 {self.name!r}")
        try:
            path = tuple(self.path)
        except TypeError as exc:
            raise TSNGateError(f"Flow({self.name!r}).path: 不可迭代({exc})") from exc
        if not path:
            raise TSNGateError(f"Flow({self.name!r}).path: 至少包含 2 个节点")
        for j, node in enumerate(path):
            if not isinstance(node, str) or not node:
                raise TSNGateError(
                    f"Flow({self.name!r}).path[{j}]: 需要非空 str,得到 {node!r}"
                )
        if len(set(path)) != len(path):
            raise TSNGateError(f"Flow({self.name!r}).path: 节点重复,路径不得成环")
        object.__setattr__(self, "path", path)
        sizes = self.sizes_bytes
        if isinstance(sizes, int) and not isinstance(sizes, bool):
            object.__setattr__(self, "sizes_bytes", sizes)
        else:
            try:
                sizes = tuple(sizes)
            except TypeError as exc:
                raise TSNGateError(
                    f"Flow({self.name!r}).sizes_bytes: 需要 int 或可迭代({exc})"
                ) from exc
            object.__setattr__(self, "sizes_bytes", sizes)


@dataclass(frozen=True)
class Network:
    """有向链路集合与逐链路速率/guard;未列出的链路用默认值。

    link_rate_bits_per_ns / link_guards_ns 的键必须出现在 links 中。
    """

    links: tuple
    default_rate_bits_per_ns: int = 8
    default_guard_ns: int = 0
    link_rates_bits_per_ns: dict = field(default_factory=dict)
    link_guards_ns: dict = field(default_factory=dict)

    def __post_init__(self):
        try:
            links = tuple(self.links)
        except TypeError as exc:
            raise TSNGateError(f"Network.links: 不可迭代({exc})") from exc
        if not links:
            raise TSNGateError("Network.links: 至少包含 1 条链路")
        for j, lk in enumerate(links):
            if not isinstance(lk, str) or not lk:
                raise TSNGateError(f"Network.links[{j}]: 需要非空 str,得到 {lk!r}")
        if len(set(links)) != len(links):
            raise TSNGateError("Network.links: 链路名重复")
        object.__setattr__(self, "links", links)

        _require_int(self.default_rate_bits_per_ns,
                     "Network.default_rate_bits_per_ns", lo=1)
        _require_int(self.default_guard_ns, "Network.default_guard_ns", lo=0)

        rates = self._check_mapping(
            self.link_rates_bits_per_ns, "link_rates_bits_per_ns", links, lo=1)
        guards = self._check_mapping(
            self.link_guards_ns, "link_guards_ns", links, lo=0)
        object.__setattr__(self, "link_rates_bits_per_ns",
                           MappingProxyType(dict(rates)))
        object.__setattr__(self, "link_guards_ns",
                           MappingProxyType(dict(guards)))

    @staticmethod
    def _check_mapping(mapping, label, links, lo):
        if not isinstance(mapping, dict):
            raise TSNGateError(
                f"Network.{label}: 需要 dict,得到 {type(mapping).__name__}")
        out = {}
        for key, val in mapping.items():
            if key not in links:
                raise TSNGateError(f"Network.{label}[{key!r}]: 该链路不在 links 中")
            _require_int(val, f"Network.{label}[{key!r}]", lo=lo)
            out[key] = val
        return out

    def rate_of(self, link: str) -> int:
        return self.link_rates_bits_per_ns.get(link, self.default_rate_bits_per_ns)

    def guard_of(self, link: str) -> int:
        return self.link_guards_ns.get(link, self.default_guard_ns)


@dataclass(frozen=True)
class HopSchedule:
    flow: str
    hop_index: int            # 0 = 第一跳
    link: str
    start_ns: int
    end_ns: int
    duration_ns: int


@dataclass(frozen=True)
class FlowSchedule:
    name: str
    path: tuple
    period_ns: int
    release_ns: int           # = offset_ns
    deadline_ns: int          # 绝对截止时刻 = offset + relative deadline
    total_duration_ns: int
    hops: tuple               # HopSchedule,按跳序


@dataclass(frozen=True)
class LinkReplay:
    """一条链路的重放制品:门表、帧、期望(开始,结束) 与实测 replay 结果。"""

    link: str
    gate_schedule: GateSchedule
    frames: tuple             # Frame,按排程起点升序
    expected: tuple           # (start_ns, end_ns) 元组,与 frames 对齐
    result: ReplayResult


@dataclass(frozen=True)
class MultiHopSchedule:
    hyperperiod_ns: int
    flows: tuple              # FlowSchedule,按输入顺序
    links: tuple              # 实际被路径使用的链路名(升序)
    hops: tuple               # HopSchedule,按 (开始,链路,流名,跳序) 升序
    link_replays: dict        # link -> LinkReplay(只读映射)

    def replay_link(self, link: str) -> ReplayResult:
        """单独重放一条链路(重新计算,不修改排程)。"""
        if link not in self.link_replays:
            raise TSNGateError(f"replay_link: 未知链路 {link!r}")
        lr = self.link_replays[link]
        return replay(lr.frames, lr.gate_schedule)

    def replay_all(self) -> dict:
        """重放全部链路,返回 {link: ReplayResult}。"""
        return {lk: self.replay_link(lk) for lk in self.link_replays}


# --------------------------------------------------------------------------- #
# 周期区间列的冲突判定(模运算,与超周期环上的逐实例展开等价)
# --------------------------------------------------------------------------- #
#
# 跳 X 在 t_X 首发、周期 P_X、时长 d_X,其实例为 [t_X + k*P_X, +d_X)。
# 两条这样的区间列(半开区间)存在相交实例,当且仅当
#     r < d_B 或 r > g - d_A,  其中 g = gcd(P_A, P_B), r = (t_A - t_B) mod g
# (即 (t_A - t_B) + g*ℤ 与开区间 (-d_A, d_B) 有交)。因为超周期 H 是两周期的
# 公倍数,该判据与“把 [0,H) 内全部实例展开后两两比较”完全等价,但代价 O(1)。

def _patterns_conflict(t_a: int, p_a: int, d_a: int,
                       t_b: int, p_b: int, d_b: int) -> bool:
    g = math.gcd(p_a, p_b)
    r = (t_a - t_b) % g
    return r < d_b or r > g - d_a


def _fits(patterns, t: int, period: int, dur: int) -> bool:
    """候选 (首发 t, 周期 period, 时长 dur) 与该链路已有模式全部不冲突。"""
    return all(not _patterns_conflict(t, period, dur, pt, pp, pd)
               for (pt, pp, pd) in patterns)


def synthesize(flows, network, *, max_search_ns=DEFAULT_MAX_SEARCH_NS,
               max_nodes=DEFAULT_MAX_NODES) -> MultiHopSchedule:
    """合成多跳存储转发排程。

    纯函数:先完整校验所有输入,再搜索;成功返回可重放的 MultiHopSchedule,
    无解抛 InfeasibleError,预算不足抛 BudgetExhaustedError,
    非法输入抛 TSNGateError。任何失败路径都不产生部分结果。

    max_search_ns: 时间轴预算,超周期 H 与最晚截止时刻均不得超过它;
    max_nodes:    DFS 候选节点预算(防错安全阀)。
    """
    _require_int(max_search_ns, "max_search_ns", lo=1)
    _require_int(max_nodes, "max_nodes", lo=1)
    if not isinstance(network, Network):
        raise TSNGateError(
            f"network: 需要 Network,得到 {type(network).__name__}")
    try:
        flows = tuple(flows)
    except TypeError as exc:
        raise TSNGateError(f"flows: 不可迭代({exc})") from exc
    if not 1 <= len(flows) <= MAX_FLOWS:
        raise TSNGateError(
            f"flows: 流数量 {len(flows)} 超出范围 1..{MAX_FLOWS}")

    names = set()
    norm_sizes = []             # 与 flows 对齐的逐跳字节数元组(本地归一化,不改入参)
    for i, fl in enumerate(flows):
        where = f"flows[{i}]"
        if not isinstance(fl, Flow):
            raise TSNGateError(f"{where}: 需要 Flow,得到 {type(fl).__name__}")
        if fl.name in names:
            raise TSNGateError(f"{where}.name: 流名 {fl.name!r} 重复")
        names.add(fl.name)
        if not 2 <= len(fl.path) <= MAX_HOPS + 1:
            raise TSNGateError(
                f"{where}.path: 节点数 {len(fl.path)} 超出范围 2..{MAX_HOPS + 1}"
                f"(即传输跳数 1..{MAX_HOPS})")
        for j in range(len(fl.path) - 1):
            lk = Link(fl.path[j], fl.path[j + 1])
            if lk not in network.links:
                raise TSNGateError(
                    f"{where}.path 第 {j} 跳链路 {lk!r} 不在 Network.links 中")
        _require_int(fl.period_ns, f"{where}.period_ns", lo=1)
        _require_int(fl.offset_ns, f"{where}.offset_ns", lo=0)
        _require_int(fl.deadline_ns, f"{where}.deadline_ns", lo=1)

        nhops = len(fl.path) - 1
        sizes = fl.sizes_bytes
        if isinstance(sizes, int):
            if isinstance(sizes, bool):
                raise TSNGateError(f"{where}.sizes_bytes: 拒绝 bool")
            if sizes < 1:
                raise TSNGateError(f"{where}.sizes_bytes: {sizes} 小于下限 1")
            sizes = (sizes,) * nhops
        else:
            sizes = tuple(sizes)
            if len(sizes) != nhops:
                raise TSNGateError(
                    f"{where}.sizes_bytes: 长度 {len(sizes)} 与跳数 {nhops} 不一致")
            for j, z in enumerate(sizes):
                _require_int(z, f"{where}.sizes_bytes[{j}]", lo=1)
        norm_sizes.append(sizes)

    # 超周期(完整枚举所依赖的周期冲突环长度)
    H = flows[0].period_ns
    for fl in flows[1:]:
        H = math.lcm(H, fl.period_ns)
    if H > max_search_ns:
        raise BudgetExhaustedError(
            f"超周期 H=lcm(周期)={H} 超过时间轴预算 max_search_ns="
            f"{max_search_ns}:搜索未执行,这不是“无解”的证明;"
            f"题面限定短超周期,如确需可显式调大预算")

    latest_abs = max(fl.offset_ns + fl.deadline_ns for fl in flows)
    if latest_abs > max_search_ns:
        raise BudgetExhaustedError(
            f"最晚绝对截止时刻 {latest_abs} 超过时间轴预算 "
            f"max_search_ns={max_search_ns}:搜索未执行,这不是“无解”的证明")

    # 逐跳展开:(flow_idx, hop_idx, link, dur, abs_deadline)
    hop_rows = []
    durs = []
    for i, fl in enumerate(flows):
        row = []
        ds = []
        release = fl.offset_ns
        for h in range(len(fl.path) - 1):
            lk = Link(fl.path[h], fl.path[h + 1])
            rate = network.rate_of(lk)
            d = (norm_sizes[i][h] * 8 + rate - 1) // rate
            if d > fl.period_ns:
                raise InfeasibleError(
                    f"流 {fl.name!r} 第 {h} 跳时长 {d}ns 超过周期 "
                    f"{fl.period_ns}ns:该流相邻周期实例在链路 {lk!r} 上自我重叠,"
                    f"任何排程都不可能满足")
            row.append((i, h, lk, d, release + fl.deadline_ns))
            ds.append(d)
        durs.append(ds)
        hop_rows.extend(row)

    n = len(hop_rows)
    suffix = []                 # suffix[i][h] = 第 h 跳到末跳的时长之和(含 h)
    for ds in durs:
        acc = [0] * (len(ds) + 1)
        for h in range(len(ds) - 1, -1, -1):
            acc[h] = acc[h + 1] + ds[h]
        suffix.append(acc)

    chosen = [[None] * len(durs[i]) for i in range(len(flows))]
    nodes = 0

    def dfs(idx: int, busy: dict) -> bool:
        nonlocal nodes
        if idx == n:
            return True
        fi, h, lk, dur, abs_dl = hop_rows[idx]
        release = flows[fi].offset_ns
        period = flows[fi].period_ns
        earliest = release if h == 0 else chosen[fi][h - 1] + durs[fi][h - 1]
        latest = abs_dl - suffix[fi][h]      # 留足本跳及后续各跳
        if earliest > latest:
            return False
        intervals = busy.get(lk, ())
        saved = intervals
        for t in range(earliest, latest + 1):
            nodes += 1                       # 每个候选时刻计一个搜索节点
            if nodes > max_nodes:
                raise BudgetExhaustedError(
                    f"搜索节点预算 max_nodes={max_nodes} 耗尽:枚举被截断,"
                    f"这不是“无解”的证明;可显式调大预算重试")
            if not _fits(intervals, t, period, dur):
                continue
            busy[lk] = intervals + ((t, period, dur),)
            chosen[fi][h] = t
            if dfs(idx + 1, busy):
                return True
            busy[lk] = saved
            chosen[fi][h] = None
        return False

    feasible = dfs(0, {})
    if not feasible:
        raise InfeasibleError(
            "完整枚举穷尽:在存储转发先后、周期实例不冲突(超周期 "
            f"H={H}ns)与各流截止期约束下不存在可行排程")

    # ---- 构造结果与可重放门表 ------------------------------------------------
    flow_schedules = []
    per_link_windows = {lk: [] for lk in network.links}
    per_link_frames = {}        # lk -> [(start, Frame, (start,end))]
    flat_hops = []

    for i, fl in enumerate(flows):
        hops_out = []
        release = fl.offset_ns
        abs_dl = release + fl.deadline_ns
        for h in range(len(fl.path) - 1):
            lk = Link(fl.path[h], fl.path[h + 1])
            d = durs[i][h]
            t = chosen[i][h]
            hs = HopSchedule(flow=fl.name, hop_index=h, link=lk,
                             start_ns=t, end_ns=t + d, duration_ns=d)
            hops_out.append(hs)
            flat_hops.append(hs)
            arrive = release if h == 0 else chosen[i][h - 1] + durs[i][h - 1]
            guard = network.guard_of(lk)
            per_link_windows[lk].append(
                GateWindow(t, t + d + guard, frozenset({i})))
            per_link_frames.setdefault(lk, []).append((
                t,
                Frame(queue=i, arrive_ns=arrive, size_bytes=norm_sizes[i][h],
                      deadline_ns=abs_dl),
                (t, t + d),
            ))
        flow_schedules.append(FlowSchedule(
            name=fl.name, path=fl.path, period_ns=fl.period_ns,
            release_ns=release, deadline_ns=abs_dl,
            total_duration_ns=sum(durs[i]),
            hops=tuple(hops_out)))

    used_links = tuple(sorted({hk.link for hk in flat_hops}))
    link_replays = {}
    for lk in used_links:
        wins = tuple(sorted(per_link_windows[lk], key=lambda w: w.start_ns))
        cycle = max(w.end_ns for w in wins)
        gs = GateSchedule(
            cycle_ns=cycle,
            windows=wins,
            link_rate_bits_per_ns=network.rate_of(lk),
            guard_ns=0,                 # guard 已含进窗口长度
            clock_offset_ns=0,
        )
        entries = sorted(per_link_frames[lk], key=lambda x: (x[0], x[2][1]))
        frames = tuple(e[1] for e in entries)
        expected = tuple(e[2] for e in entries)
        got = replay(frames, gs)
        for k, fr in enumerate(got.frames):
            exp = expected[k]
            if fr.start_ns != exp[0] or fr.end_ns != exp[1] or fr.deadline_violated:
                raise TSNGateError(
                    f"内部错误:链路 {lk!r} 重放与排程不一致:"
                    f"帧 seq={k} 期望 {exp},实测 ({fr.start_ns},{fr.end_ns}),"
                    f"deadline_violated={fr.deadline_violated}")
        link_replays[lk] = LinkReplay(
            link=lk, gate_schedule=gs, frames=frames, expected=expected,
            result=got)

    return MultiHopSchedule(
        hyperperiod_ns=H,
        flows=tuple(flow_schedules),
        links=used_links,
        hops=tuple(sorted(flat_hops,
                          key=lambda x: (x.start_ns, x.link, x.flow, x.hop_index))),
        link_replays=MappingProxyType(link_replays),
    )


def replay_schedule(schedule: MultiHopSchedule) -> dict:
    """对合成结果重放全部链路(便捷入口;结果对象自身也可 .replay_all())。"""
    if not isinstance(schedule, MultiHopSchedule):
        raise TSNGateError(
            f"schedule: 需要 MultiHopSchedule,得到 {type(schedule).__name__}")
    return schedule.replay_all()
