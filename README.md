# TSN 门控 + 信用整形发送验证(tsn_gate)

离线、确定性的时间敏感网络(TSN)门控发送重放模块。给定帧到达序列与周期门控表,
计算每帧的发送开始/结束时刻、等待原因与 deadline 违例;任何窗口都容纳不下的帧
被证明并标记为“永不可发”,不会死循环。

本次迭代:在门控之外为单个类别(队列)叠加**信用整形**(credit shaping),
由调用方给定一正(idleSlope)一负(sendSlope)两种斜率,控制该队列的发送节奏。

## 环境

- Python 3.14.7(仅标准库,无需安装任何第三方依赖,Linux 原生运行)
- 核心、测试、演示全部离线;示例数据由仓库内确定性代码生成

### venv 准备(可选)

模块只用标准库,系统 Python 3.14.7 即可直接运行;如需隔离环境:

    python3.14 -m venv .venv
    .venv/bin/python -m unittest discover -s tests -v
    .venv/bin/python demo_iteration.py

## 接口

```python
from tsn_gate import Frame, GateWindow, GateSchedule, CreditShaper, replay

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

# 本次迭代新增:为队列 1 叠加信用整形(可选,不给 shapers 时行为与旧版一致)
shapers = (
    CreditShaper(
        queue=1,
        idle_slope=1,            # 有积压且未发送时每 ns 增加的信用,必须 > 0
        send_slope=-3,           # 发送期间每 ns 减少的信用,必须 < 0
        hi_credit=None,          # 信用上限(>= 0),None 表示不设限
        lo_credit=None,          # 信用下限(<= 0),None 表示不设限
    ),
)
result2 = replay(arrivals, schedule, shapers=shapers)
```

`replay(arrivals, gate_schedule, *, max_events=DEFAULT_MAX_EVENTS, shapers=None)`
是纯函数:不修改入参,要么返回完整 `ReplayResult`,要么抛异常,
不存在未说明的部分结果。`shapers` 为 `CreditShaper` 序列,每个队列至多一个;
`shapers=None`(默认)即旧的无整形模式,输出与上一迭代完全一致。

### 信用整形语义(本题契约)

- 信用初始为 0。**只有门打开且信用非负才能开始帧**;开始后按原规则发完,
  发送期间信用可以变负。
- 有积压(队列内有等待帧)且未在发送时,信用按 `idle_slope` 增加;
  本队列发送期间按 `send_slope` 减少(链路被其他队列占用算“未发送”,照常累积)。
- 队列变空时:正信用**立即归 0**;负信用继续按 `idle_slope` 恢复,到 0 为止
  (不会“超额恢复”出正信用)。
- **门关闭期间信用冻结**(本题明确规定,不采用其他标准的关门行为);
  恢复/下降都只按开门时长累计。
- 信用被 `hi_credit` / `lo_credit` 截断(达到上下限后停在该处)。
- 斜率与上下限接受 `int` 或 `fractions.Fraction`,要求一正一负
  (`idle_slope > 0 > send_slope`),否则报错。
- 信用与时间为**精确分数**演算:信用归零时刻由事件跳跃一次算出,
  不做小步轮询,无累计误差。因此整形队列的 `start_ns` / `end_ns` /
  `waited_ns` 可能是 `fractions.Fraction`;恰为整数时仍输出 `int`,
  与旧输出形态一致。
- 等待原因新增 `WaitReason.CREDIT_NEGATIVE`(`"credit-negative"`):
  门开着但信用为负、等待恢复。

### 输出

`ReplayResult.frames`(按到达序号排序)中每个 `FrameResult`:

| 字段 | 含义 |
| --- | --- |
| `seq` / `queue` / `arrive_ns` | 到达序号(输入下标)/ 队列 / 到达时刻(参考时钟) |
| `duration_ns` | 序列化时长 = `ceil(size_bytes*8 / link_rate_bits_per_ns)` |
| `status` | `SENT` 或 `NEVER_TRANSMITTABLE` |
| `start_ns` / `end_ns` | 发送开始/结束(参考时钟;永不可发为 `None`) |
| `waited_ns` | `start_ns - arrive_ns` |
| `wait_reasons` | 等待期间出现过的原因:`gate-closed`、`insufficient-window`、`link-busy`、`outranked`、`credit-negative` |
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
- 信用参数(`idle_slope`/`send_slope`/`hi_credit`/`lo_credit`)接受 `int` 与
  `Fraction`,拒绝 `bool` 与一切 `float`;斜率必须一正一负,上限 >= 0、下限 <= 0;
  同一队列重复配置整形器、`shapers` 元素类型错误,均抛 `TSNGateError`。
- `BudgetExhaustedError` 是 `TSNGateError` 的子类,可单独捕获。

## 输入上限

- 队列:最多 4 个,编号 `0..3`(`MAX_QUEUES`)。
- `cycle_ns >= 1`,`link_rate_bits_per_ns >= 1`,`guard_ns >= 0`,`size_bytes >= 1`,
  `arrive_ns >= 0`,`deadline_ns >= 0`;`clock_offset_ns` 可为任意整数。
- 窗口数量与到达帧数无硬上限;事件预算 `max_events` 默认 1 000 000
  (正常输入下每帧只产生常数个事件,预算仅是防错安全阀)。

## 设计取舍

- **事件驱动**而非逐 tick:可发性只在到达、窗口边沿、链路空闲时刻变化,
  跳转这些事件点即可,结果与逐 ns 仿真一致(测试中用独立逐 tick 参考交叉验证)。
- **信用事件跳跃**:信用为负时直接算出“按 idle_slope 恢复到 0 所需的开门时长”,
  再由周期门表反解出精确(可能是分数的)归零时刻,一次跳转到位;
  关门段信用冻结,恢复只累计开门时长。测试中用独立的“细时间格 + 精确子步”
  参考实现(不共享被测代码)对小整数配置做随机交叉验证。
- **永不可发预先证明**:`时长+guard > 该队列最长窗口` 是充要条件(窗口周期重复),
  因此可在仿真前判定,从机制上排除死循环。
- **无浮点运算**:整数路径保持纯整数;信用路径用 `fractions.Fraction`,
  不存在浮点累计误差。

## 误差口径

本模块不使用浮点:无整形时所有时间为整数纳秒,序列化时长为精确整数上取整
`ceil(size_bytes*8/rate)`;整形时信用与时间用 `fractions.Fraction` 精确演算,
输出不存在浮点误差,测试按精确相等断言。

## 已知限制

- 只实现本题契约的有限子集,**不宣称兼容完整 IEEE 802.1Qbv**:无帧抢占、
  无逐队列 guard、无多跳/多链路、无时钟漂移(仅固定偏差)、无控制面。
- 关门期间信用冻结是本题规定,与 802.1Qav 标准行为不同,不互相替代。
- guard 为全局单值;deadline 违例只报告不阻止发送。
- 等待原因为定性标签(出现过的原因集合),不输出逐段等待时间线。
- 整形队列的开始/结束时刻可能是 `Fraction`(精确分数纳秒);需要整数时间的
  下游应自行决定取舍策略,本模块不做隐式舍入。

## 验证命令

    python -m unittest discover -s tests -v   # 全部单元测试(含新旧回归与随机对照)
    python demo.py                            # 上一迭代的门控演示(保持不变)
    python demo_iteration.py                  # 本次迭代:信用整形功能 + 边界情况

## 运行

```bash
python -m unittest discover -s tests -v   # 单元测试 + 与独立逐 tick 参考的随机对照
python demo.py                            # 演示:门控造成的错过窗口 + 非法输入拒绝
```

两条命令均在数秒内完成,全程离线。
