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

预定配置切换(config_change)
--------------------------
- ConfigChange(schedule, effective_ns, accepted_ns) 表示一次预定的门表切换:
  请求在 accepted_ns(参考时钟)被接受,新表在 effective_ns(参考时钟)生效。
  接受请求时先整体校验新表与生效时刻,任一不合法即拒绝,不留部分变更。
- effective_ns 必须落在旧周期边界:(effective_ns + 旧表.clock_offset_ns)
  % 旧表.cycle_ns == 0(边界按本地时钟相位判定,既有偏差统一参与换算)。
- 新表的 clock_offset_ns 与 link_rate_bits_per_ns 必须与旧表一致:
  时钟偏差与链路速率是物理属性,不随门表配置切换。
- 在生效时刻尚未开始的帧一律按新门表判断,排队选择规则不变;
  已经开始的不可抢占帧不受切换影响,继续发送到完成。
- accepted_ns 之后,不允许开始任何 end+guard 越过 effective_ns 的旧表发送
  (不借旧表预约未来时段)。注:由于生效时刻对齐旧周期边界且窗口不跨周期,
  旧表发送的 end+guard 数学上必然不超过生效时刻,该约束是显式安全网;
  测试中的逐纳秒参考扫描会对每条输出验证这一性质。
- “永不可发”按两张表重新计算,不直接丢帧:容得下新表的帧可以等到生效后发送;
  仅旧表容得下的帧,若到达时已没有任何可行的旧表开始时刻,则(最迟在错过
  最后一个可行开始时刻时)被证明为 NEVER_TRANSMITTABLE。

本模块只实现本题契约的有限子集,不宣称兼容完整 IEEE 802.1Qbv。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

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
    "ConfigChange",
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
class ConfigChange:
    """一次预定的门表切换:新表在 effective_ns(参考时钟)生效。

    - schedule: 整张新门表(构造 GateSchedule 时已完成自身校验)。
    - effective_ns: 生效参考时刻,必须落在旧周期边界;该校验需要旧表,
      因此在 replay() 中接受请求时进行。
    - accepted_ns: 请求被接受的参考时刻,不得晚于 effective_ns;
      此前已开始发送的帧不受切换影响,继续发送到完成。
    """

    schedule: GateSchedule
    effective_ns: int
    accepted_ns: int = 0

    def __post_init__(self):
        if not isinstance(self.schedule, GateSchedule):
            raise TSNGateError(
                f"ConfigChange.schedule: 需要 GateSchedule,"
                f"得到 {type(self.schedule).__name__}"
            )
        _require_int(self.effective_ns, "ConfigChange.effective_ns", lo=0)
        _require_int(self.accepted_ns, "ConfigChange.accepted_ns", lo=0)
        if self.accepted_ns > self.effective_ns:
            raise TSNGateError(
                f"ConfigChange.accepted_ns: {self.accepted_ns} 晚于生效时刻 "
                f"effective_ns({self.effective_ns})"
            )


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


def _queue_windows(schedule: GateSchedule):
    """每队列的窗口列表(按起点排序)及最长窗口长度。"""
    qwin = [[] for _ in range(MAX_QUEUES)]
    for w in schedule.windows:
        for q in w.queues:
            qwin[q].append((w.start_ns, w.end_ns))
    for q in range(MAX_QUEUES):
        qwin[q].sort()
    maxwin = [max((e - s for s, e in ws), default=0) for ws in qwin]
    return qwin, maxwin


def _latest_old_start(queue: int, need_q: int, t_max: int, qwin, cycle: int,
                      offset: int) -> int | None:
    """旧表下不晚于 t_max 的最晚可行开始时刻(参考时钟);不存在返回 None。

    可行开始:本地相位 p = (t + offset) % cycle 落在某窗口 [s, e) 内
    且 e - p >= need_q(时长+guard 完整落入窗口剩余部分)。
    """
    if t_max < 0:
        return None
    best = None
    for s, e in qwin[queue]:
        if e - s < need_q:
            continue
        hi = e - need_q  # 可行相位区间 [s, hi]
        p0 = (t_max + offset) % cycle
        back = 0 if s <= p0 <= hi else (p0 - hi) % cycle
        cand = t_max - back
        if best is None or cand > best:
            best = cand
    return best


def replay(arrivals, gate_schedule, *, config_change=None,
           max_events=DEFAULT_MAX_EVENTS) -> ReplayResult:
    """重放到达序列在周期门控表下的发送过程,返回确定性结果。

    纯函数:不修改入参,失败时抛出 TSNGateError(或其子类),不留下任何部分结果。
    config_change 给定一次预定门表切换;为 None 时即单表重放。
    """
    _require_int(max_events, "max_events", lo=1)
    if not isinstance(gate_schedule, GateSchedule):
        raise TSNGateError(
            f"gate_schedule: 需要 GateSchedule,得到 {type(gate_schedule).__name__}"
        )
    if config_change is not None and not isinstance(config_change, ConfigChange):
        raise TSNGateError(
            f"config_change: 需要 ConfigChange,得到 {type(config_change).__name__}"
        )
    try:
        arrivals = list(arrivals)
    except TypeError as exc:
        raise TSNGateError(f"arrivals: 不可迭代({exc})") from exc

    rate = gate_schedule.link_rate_bits_per_ns
    guard = gate_schedule.guard_ns
    cycle = gate_schedule.cycle_ns
    offset = gate_schedule.clock_offset_ns

    # 接受切换请求时先整体校验新表与生效时刻;任一不合法即拒绝,不留部分变更
    if config_change is not None:
        new_sched = config_change.schedule
        eff = config_change.effective_ns
        acc = config_change.accepted_ns
        if new_sched.clock_offset_ns != offset:
            raise TSNGateError(
                "config_change.schedule.clock_offset_ns: 新表偏差 "
                f"{new_sched.clock_offset_ns} 与旧表 {offset} 不一致;"
                "本地时钟偏差是物理属性,不随门表切换"
            )
        if new_sched.link_rate_bits_per_ns != rate:
            raise TSNGateError(
                "config_change.schedule.link_rate_bits_per_ns: 新表速率 "
                f"{new_sched.link_rate_bits_per_ns} 与旧表 {rate} 不一致;"
                "链路速率是物理属性,不随门表切换"
            )
        if (eff + offset) % cycle != 0:
            raise TSNGateError(
                f"config_change.effective_ns: {eff} 未落在旧周期边界"
                f"(要求 (effective_ns + {offset}) % {cycle} == 0)"
            )

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

    qwin, maxwin = _queue_windows(gate_schedule)
    need = [dur_of[i] + guard for i in range(n)]

    if config_change is None:
        # 可证明的“永不可发”:不存在任何窗口容得下 时长+guard
        transmittable = [need[i] <= maxwin[queue_of[i]] for i in range(n)]
        fits_new = None
        last_old = None
    else:
        qwin_new, maxwin_new = _queue_windows(new_sched)
        new_cycle = new_sched.cycle_ns
        need_new = [dur_of[i] + new_sched.guard_ns for i in range(n)]
        fits_new = [need_new[i] <= maxwin_new[queue_of[i]] for i in range(n)]
        # 每帧在旧表下的最晚可行开始时刻:accepted_ns 之后开始的旧表发送
        # 不得让 end+guard 越过生效时刻
        last_old = [
            _latest_old_start(queue_of[i], need[i],
                              max(acc - 1, eff - need[i]), qwin, cycle, offset)
            for i in range(n)
        ]
        # “永不可发”按两张表重算:容得下新表即可等生效后发送;
        # 否则须存在不早于到达时刻的可行旧表开始
        transmittable = [
            fits_new[i] or (last_old[i] is not None and last_old[i] >= arrive_of[i])
            for i in range(n)
        ]

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
    never_late: set[int] = set()   # 重放中证明永不可发(错过最后旧表开始时刻且容不下新表)

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
        if config_change is not None:
            still = []
            for i in ready:
                if not fits_new[i] and t > last_old[i]:
                    # 旧表已无可行开始且容不下新表:此刻证明永不可发,不直接丢帧
                    never_late.add(i)
                else:
                    still.append(i)
            ready = still
        if not ready:
            if pi >= n:
                break
            t = max(t, busy_until, arrive_of[order[pi]])
            continue
        if t < busy_until:
            t = busy_until
            continue

        use_new = config_change is not None and t >= eff
        if use_new:
            cur_qwin, cur_cycle, cur_need = qwin_new, new_cycle, need_new
        else:
            cur_qwin, cur_cycle, cur_need = qwin, cycle, need
        phase = (t + offset) % cur_cycle
        ready.sort(key=lambda i: (queue_of[i], i))
        chosen = None
        outranked = []
        for i in ready:
            q = queue_of[i]
            fits = any(s <= phase < e and e - phase >= cur_need[i]
                       for s, e in cur_qwin[q])
            if (fits and not use_new and config_change is not None
                    and t >= acc and t + need[i] > eff):
                # 不借旧表预约跨过生效时刻的时段(连同 guard 须在切换前完成)
                fits = False
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
            open_now = any(s <= phase < e for s, e in cur_qwin[q])
            note(i, WaitReason.INSUFFICIENT_WINDOW if open_now else WaitReason.GATE_CLOSED)
        nxt = arrive_of[order[pi]] if pi < n else None
        if config_change is not None and t < eff and (nxt is None or eff < nxt):
            nxt = eff  # 生效时刻本身是事件点:此后按新表判断
        for i in ready:
            for s, e in cur_qwin[queue_of[i]]:
                for cand in (s, e):
                    d = (cand - phase) % cur_cycle
                    if d == 0:
                        d = cur_cycle
                    cand_t = t + d
                    if config_change is not None and t < eff and cand_t > eff:
                        continue  # 旧表在生效后失效,eff 已是候选事件点
                    if nxt is None or cand_t < nxt:
                        nxt = cand_t
        if nxt is None or nxt <= t:
            raise TSNGateError("内部错误:事件时间未能推进(不应发生)")
        t = nxt

    frames_out = []
    for i in range(n):
        if not transmittable[i] or i in never_late:
            frames_out.append(FrameResult(
                seq=i, queue=queue_of[i], status=FrameStatus.NEVER_TRANSMITTABLE,
                arrive_ns=arrive_of[i], duration_ns=dur_of[i],
                start_ns=None, end_ns=None, waited_ns=None,
                wait_reasons=tuple(reasons[i]),
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
        sent_count=sum(1 for i in range(n) if start_of[i] is not None),
        never_count=sum(1 for i in range(n) if start_of[i] is None),
        deadline_violations=tuple(
            i for i in range(n)
            if start_of[i] is not None and frames_out[i].deadline_violated
        ),
    )
