# TSN 门控发送验证(tsn_gate)

离线、确定性的时间敏感网络(TSN)门控发送重放模块。给定帧到达序列与周期门控表,
计算每帧的发送开始/结束时刻、等待原因与 deadline 违例;任何窗口都容纳不下的帧
被证明并标记为“永不可发”,不会死循环。
本次迭代在门控之外新增**逐队列信用整形**(CreditShaper):门打开且信用非负才可
开始一帧,用信用斜率控制发送节奏;不配置整形的队列行为与旧版本完全一致。

## 环境

- Python 3.14.7(仅标准库,无需安装任何第三方依赖,Linux 原生运行)
- 核心、测试、演示全部离线;示例数据由仓库内确定性代码生成

### 准备 venv(可选)

仓库不强制虚拟环境,系统 Python 3.14.7 即可直接运行。若希望隔离环境:

```bash
python3.14 -m venv .venv
.venv/bin/python -m unittest discover -s tests -v
```

## 接口

```python
from tsn_gate import Frame, GateWindow, GateSchedule, replay

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
```

`replay(arrivals, gate_schedule, *, max_events=DEFAULT_MAX_EVENTS)` 是纯函数:
不修改入参,要么返回完整 `ReplayResult`,要么抛异常,不存在未说明的部分结果。

### 信用整形(新接口)

```python
from tsn_gate import CreditShaper

schedule = GateSchedule(
    cycle_ns=100,
    windows=(GateWindow(0, 100, frozenset({0})),),
    link_rate_bits_per_ns=8,
    credit_shapers={              # 可选;{队列号: CreditShaper},默认 None 表示不整形
        0: CreditShaper(
            idle_slope=1,         # 信用增加斜率(信用/ns),正整数
            send_slope=-2,        # 发送期间信用斜率,负整数;两者必须一正一负
            hi_credit=20,         # 信用上限,>= 0
            lo_credit=-20,        # 信用下限,<= 0
        ),
    },
)
```

整形语义(本题规定,逐队列独立计信用):

- 有积压(已到达未发送)且本队列未在发送:信用按 `idle_slope` 增加,封顶 `hi_credit`;
- 本队列发送期间:信用按 `send_slope` 减少,封底 `lo_credit`;
- 队列变空:正信用立即归 0;负信用继续按 `idle_slope` 恢复到 0 为止;
- **门关闭期间信用冻结**(本题规定,不套用其他标准的开门恢复行为);
- 仅当门打开 **且信用非负** 才可开始一帧;开始后按原规则发完(不可抢占)。
- 整形队列的等待原因新增 `credit-negative`。

配置整形后,内部时间与信用用 `fractions.Fraction` 精确表示,事件跳跃直接算出
下一次信用到 0 或门切换的时刻,不做小步轮询。`FrameResult` 的
`start_ns`/`end_ns`/`waited_ns` 在不能整除时为 `Fraction`(精确值),可整除时
仍为 `int`;`credit_shapers=None`(默认)时走旧引擎,输出与旧版本完全一致(纯 int)。

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

## 错误语义

- 所有非法输入抛 `TSNGateError`(继承 `ValueError`),消息含可定位字段路径,
  如 `arrivals[2].size_bytes: -1 小于下限 1`;不静默纠正。
- 整数参数拒绝 `bool`;数值输入拒绝 `NaN`/`Infinity`;时间参数拒绝一切非 `int`
  (包括 `3.0` 这样的整值浮点)。
- `BudgetExhaustedError` 是 `TSNGateError` 的子类,可单独捕获。
- 整形参数同样拒绝非法值:`idle_slope <= 0`、`send_slope >= 0`(必须一正一负)、
  `hi_credit < 0`、`lo_credit > 0`、`credit_shapers` 非映射、队列号越界或值不是
  `CreditShaper`,都抛带字段路径的 `TSNGateError`。

## 输入上限

- 队列:最多 4 个,编号 `0..3`(`MAX_QUEUES`)。
- `cycle_ns >= 1`,`link_rate_bits_per_ns >= 1`,`guard_ns >= 0`,`size_bytes >= 1`,
  `arrive_ns >= 0`,`deadline_ns >= 0`;`clock_offset_ns` 可为任意整数。
- 窗口数量与到达帧数无硬上限;事件预算 `max_events` 默认 1 000 000
  (正常输入下每帧只产生常数个事件,预算仅是防错安全阀)。
- 整形斜率/上下限为整数(信用/纳秒、信用);`credit_shapers` 的队列号须在 `0..3`。

## 设计取舍

- **事件驱动**而非逐 tick:可发性只在到达、窗口边沿、链路空闲时刻变化,
  跳转这些事件点即可,结果与逐 ns 仿真一致(测试中用独立逐 tick 参考交叉验证)。
- **整形用事件跳跃 + 精确分数时间**:信用到 0 的时刻、门切换时刻直接解析计算,
  相邻事件点之间信用按区间解析推进并夹取上下限,避免小步轮询的累计误差;
  测试中用独立的细时间格(固定小量子)参考对小整数配置做交叉验证。
- **永不可发预先证明**:`时长+guard > 该队列最长窗口` 是充要条件(窗口周期重复),
  因此可在仿真前判定,从机制上排除死循环。
- **全整数运算**:唯一除法是序列化时长的向上取整,无浮点参与。

## 误差口径

本模块不使用浮点:所有时间为整数纳秒,序列化时长为精确整数上取整
`ceil(size_bytes*8/rate)`,输出不存在浮点误差,测试按精确相等断言。
配置信用整形后,信用与内部时间用 `fractions.Fraction` 精确表示,输出的分数时刻
(如信用恢复到 0 的 1/3 ns 时刻)为精确有理数,同样不存在浮点误差。

## 已知限制

- 只实现本题契约的有限子集,**不宣称兼容完整 IEEE 802.1Qbv**:无帧抢占、
  无逐队列 guard、无多跳/多链路、无时钟漂移(仅固定偏差)、无控制面。
- guard 为全局单值;deadline 违例只报告不阻止发送。
- 等待原因为定性标签(出现过的原因集合),不输出逐段等待时间线。
- 信用整形为本题规定语义的子集:门关闭冻结信用、空队列正信用归 0 等均以本文为准,
  不宣称兼容完整 IEEE 802.1Qav;斜率只支持整数(信用/纳秒)。

## 运行

```bash
python -m unittest discover -s tests -v   # 单元测试 + 与独立逐 tick 参考的随机对照
python demo.py                            # 演示:门控造成的错过窗口 + 非法输入拒绝
python demo_iteration.py                  # 演示:信用整形控速 + 负信用遇关门冻结边界
```

两条命令均在数秒内完成,全程离线。
