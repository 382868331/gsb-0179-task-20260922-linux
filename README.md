# TSN 门控发送验证(tsn_gate)

离线、确定性的时间敏感网络(TSN)门控发送重放模块。给定帧到达序列与周期门控表,
计算每帧的发送开始/结束时刻、等待原因与 deadline 违例;任何窗口都容纳不下的帧
被证明并标记为“永不可发”,不会死循环。支持在重放中携带**一次预定门表切换**:
新门表携带生效参考时刻,且必须落在旧表周期边界。

## 环境

- Python 3.14.7(仅标准库,无需安装任何第三方依赖,Linux 原生运行)
- 核心、测试、演示全部离线;示例数据由仓库内确定性代码生成;不接硬件/在线服务,不用 Docker

## 接口

```python
from tsn_gate import Frame, GateWindow, GateSchedule, ScheduledUpdate, replay

schedule = GateSchedule(
    cycle_ns=1000,                      # 门表周期,整数 ns,>= 1
    windows=(                           # 周期内半开区间 [start, end),循环重复
        GateWindow(0, 400, frozenset({0})),
        GateWindow(400, 800, frozenset({1})),
    ),
    link_rate_bits_per_ns=1,            # 整数链路速率,bit/ns(1 = 1 Gbps),>= 1
    guard_ns=50,                        # 全局 guard 带,>= 0
    clock_offset_ns=0,                  # 本地时钟相对参考时钟的固定偏差(可负)
)

# 可选:一次预定切换。new_schedule 在 effective_ns(参考时钟)起生效,
# effective_ns 必须满足 (effective_ns + old.clock_offset_ns) % old.cycle_ns == 0。
update = ScheduledUpdate(new_schedule=schedule2, effective_ns=5000)

arrivals = [
    Frame(queue=1, arrive_ns=350, size_bytes=40, deadline_ns=None),
    Frame(queue=1, arrive_ns=350, size_bytes=40, deadline_ns=1600),
]
result = replay(arrivals, schedule, scheduled_update=update,
                max_events=1_000_000)
```

`replay(arrivals, gate_schedule, *, scheduled_update=None, max_events=...)` 是纯函数:
不修改入参,要么返回完整 `ReplayResult`,要么抛异常,不存在未说明的部分结果。
不带 `scheduled_update` 时行为与旧版完全一致。

### 输出

`ReplayResult.frames`(按到达序号排序)中每个 `FrameResult`:

| 字段 | 含义 |
| --- | --- |
| `seq` / `queue` / `arrive_ns` | 到达序号(输入下标)/ 队列 / 到达时刻(参考时钟) |
| `duration_ns` | 序列化时长 = `ceil(size_bytes*8 / link_rate_bits_per_ns)`;已发送帧按实际生效的表计算 |
| `status` | `SENT` 或 `NEVER_TRANSMITTABLE` |
| `start_ns` / `end_ns` | 发送开始/结束(参考时钟;永不可发为 `None`) |
| `waited_ns` | `start_ns - arrive_ns` |
| `wait_reasons` | 等待期间出现过的原因:`gate-closed`、`insufficient-window`、`link-busy`、`outranked` |
| `deadline_ns` / `deadline_violated` | 截止时间与是否违例(`end_ns > deadline_ns`;帧仍发送,不丢弃) |
| `config_epoch` | 发送时生效的配置:`0`=旧表,`1`=新表;永不可发为 `None` |

`ReplayResult` 另有 `sent_count`、`never_count`、`deadline_violations`(违例帧 seq 元组)
与 `switch_effective_ns`(实际携带的生效时刻;无切换为 `None`)。

## 语义约定

- 时间均为整数纳秒;输入与输出时间戳都是**参考时钟**;门表作用在本地时钟相位上,
  本地时钟 = 参考时钟 + `clock_offset_ns`。偏差是输入,两张表各自的偏差都参与换算。
- 门表窗口为半开区间 `[start_ns, end_ns)`,到 `end_ns` 即关;同一队列的窗口不得重叠。
- 帧不可抢占:仅当 **序列化时长 + guard** 完整落入当前开门窗口剩余部分时才可开始;
  结束+guard 恰好顶到窗口终点是允许的。
- 多个可发帧竞争时按 **(队列号升序, 到达序号升序)** 选择;到达时间相同者按输入下标。
- 任何窗口都容纳不下 时长+guard 的帧,在重放前即被**证明**并标记 `NEVER_TRANSMITTABLE`。
- `max_events` 是事件预算安全阀:耗尽时抛 `BudgetExhaustedError`,
  与“已证明永不可发”是两种不同结论,互不混淆。

### 预定门表切换契约

- **一次重放最多一次切换**。新表可以有不同的周期、窗口、速率、guard 与时钟偏差。
- **生效时刻必须落在旧表本地时钟的周期边界**:
  `(effective_ns + old.clock_offset_ns) % old.cycle_ns == 0`,否则请求被拒绝。
  边界选择使用 `[start,end)` 半开约定,即 `t < effective_ns` 用旧表,
  `t >= effective_ns` 用新表;**恰在边界到达的帧按新表判断**。
- **接受请求前先校验整张新表与时间**:新表构造时完成窗口超周期/重叠等全部校验,
  `replay` 再校验边界;任一非法整体抛错,不产生部分生效。
- **在哪个表下开始,就用哪个表的规则**(窗口、guard、速率);帧不会被中途重新评估。
- **已开始的不可抢占帧继续发送到完成**:事件驱动在链路忙区间内不做任何门控决策。
  由于生效时刻对齐旧周期边界,旧表最后一个周期实例的窗口最迟恰在 `effective_ns`
  关闭,合法输入下不会有发送(含 guard)越过切换点;防御性地,旧表适配还要求
  `start + 时长 + guard <= effective_ns`。
- **切换前未开始的帧一律按新表重新判断,排队先后不变**:旧表 alone 会推导出的
  “永不可发”结论**不沿用**——新表有窗口能容纳,就照常发送,不丢帧。
- **不能借旧表预约切换点之后的时段**:事件推进不考虑旧表在 `effective_ns` 之后的
  窗口实例;几何预判定只检查旧表有限前缀 `[0, effective_ns)`。
- **动态证明**:某帧在旧表有限前缀里几何上存在机会(因此先排队等待、不是被预先丢弃),
  但因竞争在切换前未开始,而新表确实任何窗口都容不下它,则在切换发生后才定论为
  `NEVER_TRANSMITTABLE`,并保留它实际经历过的等待原因。
- 两张表几何上都永不可发的帧,在重放前即被证明;新表可发的帧永远不会被判永不可发。

## 错误语义

- 所有非法输入抛 `TSNGateError`(继承 `ValueError`),消息含可定位字段路径,
  如 `arrivals[2].size_bytes: -1 小于下限 1`;不静默纠正。
- 整数参数拒绝 `bool`;数值输入拒绝 `NaN`/`Infinity`;时间参数拒绝一切非 `int`
  (包括 `3.0` 这样的整值浮点)。
- `ScheduledUpdate` 类型错误、`effective_ns < 0`、非边界时刻均被可定位地拒绝。
- `BudgetExhaustedError` 是 `TSNGateError` 的子类,可单独捕获。

## 输入上限

- 队列:最多 4 个,编号 `0..3`(`MAX_QUEUES`)。
- `cycle_ns >= 1`,`link_rate_bits_per_ns >= 1`,`guard_ns >= 0`,`size_bytes >= 1`,
  `arrive_ns >= 0`,`deadline_ns >= 0`,`effective_ns >= 0`;
  `clock_offset_ns` 可为任意整数。
- 窗口数量与到达帧数无硬上限;事件预算 `max_events` 默认 1 000 000
  (正常输入下每帧只产生常数个事件,预算仅是防错安全阀)。
- 一次重放至多携带一个 `ScheduledUpdate`(本题契约;无多切换链)。

## 设计取舍

- **事件驱动**而非逐 tick:可发性只在到达、窗口边沿、链路空闲、切换点变化,
  跳转这些事件点即可,结果与逐 ns 仿真一致(测试中用独立逐 tick 参考交叉验证)。
- **永不可发预先证明**:`时长+guard > 该队列最长窗口` 是充要条件(窗口周期重复)。
  有切换时拆成两部分:新表无限重复的最长窗口判定 + 旧表有限前缀 `[0, eff)` 的
  闭式整数区间判定(对周期实例号取上下界,不逐周期枚举);两者皆无才是几何不可发。
- **切换点强制为事件点**:旧表时段推进时一定考虑 `effective_ns`,避免跨过边界决策。
- **全整数运算**:唯一除法是序列化时长的向上取整,无浮点参与。

## 误差口径

本模块不使用浮点:所有时间为整数纳秒,序列化时长为精确整数上取整
`ceil(size_bytes*8/rate)`,输出不存在浮点误差,测试按精确相等断言。

## 已知限制

- 只实现本题契约的有限子集,**不宣称兼容完整 IEEE 802.1Qbv**:无帧抢占、
  无逐队列 guard、无多跳/多链路、无时钟漂移(仅固定偏差)、无控制面。
- 只支持**一次**预定切换;切换原子完成,没有切换过程中的过渡表。
- guard 为全局单值;deadline 违例只报告不阻止发送。
- 等待原因为定性标签(出现过的原因集合),不输出逐段等待时间线。
- 速率/guard 在两张表之间可以不同,因此同一帧切换前后的 `duration_ns` 理论值
  可能不同;输出中 `duration_ns` 取帧实际发送时生效表的数值。

## 运行

```bash
./.venv/bin/python -m unittest discover -s tests -v   # 单元测试 + 独立逐 tick 参考对照
./.venv/bin/python demo.py                            # 错过窗口 + 预定切换 + 非法拒绝
```

测试包含:定点契约用例(恰在切换边界到达、旧可发/新不可发、旧不可发/新可发、
在途帧、guard 跨切换、排队顺序、两侧偏差、速率变化、动态永不可发),
300 组带切换 + 120 组无切换的确定性随机用例与独立逐纳秒参考逐一精确对照,
以及独立后置审计(发送区间两两不重叠、不早于到达开始、起始时刻门控几何成立、
旧表发送含 guard 不越过切换点、永不可发结论与窗口几何一致)。
演示在 1 秒内完成,全程离线。
