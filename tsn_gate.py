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

本模块只实现本题契约的有限子集,不宣称兼容完整 IEEE 802.1Qbv。

帧抢占(可选,GateSchedule.preemption 不为 None 时启用)
----------------------------------------------------
  * 帧分两类:TrafficClass.EXPRESS(紧急帧,原子发送)与
    TrafficClass.PREEMPTABLE(普通帧,按固定字节片段发送,默认类别)。
  * 普通帧只能在片段边界处暂停;紧急帧到达后等到当前片段结束
    (下一个合法边界)再发送,不会截断已开始的片段。
  * 每个片段在线上占 ceil((片段载荷 + fragment_overhead_bytes) * 8 / rate) ns;
    片段越多额外开销越大,被暂停后恢复同样按新片段计入开销。
  * 门关闭不会截断已开始的片段,但剩余窗口容不下 片段时长+guard 时
    不能再开新片段;被门拦住的普通帧在窗口重开后继续。
    紧急帧也必须整体满足自己队列的门(时长+guard 落入当前窗口剩余部分)。
  * 多个被暂停的普通帧按 (原开始时刻, 到达序号) 顺序恢复;
    暂停帧优先于尚未开始的普通帧。
  * 同一时刻的事件处理次序固定为:片段完成 -> 门状态切换(半开区间自然定义)
    -> 帧到达 -> 调度决策。所有时间仍为整数纳秒。
  * 不启用抢占(preemption=None)时,行为与上一迭代完全一致。
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
    "TrafficClass",
    "PreemptionConfig",
    "Frame",
    "GateWindow",
    "GateSchedule",
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
    PREEMPTED = "preempted"                    # 被紧急帧在片段边界暂停,等待恢复


class FrameStatus(str, Enum):
    SENT = "sent"
    NEVER_TRANSMITTABLE = "never-transmittable"


class TrafficClass(str, Enum):
    EXPRESS = "express"            # 紧急帧:原子发送,可在片段边界抢占普通帧
    PREEMPTABLE = "preemptable"    # 普通帧:按固定片段发送,可被抢占(默认)


@dataclass(frozen=True)
class PreemptionConfig:
    """帧抢占配置:固定片段载荷字节数 + 每片段附加开销字节数。"""

    fragment_bytes: int               # 单片载荷上限,>= 1
    fragment_overhead_bytes: int = 0  # 每片附加开销(暂停恢复时计入),>= 0

    def __post_init__(self):
        _require_int(self.fragment_bytes, "PreemptionConfig.fragment_bytes", lo=1)
        _require_int(self.fragment_overhead_bytes,
                     "PreemptionConfig.fragment_overhead_bytes", lo=0)


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
    traffic_class 缺省为 PREEMPTABLE(普通帧);仅在 GateSchedule.preemption
    启用时影响行为,未启用抢占时该字段被校验但不改变结果。
    """

    queue: int
    arrive_ns: int
    size_bytes: int
    deadline_ns: int | None = None
    traffic_class: TrafficClass = TrafficClass.PREEMPTABLE


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
    """周期门表 + 链路参数 + 本地时钟偏差 + 可选帧抢占配置。"""

    cycle_ns: int
    windows: tuple
    link_rate_bits_per_ns: int
    guard_ns: int = 0
    clock_offset_ns: int = 0
    preemption: PreemptionConfig | None = None

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
        if self.preemption is not None and not isinstance(self.preemption,
                                                          PreemptionConfig):
            raise TSNGateError(
                f"GateSchedule.preemption: 需要 PreemptionConfig 或 None,得到 "
                f"{type(self.preemption).__name__}"
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
    fragments: tuple = ()         # 各片段 (start_ns, end_ns),按时间升序;未发送为 ()
    preempted_count: int = 0      # 被紧急帧抢占而暂停的次数(仅抢占模式)
    overhead_ns: int = 0          # 片段化引入的额外线上时长(各片段时长和 - duration_ns)


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
    queue_of, arrive_of, size_of = [], [], []
    dur_of, deadline_of, cls_of = [], [], []
    for i, f in enumerate(arrivals):
        if not isinstance(f, Frame):
            raise TSNGateError(f"arrivals[{i}]: 需要 Frame,得到 {type(f).__name__}")
        queue_of.append(_require_int(f.queue, f"arrivals[{i}].queue",
                                     lo=0, hi=MAX_QUEUES - 1))
        arrive_of.append(_require_int(f.arrive_ns, f"arrivals[{i}].arrive_ns", lo=0))
        size = _require_int(f.size_bytes, f"arrivals[{i}].size_bytes", lo=1)
        size_of.append(size)
        d = f.deadline_ns
        if d is not None:
            d = _require_int(d, f"arrivals[{i}].deadline_ns", lo=0)
        deadline_of.append(d)
        cls = f.traffic_class
        if not isinstance(cls, TrafficClass):
            raise TSNGateError(
                f"arrivals[{i}].traffic_class: 需要 TrafficClass,得到 "
                f"{type(cls).__name__}({cls!r})"
            )
        cls_of.append(cls)
        dur_of.append((size * 8 + rate - 1) // rate)  # ceil,精确整数

    # 每个队列的窗口列表(按起点排序)及最长窗口长度
    qwin = [[] for _ in range(MAX_QUEUES)]
    for w in gate_schedule.windows:
        for q in w.queues:
            qwin[q].append((w.start_ns, w.end_ns))
    for q in range(MAX_QUEUES):
        qwin[q].sort()
    maxwin = [max((e - s for s, e in ws), default=0) for ws in qwin]

    if gate_schedule.preemption is not None:
        return _replay_preemptive(
            n, queue_of, arrive_of, size_of, dur_of, deadline_of, cls_of,
            qwin, maxwin, gate_schedule, max_events,
        )

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


def _replay_preemptive(n, queue_of, arrive_of, size_of, dur_of, deadline_of,
                       cls_of, qwin, maxwin, gate_schedule, max_events):
    """启用帧抢占的重放路径(GateSchedule.preemption 不为 None)。

    事件驱动;同刻处理次序:片段完成 -> 门状态(半开区间)-> 帧到达 -> 调度决策。
    调度优先级:紧急帧(队列号, 到达序号) > 暂停中的普通帧(原开始时刻, 序号)
    > 未开始的普通帧(队列号, 到达序号)。
    """
    rate = gate_schedule.link_rate_bits_per_ns
    guard = gate_schedule.guard_ns
    cycle = gate_schedule.cycle_ns
    offset = gate_schedule.clock_offset_ns
    cfg = gate_schedule.preemption
    frag_payload = cfg.fragment_bytes
    frag_ovh = cfg.fragment_overhead_bytes

    is_express = [cls_of[i] is TrafficClass.EXPRESS for i in range(n)]

    def frag_wire_dur(i: int, remaining_bytes: int) -> int:
        payload = min(remaining_bytes, frag_payload)
        return ((payload + frag_ovh) * 8 + rate - 1) // rate

    # 可证明的“永不可发”:紧急帧要求 时长+guard 落入某窗口;
    # 普通帧只要求最大片段(含开销)+guard 落入某窗口(可跨窗口暂停续传)。
    transmittable = []
    for i in range(n):
        if is_express[i]:
            need = dur_of[i] + guard
        else:
            need = frag_wire_dur(i, size_of[i]) + guard
        transmittable.append(need <= maxwin[queue_of[i]])

    reasons: list[list[WaitReason]] = [[] for _ in range(n)]

    def note(i: int, r: WaitReason) -> None:
        if r not in reasons[i]:
            reasons[i].append(r)

    def fits(q: int, phase: int, need: int) -> bool:
        return any(s <= phase < e and e - phase >= need for s, e in qwin[q])

    def open_now(q: int, phase: int) -> bool:
        return any(s <= phase < e for s, e in qwin[q])

    order = sorted(range(n), key=lambda i: (arrive_of[i], i))
    start_of: list[int | None] = [None] * n
    end_of: list[int | None] = [None] * n
    first_start: list[int | None] = [None] * n
    remaining = list(size_of)
    fragments: list[list[tuple[int, int]]] = [[] for _ in range(n)]
    frag_time = [0] * n
    preempted_count = [0] * n

    ready_exp: list[int] = []   # 已到达未发送的紧急帧
    ready_pre: list[int] = []   # 已到达未开始的普通帧
    paused: list[int] = []      # 已开始、被暂停的普通帧(恢复时按原开始顺序)
    pi = 0
    t = 0
    busy_until = 0
    current: int | None = None  # 正在发送且尚有剩余字节的普通帧
    events = 0

    while True:
        events += 1
        if events > max_events:
            raise BudgetExhaustedError(
                f"事件预算 max_events={max_events} 耗尽:重放被截断,"
                f"这不是“永不可发”的证明"
            )
        # 1) 片段完成(同刻最先):有剩余字节的普通帧进入暂停集
        if t < busy_until:
            t = busy_until
            if current is not None and remaining[current] > 0:
                paused.append(current)
            current = None
        # 2) 门状态由半开区间在决策时自然体现;3) 同刻到达入队
        while pi < n and arrive_of[order[pi]] <= t:
            i = order[pi]
            pi += 1
            if transmittable[i]:
                (ready_exp if is_express[i] else ready_pre).append(i)
        if not ready_exp and not ready_pre and not paused:
            if pi >= n:
                break
            t = max(t, arrive_of[order[pi]])
            continue

        # 4) 调度决策:紧急 > 暂停普通 > 新普通
        phase = (t + offset) % cycle
        chosen = None
        chosen_from = None
        for i in sorted(ready_exp, key=lambda i: (queue_of[i], i)):
            if fits(queue_of[i], phase, dur_of[i] + guard):
                chosen = i
                chosen_from = "exp"
                break
        if chosen is None:
            paused.sort(key=lambda i: (first_start[i], i))
            for i in paused:
                if fits(queue_of[i], phase,
                        frag_wire_dur(i, remaining[i]) + guard):
                    chosen = i
                    chosen_from = "paused"
                    break
        if chosen is None:
            for i in sorted(ready_pre, key=lambda i: (queue_of[i], i)):
                if fits(queue_of[i], phase,
                        frag_wire_dur(i, remaining[i]) + guard):
                    chosen = i
                    chosen_from = "pre"
                    break

        if chosen is not None:
            if chosen_from == "exp":
                d = dur_of[chosen]
                ready_exp.remove(chosen)
                remaining[chosen] = 0
                for j in paused:
                    if fits(queue_of[j], phase,
                            frag_wire_dur(j, remaining[j]) + guard):
                        note(j, WaitReason.PREEMPTED)
                        preempted_count[j] += 1
                for j in ready_exp:
                    if fits(queue_of[j], phase, dur_of[j] + guard):
                        note(j, WaitReason.OUTRANKED)
            else:
                d = frag_wire_dur(chosen, remaining[chosen])
                remaining[chosen] -= min(remaining[chosen], frag_payload)
                if chosen_from == "paused":
                    paused.remove(chosen)
                else:
                    ready_pre.remove(chosen)
                    first_start[chosen] = t
            end = t + d
            fragments[chosen].append((t, end))
            frag_time[chosen] += d
            if start_of[chosen] is None:
                start_of[chosen] = t
            if remaining[chosen] == 0:
                end_of[chosen] = end
            else:
                current = chosen
            busy_until = end
            for j in ready_exp + ready_pre + paused:
                note(j, WaitReason.LINK_BUSY)
            continue  # t 由循环顶端推进到 busy_until(片段完成)

        # 没有帧能在当前时刻开始:记录原因,推进到下一个事件点
        for i in ready_exp + ready_pre + paused:
            q = queue_of[i]
            note(i, WaitReason.INSUFFICIENT_WINDOW if open_now(q, phase)
                 else WaitReason.GATE_CLOSED)
        nxt = arrive_of[order[pi]] if pi < n else None
        for i in ready_exp + ready_pre + paused:
            for s, e in qwin[queue_of[i]]:
                for cand in (s, e):
                    dd = (cand - phase) % cycle
                    if dd == 0:
                        dd = cycle
                    cand_t = t + dd
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
                fragments=tuple(fragments[i]),
                preempted_count=preempted_count[i],
                overhead_ns=frag_time[i] - dur_of[i],
            ))
    return ReplayResult(
        frames=tuple(frames_out),
        sent_count=sum(1 for i in range(n) if transmittable[i]),
        never_count=sum(1 for i in range(n) if not transmittable[i]),
        deadline_violations=tuple(
            i for i in range(n) if transmittable[i] and frames_out[i].deadline_violated
        ),
    )
