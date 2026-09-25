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

预定门表切换(ScheduledUpdate)
-----------------------------
- 一次重放最多携带一次预定切换:新门表 + 生效参考时刻 effective_ns。
- effective_ns 必须落在**旧表本地时钟的周期边界**上:
  (effective_ns + old.clock_offset_ns) % old.cycle_ns == 0。
- 请求被接受(进入重放)前,整张新表(GateSchedule 自身的全部校验)与生效时刻
  一起校验;任一非法则整体抛错,不存在部分生效。
- t < effective_ns 用旧表判断;t >= effective_ns 用新表判断(恰在边界到达的帧
  按新表)。帧在哪个表下开始发送,就用哪个表的 rate/guard 计算时长与适配。
- 已经开始发送的不可抢占帧继续发送到完成;事件驱动在链路忙区间内不做任何门控
  决策。又因生效时刻对齐旧周期边界,旧表最后一个周期实例的窗口最迟恰在
  effective_ns 关闭,合法输入下不会有发送跨越切换点。
- 生效时刻尚未开始的帧一律按新表重新判断,排队先后((队列, 到达序号))不变;
  旧表 alone 会推导出的“永不可发”结论不沿用:新表有窗口则照常发送。
- 旧表只允许安排“在切换前连同 guard 一起完成”的发送:旧窗口在 t 处适配还要求
  t + 时长 + guard <= effective_ns(防御性条件;对齐边界时它与窗口适配等价),
  不能借旧表预约 effective_ns 之后的时段——事件推进也不考虑旧表在切换点之后
  的窗口实例。
- 仅在旧表有限前缀里几何可发、但因竞争未及在切换前开始、且新表确实永不可发的
  帧,在切换发生后才被**证明**为永不可发(动态结论,非沿用旧表,也不是丢帧)。

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
    "ScheduledUpdate",
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


def _ceil_div(a: int, b: int) -> int:
    """整数上取整除法,b > 0,允许 a 为负。"""
    return -((-a) // b)


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
class ScheduledUpdate:
    """一次预定门表切换:new_schedule 自 effective_ns(参考时钟)起生效。

    生效时刻是否落在旧表周期边界,需要结合旧表校验,因此发生在 replay() 中;
    此处只做与旧表无关的类型与取值校验。
    """

    new_schedule: GateSchedule
    effective_ns: int

    def __post_init__(self):
        if not isinstance(self.new_schedule, GateSchedule):
            raise TSNGateError(
                "ScheduledUpdate.new_schedule: 需要 GateSchedule,"
                f"得到 {type(self.new_schedule).__name__}"
            )
        _require_int(self.effective_ns, "ScheduledUpdate.effective_ns", lo=0)


@dataclass(frozen=True)
class FrameResult:
    seq: int                      # 到达序号(arrivals 下标)
    queue: int
    status: FrameStatus
    arrive_ns: int
    duration_ns: int              # 序列化时长(不含 guard);已发送按实际生效的表
    start_ns: int | None          # 参考时钟;永不可发时为 None
    end_ns: int | None
    waited_ns: int | None         # start_ns - arrive_ns
    wait_reasons: tuple           # 等待期间出现过的原因(WaitReason,按首次出现排序)
    deadline_ns: int | None
    deadline_violated: bool       # end_ns > deadline_ns(帧仍发送,不丢弃)
    config_epoch: int | None      # 发送时生效的表:0=旧表,1=新表;永不可发为 None


@dataclass(frozen=True)
class ReplayResult:
    frames: tuple                 # FrameResult,按 seq 升序
    sent_count: int
    never_count: int
    deadline_violations: tuple    # 违例帧的 seq 元组
    switch_effective_ns: int | None  # 实际携带的切换生效时刻;无切换为 None


@dataclass(frozen=True)
class _Regime:
    """重放内部:某张表在一个时间段内生效的全部判定参数。"""

    cycle: int
    offset: int
    guard: int
    rate: int
    qwin: list                    # [q] -> sorted[(start_phase, end_phase)]
    maxwin: list                  # [q] -> 最长窗口


def _make_regime(schedule: GateSchedule) -> _Regime:
    qwin = [[] for _ in range(MAX_QUEUES)]
    for w in schedule.windows:
        for q in w.queues:
            qwin[q].append((w.start_ns, w.end_ns))
    for q in range(MAX_QUEUES):
        qwin[q].sort()
    maxwin = [max((e - s for s, e in ws), default=0) for ws in qwin]
    return _Regime(
        cycle=schedule.cycle_ns,
        offset=schedule.clock_offset_ns,
        guard=schedule.guard_ns,
        rate=schedule.link_rate_bits_per_ns,
        qwin=qwin,
        maxwin=maxwin,
    )


def _old_opportunity_exists(q: int, arrive_ns: int, need_ns: int, eff_ns: int,
                            old: _Regime) -> bool:
    """旧表有限前缀 [0, eff_ns) 内,是否存在几何上的合法开始时刻。

    存在整数 t 与窗口相位 (s,e)、周期实例 k 使得:
        t >= max(arrive_ns, 0), t + need_ns <= eff_ns(含 guard 在切换前完成),
        k*C + s - off <= t <= k*C + e - off - need_ns。
    对 k 取整区间 [klo, khi](闭式上下界),不空即存在;O(窗口数),不逐周期枚举。
    """
    a = max(arrive_ns, 0)
    if a + need_ns > eff_ns:
        return False
    c, off = old.cycle, old.offset
    for s, e in old.qwin[q]:
        if e - s < need_ns:
            continue
        klo = _ceil_div(a + need_ns - e + off, c)   # t <= 实例末端 - need
        khi = (eff_ns - need_ns - s + off) // c     # 实例起点 <= eff - need
        if klo <= khi:
            return True
    return False


def replay(arrivals, gate_schedule, *, scheduled_update=None,
           max_events=DEFAULT_MAX_EVENTS) -> ReplayResult:
    """重放到达序列在周期门控表(可含一次预定切换)下的发送过程。

    纯函数:不修改入参,失败时抛出 TSNGateError(或其子类),不留下任何部分结果。
    """
    _require_int(max_events, "max_events", lo=1)
    if not isinstance(gate_schedule, GateSchedule):
        raise TSNGateError(
            f"gate_schedule: 需要 GateSchedule,得到 {type(gate_schedule).__name__}"
        )
    if scheduled_update is not None and not isinstance(scheduled_update, ScheduledUpdate):
        raise TSNGateError(
            "scheduled_update: 需要 ScheduledUpdate 或 None,"
            f"得到 {type(scheduled_update).__name__}"
        )
    try:
        arrivals = list(arrivals)
    except TypeError as exc:
        raise TSNGateError(f"arrivals: 不可迭代({exc})") from exc

    old = _make_regime(gate_schedule)
    eff = None
    new = None
    if scheduled_update is not None:
        eff = scheduled_update.effective_ns
        # 生效时刻必须落在旧表本地时钟的周期边界(偏差参与换算)
        if (eff + old.offset) % old.cycle != 0:
            raise TSNGateError(
                f"scheduled_update.effective_ns: {eff} 未落在旧表本地周期边界: "
                f"(effective_ns + clock_offset_ns={old.offset}) "
                f"% cycle_ns={old.cycle} = "
                f"{(eff + old.offset) % old.cycle},应为 0"
            )
        new = _make_regime(scheduled_update.new_schedule)

    n = len(arrivals)
    queue_of, arrive_of, deadline_of = [], [], []
    dur_old, dur_new, need_old, need_new = [], [], [], []
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
        dur_old.append((size * 8 + old.rate - 1) // old.rate)  # ceil,精确整数
        need_old.append(dur_old[-1] + old.guard)
        if new is not None:
            dn = (size * 8 + new.rate - 1) // new.rate
            dur_new.append(dn)
            need_new.append(dn + new.guard)
        else:
            dur_new.append(dur_old[-1])
            need_new.append(need_old[-1])

    # “永不可发”的几何预判定(重放前证明,不参与等待):
    # 无切换:旧表(也是唯一表)最长窗口容不下即永不可发。
    # 有切换:旧表有限前缀 [0,eff) 存在机会,或新表无限重复中存在机会,二者皆无才是。
    # 旧表 alone 的“永不可发”结论不直接沿用;仅旧表可发的帧仍先进入等待。
    transmittable = [False] * n
    for i in range(n):
        q = queue_of[i]
        if new is None:
            transmittable[i] = need_old[i] <= old.maxwin[q]
        else:
            new_ok = need_new[i] <= new.maxwin[q]
            old_ok = _old_opportunity_exists(q, arrive_of[i], need_old[i], eff, old)
            transmittable[i] = old_ok or new_ok
    new_fittable = [need_new[i] <= new.maxwin[queue_of[i]] if new is not None
                    else True for i in range(n)]

    reasons: list[list[WaitReason]] = [[] for _ in range(n)]

    def note(i: int, r: WaitReason) -> None:
        if r not in reasons[i]:
            reasons[i].append(r)

    order = sorted(range(n), key=lambda i: (arrive_of[i], i))
    start_of: list[int | None] = [None] * n
    end_of: list[int | None] = [None] * n
    epoch_of: list[int | None] = [None] * n
    dynamic_never: list[bool] = [False] * n

    ready: list[int] = []          # 已到达、几何可发、尚未定论的帧
    pi = 0                         # order 中下一个未到达帧的指针
    t = 0
    busy_until = 0
    events = 0

    def fits_now(i: int, regime: _Regime, need: list[int]) -> bool:
        """帧 i 在时刻 t(链路已空闲)能否在当前生效表下开始。"""
        phase = (t + regime.offset) % regime.cycle
        ok = any(s <= phase < e and e - phase >= need[i]
                 for s, e in regime.qwin[queue_of[i]])
        if ok and eff is not None and t < eff:
            # 旧配置发送必须连同 guard 在切换前完成,不能借旧表预约切换后时段
            ok = t + need[i] <= eff
        return ok

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
        # 已进入新表时段:仅旧前缀几何可发的帧若仍未开始,此刻被证明新表永不可发
        # (动态结论:它确实等过旧表,只是竞争中没赶上,不是被预先丢弃)
        if new is not None and t >= eff:
            still = []
            for i in ready:
                if new_fittable[i]:
                    still.append(i)
                else:
                    dynamic_never[i] = True
            ready = still
        if not ready:
            if pi >= n:
                break
            t = max(t, busy_until, arrive_of[order[pi]])
            continue
        if t < busy_until:
            t = busy_until
            continue

        use_new = new is not None and t >= eff
        regime = new if use_new else old
        need = need_new if use_new else need_old
        phase = (t + regime.offset) % regime.cycle
        ready.sort(key=lambda i: (queue_of[i], i))
        chosen = None
        outranked = []
        for i in ready:
            if fits_now(i, regime, need):
                if chosen is None:
                    chosen = i
                else:
                    outranked.append(i)
        if chosen is not None:
            for i in outranked:
                note(i, WaitReason.OUTRANKED)
            dur = dur_new[chosen] if use_new else dur_old[chosen]
            start_of[chosen] = t
            end = t + dur
            end_of[chosen] = end
            epoch_of[chosen] = 1 if use_new else 0
            busy_until = end
            ready.remove(chosen)
            for i in ready:
                note(i, WaitReason.LINK_BUSY)
            t = end
            continue

        # 没有帧能在当前时刻开始:记录原因,推进到下一个事件点
        for i in ready:
            q = queue_of[i]
            open_now = any(s <= phase < e for s, e in regime.qwin[q])
            note(i, WaitReason.INSUFFICIENT_WINDOW if open_now else WaitReason.GATE_CLOSED)
        nxt = arrive_of[order[pi]] if pi < n else None
        # 旧表时段:切换点本身必为事件点(新表可能恰在此时开门),
        # 且只考虑严格早于切换点的旧窗口实例——不预约切换后的旧表未来实例。
        if eff is not None and t < eff:
            nxt = eff if nxt is None else min(nxt, eff)
        for i in ready:
            for s, e in regime.qwin[queue_of[i]]:
                for cand in (s, e):
                    d = (cand - phase) % regime.cycle
                    if d == 0:
                        d = regime.cycle
                    cand_t = t + d
                    if eff is not None and t < eff and cand_t >= eff:
                        continue  # 切换点之后由新表判断,旧表不得预约
                    if nxt is None or cand_t < nxt:
                        nxt = cand_t
        if nxt is None or nxt <= t:
            raise TSNGateError("内部错误:事件时间未能推进(不应发生)")
        t = nxt

    frames_out = []
    for i in range(n):
        never = not transmittable[i] or dynamic_never[i]
        if never:
            frames_out.append(FrameResult(
                seq=i, queue=queue_of[i], status=FrameStatus.NEVER_TRANSMITTABLE,
                arrive_ns=arrive_of[i],
                duration_ns=dur_new[i] if new is not None else dur_old[i],
                start_ns=None, end_ns=None, waited_ns=None,
                wait_reasons=tuple(reasons[i]) if dynamic_never[i] else (),
                deadline_ns=deadline_of[i], deadline_violated=False,
                config_epoch=None,
            ))
        else:
            violated = deadline_of[i] is not None and end_of[i] > deadline_of[i]
            frames_out.append(FrameResult(
                seq=i, queue=queue_of[i], status=FrameStatus.SENT,
                arrive_ns=arrive_of[i],
                duration_ns=dur_new[i] if epoch_of[i] == 1 else dur_old[i],
                start_ns=start_of[i], end_ns=end_of[i],
                waited_ns=start_of[i] - arrive_of[i],
                wait_reasons=tuple(reasons[i]),
                deadline_ns=deadline_of[i], deadline_violated=violated,
                config_epoch=epoch_of[i],
            ))
    return ReplayResult(
        frames=tuple(frames_out),
        sent_count=sum(1 for i in range(n)
                       if transmittable[i] and not dynamic_never[i]),
        never_count=sum(1 for i in range(n)
                        if not transmittable[i] or dynamic_never[i]),
        deadline_violations=tuple(
            i for i in range(n)
            if transmittable[i] and not dynamic_never[i]
            and frames_out[i].deadline_violated
        ),
        switch_effective_ns=eff,
    )
