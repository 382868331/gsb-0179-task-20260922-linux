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
    "PreemptionConfig",
    "FrameResult",
    "ReplayResult",
    "replay",
    "replay_preemptive",
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
    PREEMPTED = "preempted"                    # 片段边界处被紧急帧抢占,暂停等待恢复


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
    fragments: tuple = ()         # 抢占模式下每片的 (start_ns, end_ns);旧模式为 ()
    preempted_count: int = 0      # 在片段边界被紧急帧抢占而暂停的次数
    overhead_bytes: int = 0       # 恢复片段引入的额外开销字节总数


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


@dataclass(frozen=True)
class PreemptionConfig:
    """帧抢占配置(express / preemptable 两类队列)。

    - express_queues: 紧急(express)队列集合;其余队列为可抢占(preemptable)。
    - fragment_size_bytes: 可抢占帧的固定片段载荷大小(字节,>= 1);
      普通帧只能在这种固定片段边界暂停。
    - fragment_overhead_bytes: 每次恢复(第 2 片起)额外计入的开销字节数,>= 0。
    """

    express_queues: frozenset
    fragment_size_bytes: int
    fragment_overhead_bytes: int = 0

    def __post_init__(self):
        try:
            qs = frozenset(self.express_queues)
        except TypeError as exc:
            raise TSNGateError(f"PreemptionConfig.express_queues: 不可迭代({exc})") from exc
        for q in qs:
            _require_int(q, "PreemptionConfig.express_queues[]", lo=0, hi=MAX_QUEUES - 1)
        _require_int(self.fragment_size_bytes,
                     "PreemptionConfig.fragment_size_bytes", lo=1)
        _require_int(self.fragment_overhead_bytes,
                     "PreemptionConfig.fragment_overhead_bytes", lo=0)
        object.__setattr__(self, "express_queues", qs)


def replay_preemptive(arrivals, gate_schedule, config, *,
                      max_events=DEFAULT_MAX_EVENTS) -> ReplayResult:
    """带帧抢占的重放:express 帧可在片段边界打断 preemptable 帧。

    语义(在 replay 的公共约定之上):
    - preemptable 帧按 fragment_size_bytes 切成固定片段;只能在片段边界暂停。
      紧急帧到达后等到当前片段结束(下一个合法边界)再发送。
    - 暂停帧保留剩余字节;恢复时(第 2 片起)每片额外计入
      fragment_overhead_bytes 字节开销,占用链路时间。
    - 门关闭不会截断已经开始的片段(片段必然跑到完成),但门关闭后
      不能再开新片段:启动条件为 门开着 且 剩余窗口 > guard_ns。
      紧急帧也必须满足自己的门,否则原地等待,可抢占帧可继续。
    - 多个暂停帧按原开始顺序(首次开始时刻早者优先)恢复;
      暂停帧优先于从未开始的可抢占帧。
    - 同刻处理次序:片段完成 -> 门状态按新相位求值 -> 帧到达并入队 -> 启动决策。
    - 同可发紧急帧竞争按 (到达时刻, 到达序号);新可抢占帧按 (队列号, 到达序号)。
    - 永不可发的证明:该队列最长窗口 <= guard_ns(连一个片段都开不了)。
    """
    _require_int(max_events, "max_events", lo=1)
    if not isinstance(gate_schedule, GateSchedule):
        raise TSNGateError(
            f"gate_schedule: 需要 GateSchedule,得到 {type(gate_schedule).__name__}"
        )
    if not isinstance(config, PreemptionConfig):
        raise TSNGateError(
            f"config: 需要 PreemptionConfig,得到 {type(config).__name__}"
        )
    try:
        arrivals = list(arrivals)
    except TypeError as exc:
        raise TSNGateError(f"arrivals: 不可迭代({exc})") from exc

    rate = gate_schedule.link_rate_bits_per_ns
    guard = gate_schedule.guard_ns
    cycle = gate_schedule.cycle_ns
    offset = gate_schedule.clock_offset_ns
    frag_size = config.fragment_size_bytes
    frag_ov = config.fragment_overhead_bytes

    n = len(arrivals)
    queue_of, arrive_of, size_of, dur_of, deadline_of = [], [], [], [], []
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
        size_of.append(size)
        dur_of.append((size * 8 + rate - 1) // rate)

    qwin = [[] for _ in range(MAX_QUEUES)]
    for w in gate_schedule.windows:
        for q in w.queues:
            qwin[q].append((w.start_ns, w.end_ns))
    for q in range(MAX_QUEUES):
        qwin[q].sort()
    maxwin = [max((e - s for s, e in ws), default=0) for ws in qwin]

    is_exp = [q in config.express_queues for q in queue_of]
    # 可证明的“永不可发”:最长窗口 <= guard,连一个片段/整帧都开不了
    transmittable = [maxwin[queue_of[i]] > guard for i in range(n)]

    reasons: list[list[WaitReason]] = [[] for _ in range(n)]

    def note(i: int, r: WaitReason) -> None:
        if r not in reasons[i]:
            reasons[i].append(r)

    order = sorted(range(n), key=lambda i: (arrive_of[i], i))
    remaining = list(size_of)          # preemptable 帧剩余载荷字节
    frags: list[list] = [[] for _ in range(n)]
    first_start: list[int | None] = [None] * n
    last_end: list[int | None] = [None] * n
    preempted_count = [0] * n
    done = [False] * n
    done_cnt = 0
    total_tx = sum(1 for i in range(n) if transmittable[i])

    exp_ready: list[int] = []          # 已到达未发送的紧急帧
    new_ready: list[int] = []          # 已到达、从未开始的可抢占帧
    paused: list[int] = []             # 已暂停(有剩余字节)的可抢占帧
    pi = 0
    t = 0
    busy_until = 0
    active = -1
    events = 0

    def startable(q: int, phase: int) -> bool:
        return any(s <= phase < e and e - phase > guard for s, e in qwin[q])

    while True:
        events += 1
        if events > max_events:
            raise BudgetExhaustedError(
                f"事件预算 max_events={max_events} 耗尽:重放被截断,"
                f"这不是“永不可发”的证明"
            )
        # 1) 同刻次序第一步:片段/帧完成
        if active >= 0 and t >= busy_until:
            i = active
            active = -1
            if is_exp[i] or remaining[i] == 0:
                done[i] = True
                done_cnt += 1
            elif i not in paused:
                paused.append(i)
        # 2) 同刻次序第三步:帧到达(门状态在决策时按当前相位求值)
        while pi < n and arrive_of[order[pi]] <= t:
            i = order[pi]
            pi += 1
            if transmittable[i]:
                (exp_ready if is_exp[i] else new_ready).append(i)
        if done_cnt >= total_tx:
            break
        if t < busy_until:
            t = busy_until
            continue
        phase = (t + offset) % cycle

        # 3) 启动决策:紧急帧 > 暂停帧(按原开始顺序) > 新可抢占帧
        chosen = -1
        cands = [i for i in exp_ready if startable(queue_of[i], phase)]
        if cands:
            chosen = min(cands, key=lambda i: (arrive_of[i], i))
            for i in cands:
                if i != chosen:
                    note(i, WaitReason.OUTRANKED)
            for i in paused + new_ready:
                if startable(queue_of[i], phase):
                    note(i, WaitReason.PREEMPTED)
                    preempted_count[i] += 1
            exp_ready.remove(chosen)
        else:
            ps = [i for i in paused if startable(queue_of[i], phase)]
            if ps:
                chosen = min(ps, key=lambda i: first_start[i])
                for i in ps:
                    if i != chosen:
                        note(i, WaitReason.OUTRANKED)
                for i in new_ready:
                    if startable(queue_of[i], phase):
                        note(i, WaitReason.OUTRANKED)
                paused.remove(chosen)
            else:
                ns_ = [i for i in new_ready if startable(queue_of[i], phase)]
                if ns_:
                    chosen = min(ns_, key=lambda i: (queue_of[i], i))
                    for i in ns_:
                        if i != chosen:
                            note(i, WaitReason.OUTRANKED)
                    new_ready.remove(chosen)

        if chosen >= 0:
            i = chosen
            if is_exp[i]:
                unit = size_of[i]
            else:
                chunk = min(frag_size, remaining[i])
                unit = chunk + (frag_ov if frags[i] else 0)
                remaining[i] -= chunk
            ud = (unit * 8 + rate - 1) // rate
            frags[i].append((t, t + ud))
            if first_start[i] is None:
                first_start[i] = t
            last_end[i] = t + ud
            active = i
            busy_until = t + ud
            for j in exp_ready + paused + new_ready:
                note(j, WaitReason.LINK_BUSY)
                qj = queue_of[j]
                if not startable(qj, phase):
                    open_now = any(s <= phase < e for s, e in qwin[qj])
                    note(j, WaitReason.INSUFFICIENT_WINDOW
                         if open_now else WaitReason.GATE_CLOSED)
            t = busy_until
            continue

        # 4) 无可启动:记录原因,推进到下一个事件点(到达或窗口起点)
        pending = exp_ready + paused + new_ready
        for i in pending:
            q = queue_of[i]
            open_now = any(s <= phase < e for s, e in qwin[q])
            note(i, WaitReason.INSUFFICIENT_WINDOW if open_now else WaitReason.GATE_CLOSED)
        nxt = arrive_of[order[pi]] if pi < n else None
        for i in pending:
            for s, _e in qwin[queue_of[i]]:
                d = (s - phase) % cycle
                cand_t = t + (d if d > 0 else cycle)
                if nxt is None or cand_t < nxt:
                    nxt = cand_t
        if nxt is None:
            break  # 只剩永不可发帧
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
            violated = deadline_of[i] is not None and last_end[i] > deadline_of[i]
            frames_out.append(FrameResult(
                seq=i, queue=queue_of[i], status=FrameStatus.SENT,
                arrive_ns=arrive_of[i], duration_ns=dur_of[i],
                start_ns=first_start[i], end_ns=last_end[i],
                waited_ns=first_start[i] - arrive_of[i],
                wait_reasons=tuple(reasons[i]),
                deadline_ns=deadline_of[i], deadline_violated=violated,
                fragments=tuple(frags[i]),
                preempted_count=preempted_count[i],
                overhead_bytes=(0 if is_exp[i]
                                else max(0, len(frags[i]) - 1) * frag_ov),
            ))
    return ReplayResult(
        frames=tuple(frames_out),
        sent_count=sum(1 for i in range(n) if transmittable[i]),
        never_count=sum(1 for i in range(n) if not transmittable[i]),
        deadline_violations=tuple(
            i for i in range(n) if transmittable[i] and frames_out[i].deadline_violated
        ),
    )
