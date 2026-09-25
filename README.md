# TSN 门控发送验证(tsn_gate)

离线、确定性的时间敏感网络(TSN)门控发送重放模块。给定帧到达序列与周期门控表,
计算每帧的发送开始/结束时刻、等待原因与 deadline 违例;任何窗口都容纳不下的帧
被证明并标记为“永不可发”,不会死循环。支持一次**预定门表切换**:新表携带
生效参考时刻,到达该时刻后未开始的帧按新表判断。

## 环境

- Python 3.14.7(仅标准库,无需安装任何第三方依赖,Linux 原生运行)
- 核心、测试、演示全部离线;示例数据由仓库内确定性代码生成

## 接口

```python
from tsn_gate import Frame, GateWindow, GateSchedule, ConfigChange, replay

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
arrivals = [
    Frame(queue=1, arrive_ns=350, size_bytes=40, deadline_ns=None),
    Frame(queue=1, arrive_ns=350, size_bytes=40, deadline_ns=1600),
]
result = replay(arrivals, schedule, max_events=1_000_000)

# 预定门表切换(可选):新表在 effective_ns 生效,请求在 accepted_ns 被接受
new_schedule = GateSchedule(
    cycle_ns=1000,
    windows=(GateWindow(0, 200, frozenset({0})),
             GateWindow(500, 1000, frozenset({1}))),
    link_rate_bits_per_ns=1,            # 必须与旧表一致(物理属性)
    guard_ns=50,                        # 可以不同(属于门控配置)
    clock_offset_ns=0,                  # 必须与旧表一致(物理属性)
)
change = ConfigChange(schedule=new_schedule, effective_ns=2000, accepted_ns=1200)
result = replay(arrivals, schedule, config_change=change)
```

`replay(arrivals, gate_schedule, *, config_change=None, max_events=DEFAULT_MAX_EVENTS)`
是纯函数:不修改入参,要么返回完整 `ReplayResult`,要么抛异常,不存在未说明的部分结果。
`config_change=None` 时即单表重放,行为与引入切换前完全一致。

### 输出

`ReplayResult.frames`(按到达序号排序)中每个 `FrameResult`:

| 字段 | 含义 |
| --- | --- |
| `seq` / `queue` / `arrive_ns` | 到达序号(输入下标)/ 队列 / 到达时刻(参考时钟) |
| `duration_ns` | 序列化时长 = `ceil(size_bytes*8 / link_rate_bits_per_ns)` |
| `status` | `SENT` 或 `NEVER_TRANSMITTABLE` |
| `start_ns` / `end_ns` | 发送开始/结束(参考时钟;永不可发为 `None`) |
| `waited_ns` | `start_ns - arrive_ns` |
| `wait_reasons` | 等待期间出现过的原因:`gate-closed`、`insufficient-window`、`link-busy`、`outranked` |
| `deadline_ns` / `deadline_violated` | 截止时间与是否违例(`end_ns > deadline_ns`;帧仍发送,不丢弃) |

`ReplayResult` 另有 `sent_count`、`never_count`、`deadline_violations`(违例帧 seq 元组)。

## 语义约定

- 时间均为整数纳秒;输入与输出时间戳都是**参考时钟**;门表作用在本地时钟相位上,
  本地时钟 = 参考时钟 + `clock_offset_ns`。
- 门表窗口为半开区间 `[start_ns, end_ns)`,到 `end_ns` 即关;同一队列的窗口不得重叠。
- 帧不可抢占:仅当 **序列化时长 + guard** 完整落入当前开门窗口剩余部分时才可开始;
  结束+guard 恰好顶到窗口终点是允许的。
- 多个可发帧竞争时按 **(队列号升序, 到达序号升序)** 选择;到达时间相同者按输入下标。
- 任何窗口都容纳不下 时长+guard 的帧,在重放前即被**证明**并标记 `NEVER_TRANSMITTABLE`。
- `max_events` 是事件预算安全阀:耗尽时抛 `BudgetExhaustedError`,
  与“已证明永不可发”是两种不同结论,互不混淆。

## 预定配置切换(新增契约)

`ConfigChange(schedule, effective_ns, accepted_ns=0)` 表示一次预定的门表切换:

- **接受时整体校验**:请求被接受时先校验整张新表与生效时刻,任一不合法即抛
  `TSNGateError`,不留任何部分变更。校验规则:
  - `effective_ns` 必须落在**旧周期边界**:
    `(effective_ns + 旧表.clock_offset_ns) % 旧表.cycle_ns == 0`
    (边界按本地时钟相位判定,既有偏差统一参与换算);
  - `accepted_ns <= effective_ns`,两者均为非负整数纳秒;
  - 新表的 `clock_offset_ns`、`link_rate_bits_per_ns` 必须与旧表一致
    (时钟偏差与链路速率是物理属性,不随门表配置切换);
  - 新表自身须通过 `GateSchedule` 的全部既有校验。
- **切换语义**:
  - 在生效时刻尚未开始的帧一律按**新门表**判断(生效时刻到达的帧也算),
    排队选择规则 (队列号, 到达序号) 不变;
  - 已经开始的不可抢占帧不受切换影响,**继续发送到完成**;
  - `accepted_ns` 之后,不允许开始任何 `end+guard` 越过 `effective_ns` 的旧表发送
    ——不借旧表预约未来时段。注:由于生效时刻对齐旧周期边界且窗口不跨周期,
    旧表发送的 `end+guard` 数学上必然不超过生效时刻,该约束是显式安全网;
    测试中的逐纳秒参考扫描对每条输出验证这一性质;
  - 新表周期不必与旧表相同;新表相位同样按本地时钟
    `(t + clock_offset_ns) % 新表.cycle_ns` 计算。
- **“永不可发”重新计算,不直接丢帧**:
  - 旧表下永不可发的帧,若新表容得下,则等到生效后发送(不被丢弃);
  - 仅旧表容得下的帧,若到达时已没有任何可行的旧表开始时刻,直接证明为
    `NEVER_TRANSMITTABLE`;若因争用错过最后一个可行旧表开始时刻,则在重放中
    错过该时刻时被证明为 `NEVER_TRANSMITTABLE`(等待原因仍被记录)。

## 错误语义

- 所有非法输入抛 `TSNGateError`(继承 `ValueError`),消息含可定位字段路径,
  如 `arrivals[2].size_bytes: -1 小于下限 1`;不静默纠正。
- 整数参数拒绝 `bool`;数值输入拒绝 `NaN`/`Infinity`;时间参数拒绝一切非 `int`
  (包括 `3.0` 这样的整值浮点)。
- `BudgetExhaustedError` 是 `TSNGateError` 的子类,可单独捕获。

## 输入上限

- 队列:最多 4 个,编号 `0..3`(`MAX_QUEUES`)。
- `cycle_ns >= 1`,`link_rate_bits_per_ns >= 1`,`guard_ns >= 0`,`size_bytes >= 1`,
  `arrive_ns >= 0`,`deadline_ns >= 0`;`clock_offset_ns` 可为任意整数。
- 切换:`effective_ns >= 0`,`0 <= accepted_ns <= effective_ns`。
- 窗口数量与到达帧数无硬上限;事件预算 `max_events` 默认 1 000 000
  (正常输入下每帧只产生常数个事件,预算仅是防错安全阀)。

## 设计取舍

- **事件驱动**而非逐 tick:可发性只在到达、窗口边沿、链路空闲、切换生效时刻变化,
  跳转这些事件点即可,结果与逐 ns 仿真一致(测试中用独立逐 tick 参考交叉验证)。
- **永不可发预先证明**:单表时 `时长+guard > 该队列最长窗口` 是充要条件(窗口周期
  重复);切换时按两张表重算——容得下新表即可等生效后发送,否则须存在不早于
  到达时刻的可行旧表开始(最晚可行开始时刻用模运算直接求出,参考实现用逐纳秒
  回扫独立复算)。从机制上排除死循环。
- **全整数运算**:唯一除法是序列化时长的向上取整,无浮点参与。

## 误差口径

本模块不使用浮点:所有时间为整数纳秒,序列化时长为精确整数上取整
`ceil(size_bytes*8/rate)`,输出不存在浮点误差,测试按精确相等断言。

## 已知限制

- 只实现本题契约的有限子集,**不宣称兼容完整 IEEE 802.1Qbv**:无帧抢占、
  无逐队列 guard、无多跳/多链路、无时钟漂移(仅固定偏差)、无控制面。
- 切换至多一次(不支持切换链/多次更新);切换不改变链路速率与时钟偏差;
  生效时刻必须对齐旧周期边界,未对齐的请求被拒绝而非自动取整。
- guard 为全局单值(新表可带不同 guard);deadline 违例只报告不阻止发送。
- 等待原因为定性标签(出现过的原因集合),不输出逐段等待时间线。

## 运行

```bash
python -m unittest discover -s tests -v   # 单表 + 切换测试,含与独立逐 tick 参考的随机对照
python demo.py                            # 演示:错过窗口、预定切换、非法输入拒绝
```

两条命令均在数秒内完成,全程离线。测试中的 `tests/reference.py` 是独立的逐纳秒
参考实现(不导入 tsn_gate):`reference_replay_switch` 逐 tick 重述切换语义,
`verify_timeline` 对核心输出做逐纳秒可行性扫描,验证没有重叠发送、没有凭空
提前开始、没有借旧表预约切换后的时段。
