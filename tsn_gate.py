"""TSN 门控发送验证:离线、确定性、纯整数纳秒的重放模块(仅标准库)。

语义约定
--------
- 所有时间均为整数纳秒。输入时间戳基于参考时钟,输出(开始/结束)同为参考时钟。
- 本地时钟 = 参考时钟 + clock_offset_ns;门表窗口作用在本地时钟的周期相位上。
- 门表:周期 cycle_ns 内的若干半开区间 [start_ns, end_ns),循环重复;
  同一队列的窗口不得重叠(重叠属非法输入,直接报错,不静默合并)。
- 帧不可抢占;仅当 序列化时长 + guard_ns 能完整落入当前开门窗口的剩余部分时
  才允许开始发送(开始时刻可以在窗口起点,结束+guard 可以恰好顶到窗口终点)。
- 序列化时长 = ceil(size_bytes * 8 / link_rate_bits_per_ns),精确整数运算。
- 多个可发帧竞争时按 (队列号升序, 到达序号升序) 选择;
  到达序号 = arrivals 列表下标,到达时间相同者按下标先后排序。
- 任何窗口都容纳不下 序列化时长+guard 的帧,在重放前即被证明并标记为
  NEVER_TRANSMITTABLE(永不可发),不参与等待,不会死循环。
- 事件预算 max_events 耗尽时抛出 BudgetExhaustedError,
  与“已证明永不可发”是两种不同的结论,互不混淆。

信用整形(可选,逐队列)
--------------------
为某个队列配置 CreditShaper 后,该队列在门控之外再叠加一层信用整形:
- 有积压(已到达未发送)且本队列未在发送时,信用按 idle_slope 增加(上限 hi_credit);
- 本队列发送期间,信用按 send_slope 减少(下限 lo_credit);
- 队列变空时:正信用立即归 0,负信用继续按 idle_slope 恢复到 0 为止;
- 门关闭期间信用冻结(本题规定,不采用其他标准行为);
- 仅当门打开且信用非负时才允许开始一帧;开始之后按原规则发完(不可抢占)。
信用与内部时间用 fractions.Fraction 精确表示,事件跳跃直接计算下一次
信用到 0 或门切换的时刻,不做小步轮询,无累计误差。配置整形后,
FrameResult 的 start_ns/end_ns/waited_ns 在不能整除时为 Fraction(精确值),
可整除时仍为 int;不配置整形时输出与旧版本完全一致(纯 int)。

本模块只实现本题契约的有限子集,不宣称兼容完整 IEEE 802.1Qbv/802.1Qav。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction

__all__ = [
    "MAX_QUEUES",
    "DEFAULT_MAX_EVENTS",
    "TSNGateError",
    "BudgetExhaustedError",
    "WaitReason",
    "FrameStatus",
    "Frame",
    "GateWindow",
    "GateSchedule",
    "CreditShaper",
    "FrameResult",
    "ReplayResult",
    "replay",
]

MAX_QUEUES = 4
DEFAULT_MAX_EVENTS = 1_000_000


class TSNGateError(ValueError):
    """输入非法或重放失败的基类;消息中带有可定位的字段路径。"""


class BudgetExhaustedError(TSNGateError):
    """事件预算耗尽:搜索被截断,既非成功也不是“已证明永不可发”。"""


class WaitReason(str, Enum):
    GATE_CLOSED = "gate-closed"                # 该队列当前无开门窗口
    INSUFFICIENT_WINDOW = "insufficient-window"  # 门开着但剩余窗口容不下 时长+guard
    LINK_BUSY = "link-busy"                    # 链路被其他帧占用(不可抢占)
    OUTRANKED = "outranked"                    # 同时可发但优先级/到达序号靠后
    CREDIT_NEGATIVE = "credit-negative"        # 整形队列信用为负,等待恢复


class FrameStatus(str, Enum):
    SENT = "sent"
    NEVER_TRANSMITTABLE = "never-transmittable"


def _require_int(value, where: str, lo=None, hi=None) -> int:
    """校验整数纳秒参数:拒绝 bool、NaN/Infinity 及一切非 int 类型。"""
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


@dataclass(frozen=True)
class Frame:
    """一个待发送帧。deadline_ns 为 None 表示无截止时间。

    字段校验在 replay() 中进行,以便错误消息携带 arrivals[i] 下标定位。
    """

    queue: int
    arrive_ns: int
    size_bytes: int
    deadline_ns: int | None = None


@dataclass(frozen=True)
class GateWindow:
    """周期内半开窗口 [start_ns, end_ns),对 queues 中的队列开门。"""

    start_ns: int
    end_ns: int
    queues: frozenset

    def __post_init__(self):
        _require_int(self.start_ns, "GateWindow.start_ns", lo=0)
        _require_int(self.end_ns, "GateWindow.end_ns", lo=1)
        if self.end_ns <= self.start_ns:
            raise TSNGateError(
                f"GateWindow: end_ns({self.end_ns}) 必须大于 start_ns({self.start_ns})"
            )
        try:
            qs = frozenset(self.queues)
        except TypeError as exc:
            raise TSNGateError(f"GateWindow.queues: 不可迭代({exc})") from exc
        if not qs:
            raise TSNGateError("GateWindow.queues: 至少包含一个队列")
        for q in qs:
            _require_int(q, "GateWindow.queues[]", lo=0, hi=MAX_QUEUES - 1)
        object.__setattr__(self, "queues", qs)


@dataclass(frozen=True)
class CreditShaper:
    """单个队列的信用整形参数(本题语义,见模块文档)。

    idle_slope: 信用增加斜率(信用/纳秒),必须为正整数;
    send_slope: 发送期间信用减少斜率,必须为负整数;一正一负由调用方给定。
    hi_credit / lo_credit: 信用上下限(含),hi_credit >= 0 >= lo_credit。
    """

    idle_slope: int
    send_slope: int
    hi_credit: int
    lo_credit: int

    def __post_init__(self):
        _require_int(self.idle_slope, "CreditShaper.idle_slope")
        _require_int(self.send_slope, "CreditShaper.send_slope")
        _require_int(self.hi_credit, "CreditShaper.hi_credit")
        _require_int(self.lo_credit, "CreditShaper.lo_credit")
        if self.idle_slope <= 0:
            raise TSNGateError(
                f"CreditShaper.idle_slope: {self.idle_slope} 必须为正(一正一负)"
            )
        if self.send_slope >= 0:
            raise TSNGateError(
                f"CreditShaper.send_slope: {self.send_slope} 必须为负(一正一负)"
            )
        if self.hi_credit < 0:
            raise TSNGateError(
                f"CreditShaper.hi_credit: {self.hi_credit} 必须 >= 0"
            )
        if self.lo_credit > 0:
            raise TSNGateError(
                f"CreditShaper.lo_credit: {self.lo_credit} 必须 <= 0"
            )


@dataclass(frozen=True)
class GateSchedule:
    """周期门表 + 链路参数 + 本地时钟偏差。"""

    cycle_ns: int
    windows: tuple
    link_rate_bits_per_ns: int
    guard_ns: int = 0
    clock_offset_ns: int = 0
    credit_shapers: dict | None = None   # {队列号: CreditShaper},None 表示全部不整形

    def __post_init__(self):
        _require_int(self.cycle_ns, "GateSchedule.cycle_ns", lo=1)
        _require_int(self.link_rate_bits_per_ns,
                     "GateSchedule.link_rate_bits_per_ns", lo=1)
        _require_int(self.guard_ns, "GateSchedule.guard_ns", lo=0)
        _require_int(self.clock_offset_ns, "GateSchedule.clock_offset_ns")
        try:
            wins = tuple(self.windows)
        except TypeError as exc:
            raise TSNGateError(f"GateSchedule.windows: 不可迭代({exc})") from exc
        for i, w in enumerate(wins):
            if not isinstance(w, GateWindow):
                raise TSNGateError(
                    f"GateSchedule.windows[{i}]: 需要 GateWindow,得到 {type(w).__name__}"
                )
            if w.end_ns > self.cycle_ns:
                raise TSNGateError(
                    f"GateSchedule.windows[{i}]: end_ns({w.end_ns}) 超出周期 "
                    f"cycle_ns({self.cycle_ns})"
                )
        # 同一队列的窗口不得重叠(半开区间:下一窗口起点 >= 上一窗口终点即可)
        for q in range(MAX_QUEUES):
            segs = sorted((w.start_ns, w.end_ns) for w in wins if q in w.queues)
            for (s1, e1), (s2, _e2) in zip(segs, segs[1:]):
                if s2 < e1:
                    raise TSNGateError(
                    f"GateSchedule.windows: 队列 {q} 的窗口 [{s1},{e1}) 与 "
                    f"[{s2},{_e2}) 重叠"
                )
        object.__setattr__(self, "windows", wins)
        shapers = self.credit_shapers
        if shapers is not None:
            try:
                items = list(shapers.items())
            except AttributeError as exc:
                raise TSNGateError(
                    f"GateSchedule.credit_shapers: 需要映射 {{队列号: CreditShaper}}({exc})"
                ) from exc
            norm = {}
            for k, v in items:
                _require_int(k, "GateSchedule.credit_shapers 的队列号",
                             lo=0, hi=MAX_QUEUES - 1)
                if not isinstance(v, CreditShaper):
                    raise TSNGateError(
                        f"GateSchedule.credit_shapers[{k}]: 需要 CreditShaper,"
                        f"得到 {type(v).__name__}"
                    )
                norm[k] = v
            object.__setattr__(self, "credit_shapers", norm)


@dataclass(frozen=True)
class FrameResult:
    seq: int                      # 到达序号(arrivals 下标)
    queue: int
    status: FrameStatus
    arrive_ns: int
    duration_ns: int              # 序列化时长(不含 guard)
    start_ns: int | None          # 参考时钟;永不可发时为 None
    end_ns: int | None
    waited_ns: int | None         # start_ns - arrive_ns
    wait_reasons: tuple           # 等待期间出现过的原因(WaitReason,按首次出现排序)
    deadline_ns: int | None
    deadline_violated: bool       # end_ns > deadline_ns(帧仍发送,不丢弃)


@dataclass(frozen=True)
class ReplayResult:
    frames: tuple                 # FrameResult,按 seq 升序
    sent_count: int
    never_count: int
    deadline_violations: tuple    # 违例帧的 seq 元组


def replay(arrivals, gate_schedule, *, max_events=DEFAULT_MAX_EVENTS) -> ReplayResult:
    """重放到达序列在周期门控表下的发送过程,返回确定性结果。

    纯函数:不修改入参,失败时抛出 TSNGateError(或其子类),不留下任何部分结果。
    """
    _require_int(max_events, "max_events", lo=1)
    if not isinstance(gate_schedule, GateSchedule):
        raise TSNGateError(
            f"gate_schedule: 需要 GateSchedule,得到 {type(gate_schedule).__name__}"
        )
    try:
        arrivals = list(arrivals)
    except TypeError as exc:
        raise TSNGateError(f"arrivals: 不可迭代({exc})") from exc

    rate = gate_schedule.link_rate_bits_per_ns
    guard = gate_schedule.guard_ns
    cycle = gate_schedule.cycle_ns
    offset = gate_schedule.clock_offset_ns

    n = len(arrivals)
    queue_of, arrive_of, dur_of, deadline_of = [], [], [], []
    for i, f in enumerate(arrivals):
        if not isinstance(f, Frame):
            raise TSNGateError(f"arrivals[{i}]: 需要 Frame,得到 {type(f).__name__}")
        queue_of.append(_require_int(f.queue, f"arrivals[{i}].queue",
                                     lo=0, hi=MAX_QUEUES - 1))
        arrive_of.append(_require_int(f.arrive_ns, f"arrivals[{i}].arrive_ns", lo=0))
        size = _require_int(f.size_bytes, f"arrivals[{i}].size_bytes", lo=1)
        d = f.deadline_ns
        if d is not None:
            d = _require_int(d, f"arrivals[{i}].deadline_ns", lo=0)
        deadline_of.append(d)
        dur_of.append((size * 8 + rate - 1) // rate)  # ceil,精确整数

    # 每个队列的窗口列表(按起点排序)及最长窗口长度
    qwin = [[] for _ in range(MAX_QUEUES)]
    for w in gate_schedule.windows:
        for q in w.queues:
            qwin[q].append((w.start_ns, w.end_ns))
    for q in range(MAX_QUEUES):
        qwin[q].sort()
    maxwin = [max((e - s for s, e in ws), default=0) for ws in qwin]

    need = [dur_of[i] + guard for i in range(n)]
    # 可证明的“永不可发”:不存在任何窗口容得下 时长+guard
    transmittable = [need[i] <= maxwin[queue_of[i]] for i in range(n)]

    if gate_schedule.credit_shapers:
        return _replay_shaped(
            gate_schedule, n, queue_of, arrive_of, dur_of, deadline_of,
            need, transmittable, qwin, max_events,
        )

    reasons: list[list[WaitReason]] = [[] for _ in range(n)]

    def note(i: int, r: WaitReason) -> None:
        if r not in reasons[i]:
            reasons[i].append(r)

    order = sorted(range(n), key=lambda i: (arrive_of[i], i))
    start_of: list[int | None] = [None] * n
    end_of: list[int | None] = [None] * n

    ready: list[int] = []          # 已到达、可发、未发送
    pi = 0                         # order 中下一个未到达帧的指针
    t = 0
    busy_until = 0
    events = 0

    while True:
        events += 1
        if events > max_events:
            raise BudgetExhaustedError(
                f"事件预算 max_events={max_events} 耗尽:重放被截断,"
                f"这不是“永不可发”的证明"
            )
        while pi < n and arrive_of[order[pi]] <= t:
            i = order[pi]
            pi += 1
            if transmittable[i]:
                ready.append(i)
        if not ready:
            if pi >= n:
                break
            t = max(t, busy_until, arrive_of[order[pi]])
            continue
        if t < busy_until:
            t = busy_until
            continue

        phase = (t + offset) % cycle
        ready.sort(key=lambda i: (queue_of[i], i))
        chosen = None
        outranked = []
        for i in ready:
            q = queue_of[i]
            fits = any(s <= phase < e and e - phase >= need[i] for s, e in qwin[q])
            if fits:
                if chosen is None:
                    chosen = i
                else:
                    outranked.append(i)
        if chosen is not None:
            for i in outranked:
                note(i, WaitReason.OUTRANKED)
            start_of[chosen] = t
            end = t + dur_of[chosen]
            end_of[chosen] = end
            busy_until = end
            ready.remove(chosen)
            for i in ready:
                note(i, WaitReason.LINK_BUSY)
            t = end
            continue

        # 没有帧能在当前时刻开始:记录原因,推进到下一个事件点
        for i in ready:
            q = queue_of[i]
            open_now = any(s <= phase < e for s, e in qwin[q])
            note(i, WaitReason.INSUFFICIENT_WINDOW if open_now else WaitReason.GATE_CLOSED)
        nxt = arrive_of[order[pi]] if pi < n else None
        for i in ready:
            for s, e in qwin[queue_of[i]]:
                for cand in (s, e):
                    d = (cand - phase) % cycle
                    if d == 0:
                        d = cycle
                    cand_t = t + d
                    if nxt is None or cand_t < nxt:
                        nxt = cand_t
        if nxt is None or nxt <= t:
            raise TSNGateError("内部错误:事件时间未能推进(不应发生)")
        t = nxt

    frames_out = []
    for i in range(n):
        if not transmittable[i]:
            frames_out.append(FrameResult(
                seq=i, queue=queue_of[i], status=FrameStatus.NEVER_TRANSMITTABLE,
                arrive_ns=arrive_of[i], duration_ns=dur_of[i],
                start_ns=None, end_ns=None, waited_ns=None, wait_reasons=(),
                deadline_ns=deadline_of[i], deadline_violated=False,
            ))
        else:
            violated = deadline_of[i] is not None and end_of[i] > deadline_of[i]
            frames_out.append(FrameResult(
                seq=i, queue=queue_of[i], status=FrameStatus.SENT,
                arrive_ns=arrive_of[i], duration_ns=dur_of[i],
                start_ns=start_of[i], end_ns=end_of[i],
                waited_ns=start_of[i] - arrive_of[i],
                wait_reasons=tuple(reasons[i]),
                deadline_ns=deadline_of[i], deadline_violated=violated,
            ))
    return ReplayResult(
        frames=tuple(frames_out),
        sent_count=sum(1 for i in range(n) if transmittable[i]),
        never_count=sum(1 for i in range(n) if not transmittable[i]),
        deadline_violations=tuple(
            i for i in range(n) if transmittable[i] and frames_out[i].deadline_violated
        ),
    )


def _as_output_time(value):
    """内部精确时间(Fraction)转输出:整值归一为 int,否则保留 Fraction。"""
    if isinstance(value, Fraction) and value.denominator == 1:
        return int(value)
    return value


def _replay_shaped(gate_schedule, n, queue_of, arrive_of, dur_of, deadline_of,
                   need, transmittable, qwin, max_events) -> ReplayResult:
    """带信用整形的重放:Fraction 精确时间 + 事件跳跃(不做小步轮询)。

    事件点:帧到达、门窗口边沿、发送结束(链路空闲)、信用恢复到 0。
    相邻事件点之间门状态/积压/发送状态恒定,信用按区间解析推进并夹取上下限,
    因此不存在逐 tick 轮询的累计误差。
    """
    shapers = gate_schedule.credit_shapers
    cycle = gate_schedule.cycle_ns
    offset = gate_schedule.clock_offset_ns

    reasons: list[list[WaitReason]] = [[] for _ in range(n)]

    def note(i: int, r: WaitReason) -> None:
        if r not in reasons[i]:
            reasons[i].append(r)

    order = sorted(range(n), key=lambda i: (arrive_of[i], i))
    start_of: list = [None] * n
    end_of: list = [None] * n
    credit = {q: Fraction(0) for q in shapers}
    backlog = {q: 0 for q in shapers}   # 已到达、未发送(不含正在发送)的帧数
    ready: list[int] = []
    pi = 0
    t = Fraction(0)
    busy_until = Fraction(0)
    active_q = None                   # t < busy_until 时占用链路的队列
    events = 0

    def gate_open(q: int, phase) -> bool:
        return any(s <= phase < e for s, e in qwin[q])

    def fits(q: int, phase, i: int) -> bool:
        return any(s <= phase < e and e - phase >= need[i] for s, e in qwin[q])

    def advance_credit(t0, t1) -> None:
        # [t0, t1) 内无事件:门状态、积压、发送状态均恒定,可解析推进
        dt = t1 - t0
        if dt <= 0:
            return
        phase0 = (t0 + offset) % cycle
        for q, sh in shapers.items():
            if not gate_open(q, phase0):
                continue            # 本题规定:门关闭期间信用冻结
            c = credit[q]
            if active_q == q and t0 < busy_until:
                c = max(Fraction(sh.lo_credit), c + sh.send_slope * dt)
            elif backlog[q] > 0:
                c = min(Fraction(sh.hi_credit), c + sh.idle_slope * dt)
            elif c > 0:
                c = Fraction(0)     # 空队列正信用归 0
            elif c < 0:
                c = min(Fraction(0), c + sh.idle_slope * dt)  # 负信用恢复到 0
            credit[q] = c

    def jump_to(new_t) -> None:
        nonlocal t
        advance_credit(t, new_t)
        t = new_t

    def next_gate_edge(after):
        phase = (after + offset) % cycle
        best = None
        for wins in qwin:
            for s, e in wins:
                for cand in (s, e):
                    d = (Fraction(cand) - phase) % cycle
                    if d == 0:
                        d = Fraction(cycle)
                    ct = after + d
                    if best is None or ct < best:
                        best = ct
        return best

    while True:
        events += 1
        if events > max_events:
            raise BudgetExhaustedError(
                f"事件预算 max_events={max_events} 耗尽:重放被截断,"
                f"这不是“永不可发”的证明"
            )
        while pi < n and arrive_of[order[pi]] <= t:
            i = order[pi]
            pi += 1
            if transmittable[i]:
                ready.append(i)
                if queue_of[i] in shapers:
                    backlog[queue_of[i]] += 1
        if not ready:
            if pi >= n:
                break
            cands = [Fraction(arrive_of[order[pi]])]
            if busy_until > t:
                cands.append(busy_until)
            edge = next_gate_edge(t)
            if edge is not None:
                cands.append(edge)
            jump_to(min(cands))
            continue
        if t < busy_until:
            cands = [busy_until]
            if pi < n:
                cands.append(Fraction(arrive_of[order[pi]]))
            edge = next_gate_edge(t)
            if edge is not None:
                cands.append(edge)
            jump_to(min(cands))
            continue
        active_q = None
        phase = (t + offset) % cycle

        eligible = []
        for i in ready:
            q = queue_of[i]
            if not fits(q, phase, i):
                continue
            sh = shapers.get(q)
            if sh is not None and credit[q] < 0:
                continue            # 信用为负,不得开始
            eligible.append(i)
        if eligible:
            chosen = min(eligible, key=lambda i: (queue_of[i], i))
            for i in eligible:
                if i != chosen:
                    note(i, WaitReason.OUTRANKED)
            start_of[chosen] = t
            end = t + dur_of[chosen]
            end_of[chosen] = end
            busy_until = end
            active_q = queue_of[chosen]
            ready.remove(chosen)
            if active_q in shapers:
                backlog[active_q] -= 1
            for i in ready:
                note(i, WaitReason.LINK_BUSY)
            cands = [end]
            if pi < n:
                cands.append(Fraction(arrive_of[order[pi]]))
            edge = next_gate_edge(t)
            if edge is not None:
                cands.append(edge)
            jump_to(min(cands))
            continue

        # 当前时刻无可开始帧:记录原因,跳到下一事件点
        for i in ready:
            q = queue_of[i]
            if not gate_open(q, phase):
                note(i, WaitReason.GATE_CLOSED)
                continue
            if not fits(q, phase, i):
                note(i, WaitReason.INSUFFICIENT_WINDOW)
            sh = shapers.get(q)
            if sh is not None and credit[q] < 0:
                note(i, WaitReason.CREDIT_NEGATIVE)
        cands = []
        if pi < n:
            cands.append(Fraction(arrive_of[order[pi]]))
        edge = next_gate_edge(t)
        if edge is not None:
            cands.append(edge)
        for q, sh in shapers.items():
            if credit[q] < 0 and backlog[q] > 0 and gate_open(q, phase):
                cands.append(t + (-credit[q]) / sh.idle_slope)  # 信用到 0 的精确时刻
        if not cands:
            raise TSNGateError("内部错误:无后续事件(不应发生)")
        nxt = min(cands)
        if nxt <= t:
            raise TSNGateError("内部错误:事件时间未能推进(不应发生)")
        jump_to(nxt)

    frames_out = []
    for i in range(n):
        if not transmittable[i]:
            frames_out.append(FrameResult(
                seq=i, queue=queue_of[i], status=FrameStatus.NEVER_TRANSMITTABLE,
                arrive_ns=arrive_of[i], duration_ns=dur_of[i],
                start_ns=None, end_ns=None, waited_ns=None, wait_reasons=(),
                deadline_ns=deadline_of[i], deadline_violated=False,
            ))
        else:
            violated = deadline_of[i] is not None and end_of[i] > deadline_of[i]
            frames_out.append(FrameResult(
                seq=i, queue=queue_of[i], status=FrameStatus.SENT,
                arrive_ns=arrive_of[i], duration_ns=dur_of[i],
                start_ns=_as_output_time(start_of[i]),
                end_ns=_as_output_time(end_of[i]),
                waited_ns=_as_output_time(start_of[i] - arrive_of[i]),
                wait_reasons=tuple(reasons[i]),
                deadline_ns=deadline_of[i], deadline_violated=violated,
            ))
    return ReplayResult(
        frames=tuple(frames_out),
        sent_count=sum(1 for i in range(n) if transmittable[i]),
        never_count=sum(1 for i in range(n) if not transmittable[i]),
        deadline_violations=tuple(
            i for i in range(n) if transmittable[i] and frames_out[i].deadline_violated
        ),
    )
