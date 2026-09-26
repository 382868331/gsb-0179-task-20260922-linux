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
- 可选的信用整形(credit-based shaping):经 replay(..., shapers=...) 为某个
  队列配置 CreditShaper 后,该队列除门控外还须信用非负才能开始帧:
    * 有积压且未发送时信用按 idle_slope(>0)增加,发送时按 send_slope(<0)减少;
    * 队列变空时正信用立即归 0,负信用继续按 idle_slope 恢复到 0 为止;
    * 门关闭期间信用冻结(本题规定,不采用其他标准行为);
    * 信用可被 hi_credit / lo_credit 上下限截断;
    * 信用与时间为精确分数(fractions.Fraction),信用归零时刻由事件跳跃直接
      算出,不做小步轮询,无累计误差。整形队列的开始/结束时刻可能是 Fraction。

本模块只实现本题契约的有限子集,不宣称兼容完整 IEEE 802.1Qbv。
"""

from __future__ import annotations

import math
from bisect import bisect_right
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
    CREDIT_NEGATIVE = "credit-negative"        # 整形队列信用为负,等待恢复到 0


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


def _require_exact_number(value, where: str) -> Fraction:
    """校验信用/斜率参数:接受 int 与 Fraction,拒绝 bool 与一切 float。"""
    if isinstance(value, bool):
        raise TSNGateError(f"{where}: 需要 int/Fraction,拒绝 bool({value!r})")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise TSNGateError(f"{where}: 拒绝 NaN/Infinity({value!r})")
        raise TSNGateError(f"{where}: 需要精确数值(int/Fraction),拒绝 float({value!r})")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Fraction):
        return value
    raise TSNGateError(
        f"{where}: 需要 int/Fraction,得到 {type(value).__name__}({value!r})"
    )


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
class GateSchedule:
    """周期门表 + 链路参数 + 本地时钟偏差。"""

    cycle_ns: int
    windows: tuple
    link_rate_bits_per_ns: int
    guard_ns: int = 0
    clock_offset_ns: int = 0

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


@dataclass(frozen=True)
class CreditShaper:
    """单个队列的信用整形参数(斜率一正一负,信用上下限可选)。

    - idle_slope: 有积压且未发送时每 ns 增加的信用,必须为正(int 或 Fraction)。
    - send_slope: 发送期间每 ns 减少的信用,必须为负(int 或 Fraction)。
    - hi_credit: 信用上限(>= 0),None 表示不设上限。
    - lo_credit: 信用下限(<= 0),None 表示不设下限。
    信用与时间按精确分数演算;门关闭期间信用冻结。
    """

    queue: int
    idle_slope: object
    send_slope: object
    hi_credit: object = None
    lo_credit: object = None

    def __post_init__(self):
        _require_int(self.queue, "CreditShaper.queue", lo=0, hi=MAX_QUEUES - 1)
        idle = _require_exact_number(self.idle_slope, "CreditShaper.idle_slope")
        send = _require_exact_number(self.send_slope, "CreditShaper.send_slope")
        if idle <= 0:
            raise TSNGateError(
                f"CreditShaper.idle_slope: {self.idle_slope!r} 必须为正(一正一负)"
            )
        if send >= 0:
            raise TSNGateError(
                f"CreditShaper.send_slope: {self.send_slope!r} 必须为负(一正一负)"
            )
        hi = lo = None
        if self.hi_credit is not None:
            hi = _require_exact_number(self.hi_credit, "CreditShaper.hi_credit")
            if hi < 0:
                raise TSNGateError(f"CreditShaper.hi_credit: {self.hi_credit!r} 必须 >= 0")
        if self.lo_credit is not None:
            lo = _require_exact_number(self.lo_credit, "CreditShaper.lo_credit")
            if lo > 0:
                raise TSNGateError(f"CreditShaper.lo_credit: {self.lo_credit!r} 必须 <= 0")
        object.__setattr__(self, "idle_slope", idle)
        object.__setattr__(self, "send_slope", send)
        object.__setattr__(self, "hi_credit", hi)
        object.__setattr__(self, "lo_credit", lo)


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


def replay(arrivals, gate_schedule, *, max_events=DEFAULT_MAX_EVENTS,
           shapers=None) -> ReplayResult:
    """重放到达序列在周期门控表下的发送过程,返回确定性结果。

    纯函数:不修改入参,失败时抛出 TSNGateError(或其子类),不留下任何部分结果。
    shapers 为可选的 CreditShaper 序列(每队列至多一个);不提供时行为与
    旧的无整形模式完全一致。
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

    if shapers is None:
        shaper_list = []
    else:
        try:
            shaper_list = list(shapers)
        except TypeError as exc:
            raise TSNGateError(f"shapers: 不可迭代({exc})") from exc
    shaper_of: list = [None] * MAX_QUEUES
    for k, sp in enumerate(shaper_list):
        if not isinstance(sp, CreditShaper):
            raise TSNGateError(
                f"shapers[{k}]: 需要 CreditShaper,得到 {type(sp).__name__}"
            )
        if shaper_of[sp.queue] is not None:
            raise TSNGateError(
                f"shapers[{k}].queue: 队列 {sp.queue} 的信用整形器重复"
            )
        shaper_of[sp.queue] = sp
    any_shaper = any(sp is not None for sp in shaper_of)

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
    open_per_cycle = [sum(e - s for s, e in ws) for ws in qwin]

    def open_time(q: int, a, b):
        """[a, b) 内队列 q 门开着的总时长(精确,支持 Fraction;关门段不计)。"""
        ws = qwin[q]
        if b <= a or not ws:
            return 0
        opc = open_per_cycle[q]
        total = 0
        cur = a + offset
        end = b + offset
        while cur < end:
            base = (cur // cycle) * cycle
            phase = cur - base
            if phase == 0 and end - cur >= cycle:
                k = (end - cur) // cycle
                total += k * opc
                cur += k * cycle
                continue
            for s, e in ws:
                lo = base + (phase if phase > s else s)
                hi = base + e
                if hi > end:
                    hi = end
                if hi > lo:
                    total += hi - lo
            cur = base + cycle
        return total

    def time_for_open(q: int, a, x):
        """最小 τ >= a 使 [a, τ) 内队列 q 的开门时长恰为 x(x > 0,精确分数)。"""
        ws = qwin[q]
        cur = a + offset
        while True:
            base = (cur // cycle) * cycle
            phase = cur - base
            for s, e in ws:
                if phase < e:
                    start = phase if phase > s else s
                    avail = e - start
                    if x <= avail:
                        return base + start + x - offset
                    x -= avail
            cur = base + cycle

    need = [dur_of[i] + guard for i in range(n)]
    # 可证明的“永不可发”:不存在任何窗口容得下 时长+guard
    transmittable = [need[i] <= maxwin[queue_of[i]] for i in range(n)]

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

    # 每个队列可发帧的到达时刻(升序),用于在信用结算中定位“中途到达”
    q_arrivals = [[] for _ in range(MAX_QUEUES)]
    for i in order:
        if transmittable[i]:
            q_arrivals[queue_of[i]].append(arrive_of[i])

    # 信用整形状态(仅 shaper_of[q] 非空的队列使用;信用为精确 Fraction)
    credit = [Fraction(0)] * MAX_QUEUES
    credit_t = [0] * MAX_QUEUES      # 信用上次结算时刻
    xmit_until = [0] * MAX_QUEUES    # 该队列当前/最近一次发送的结束时刻

    def update_credit(q: int, t_new) -> None:
        """把队列 q 的信用从 credit_t[q] 结算到 t_new。

        区间内的状态变化只可能来自未释放的到达(发送/释放都发生在事件点,
        即结算边界上);按该队列第一个未释放到达时刻分段结算。门关闭段冻结。
        """
        t_old = credit_t[q]
        if t_new <= t_old:
            return
        sp = shaper_of[q]
        c = credit[q]
        seg = t_old
        if xmit_until[q] > t_old:
            xe = xmit_until[q] if xmit_until[q] < t_new else t_new
            c += sp.send_slope * open_time(q, t_old, xe)
            if sp.lo_credit is not None and c < sp.lo_credit:
                c = sp.lo_credit
            seg = xe
        if t_new > seg:
            has_now = any(queue_of[i] == q for i in ready)
            # (t_old, t_new] 内该队列第一个未释放(且可发)的到达时刻
            arr = q_arrivals[q]
            k = bisect_right(arr, t_old)
            a1 = arr[k] if k < len(arr) and arr[k] <= t_new else None

            def rise(a, b, c0):
                c1 = c0 + sp.idle_slope * open_time(q, a, b)
                if sp.hi_credit is not None and c1 > sp.hi_credit:
                    c1 = sp.hi_credit
                return c1

            def empty_seg(a, b, c0):
                if c0 > 0:
                    return Fraction(0)      # 空队列:正信用立即归 0
                if c0 < 0:
                    c1 = c0 + sp.idle_slope * open_time(q, a, b)
                    if c1 > 0:
                        c1 = Fraction(0)  # 空队列:负信用恢复到 0 为止
                    return c1
                return c0

            if has_now or (a1 is not None and a1 < seg):
                c = rise(seg, t_new, c)
            elif a1 is None:
                c = empty_seg(seg, t_new, c)
            else:
                # 到达之前按空队列结算;到达时刻起按积压结算
                c = empty_seg(seg, a1, c)
                c = rise(a1, t_new, c)
        credit[q] = c
        credit_t[q] = t_new

    def advance_to(t_new) -> None:
        nonlocal t
        if any_shaper:
            for q in range(MAX_QUEUES):
                if shaper_of[q] is not None:
                    update_credit(q, t_new)
        t = t_new

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
            advance_to(max(t, busy_until, arrive_of[order[pi]]))
            continue
        if t < busy_until:
            advance_to(busy_until)
            continue

        phase = (t + offset) % cycle
        ready.sort(key=lambda i: (queue_of[i], i))
        chosen = None
        outranked = []
        for i in ready:
            q = queue_of[i]
            fits = any(s <= phase < e and e - phase >= need[i] for s, e in qwin[q])
            if fits and shaper_of[q] is not None and credit[q] < 0:
                fits = False        # 整形队列:信用为负不得开始帧
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
            xmit_until[queue_of[chosen]] = end
            ready.remove(chosen)
            for i in ready:
                note(i, WaitReason.LINK_BUSY)
            advance_to(end)
            continue

        # 没有帧能在当前时刻开始:记录原因,推进到下一个事件点
        for i in ready:
            q = queue_of[i]
            open_now = any(s <= phase < e for s, e in qwin[q])
            if not open_now:
                note(i, WaitReason.GATE_CLOSED)
            else:
                fits = any(s <= phase < e and e - phase >= need[i]
                           for s, e in qwin[q])
                if not fits:
                    note(i, WaitReason.INSUFFICIENT_WINDOW)
                if shaper_of[q] is not None and credit[q] < 0:
                    note(i, WaitReason.CREDIT_NEGATIVE)
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
        for i in ready:
            q = queue_of[i]
            sp = shaper_of[q]
            if sp is not None and credit[q] < 0 and open_per_cycle[q] > 0:
                # 事件跳跃:直接算出信用按 idle_slope 恢复到 0 的精确时刻
                cand_t = time_for_open(q, t, -credit[q] / sp.idle_slope)
                if nxt is None or cand_t < nxt:
                    nxt = cand_t
        if nxt is None or nxt <= t:
            raise TSNGateError("内部错误:事件时间未能推进(不应发生)")
        advance_to(nxt)

    def _norm(v):
        # 整形队列的时间可能是 Fraction;恰为整数时归一为 int,保持旧输出形态
        if isinstance(v, Fraction) and v.denominator == 1:
            return v.numerator
        return v

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
                start_ns=_norm(start_of[i]), end_ns=_norm(end_of[i]),
                waited_ns=_norm(start_of[i] - arrive_of[i]),
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
