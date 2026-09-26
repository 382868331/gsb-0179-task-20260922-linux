# TSN 门控发送验证(tsn_gate)

离线、确定性的时间敏感网络(TSN)门控发送重放模块。给定帧到达序列与周期门控表,
计算每帧的发送开始/结束时刻、等待原因与 deadline 违例;任何窗口都容纳不下的帧
被证明并标记为“永不可发”,不会死循环。

本次迭代新增**帧抢占**:队列分为 express(紧急)与 preemptable(可抢占)两类,
紧急帧可在固定片段边界打断普通帧,暂停帧保留剩余字节、恢复时计入额外片段开销。
旧接口 `replay` 行为完全不变(不启用抢占时的原结果保留)。

## 环境

- Python 3.14.7(仅标准库,无需安装任何第三方依赖,Linux 原生运行)
- 核心、测试、演示全部离线;示例数据由仓库内确定性代码生成

### venv 准备(可选)

不依赖任何第三方包,系统 Python 3.14.7 即可直接运行。若希望使用隔离环境:

```bash
python3.14 -m venv .venv
.venv/bin/python -m unittest discover -s tests -v
```

仓库内的 `.venv` 符号链接只是现成解释器的一种提供方式,并非必须。

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

### 帧抢占(新接口)

```python
from tsn_gate import PreemptionConfig, replay_preemptive

config = PreemptionConfig(
    express_queues=frozenset({0}),   # 紧急队列;其余队列为可抢占
    fragment_size_bytes=30,          # 可抢占帧的固定片段载荷(字节,>= 1)
    fragment_overhead_bytes=6,       # 每次恢复(第 2 片起)额外开销字节,>= 0
)
result = replay_preemptive(arrivals, schedule, config, max_events=1_000_000)
```

`replay_preemptive(arrivals, gate_schedule, config, *, max_events=...)`
与 `replay` 同为纯函数,输入校验与错误语义一致,返回同一 `ReplayResult` 类型。

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
| `fragments` | 抢占模式下每片的 `(start_ns, end_ns)`;旧 `replay` 输出为 `()` |
| `preempted_count` | 在片段边界被紧急帧抢占而暂停的次数(旧接口输出为 0) |
| `overhead_bytes` | 恢复片段引入的额外开销字节总数(旧接口输出为 0) |

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

### 抢占语义(replay_preemptive)

- preemptable 帧按 `fragment_size_bytes` 切成固定片段,**只能在片段边界暂停**;
  紧急帧到达后等到当前片段结束(下一个合法边界)再发送。
- 暂停帧保留剩余字节;恢复时(第 2 片起)每片额外计入
  `fragment_overhead_bytes` 字节开销,开销字节同样占用链路时间。
- **门关闭不会截断已经开始的片段**(片段必然跑到完成),但门关闭后不能再开
  新片段:启动条件为 门开着 且 剩余窗口 > guard_ns。紧急帧也必须满足自己的门,
  门未开时原地等待,可抢占帧可继续发送。
- 多个暂停帧按**原开始顺序**(首次开始时刻早者优先)恢复;暂停帧优先于从未
  开始的可抢占帧。同类竞争:紧急帧按 (到达时刻, 到达序号),
  新可抢占帧按 (队列号, 到达序号)。
- **同刻处理次序**:片段完成 -> 门状态按新相位求值 -> 帧到达并入队 -> 启动决策。
  所有时间仍为整数纳秒。
- 抢占模式下的“永不可发”证明:该队列最长窗口 <= guard_ns(连一个片段都开不了)。
- `wait_reasons` 新增 `preempted`:该帧在片段边界被紧急帧抢占过。

## 错误语义

- 所有非法输入抛 `TSNGateError`(继承 `ValueError`),消息含可定位字段路径,
  如 `arrivals[2].size_bytes: -1 小于下限 1`;不静默纠正。
- 整数参数拒绝 `bool`;数值输入拒绝 `NaN`/`Infinity`;时间参数拒绝一切非 `int`
  (包括 `3.0` 这样的整值浮点)。
- `BudgetExhaustedError` 是 `TSNGateError` 的子类,可单独捕获。
- `PreemptionConfig` 的非法字段同样抛 `TSNGateError`,路径形如
  `PreemptionConfig.fragment_size_bytes`;`replay_preemptive` 的 `config`
  参数类型不符也立即拒绝。

## 输入上限

- 队列:最多 4 个,编号 `0..3`(`MAX_QUEUES`)。
- `cycle_ns >= 1`,`link_rate_bits_per_ns >= 1`,`guard_ns >= 0`,`size_bytes >= 1`,
  `arrive_ns >= 0`,`deadline_ns >= 0`;`clock_offset_ns` 可为任意整数。
- 窗口数量与到达帧数无硬上限;事件预算 `max_events` 默认 1 000 000
  (正常输入下每帧只产生常数个事件,预算仅是防错安全阀)。
- `fragment_size_bytes >= 1`,`fragment_overhead_bytes >= 0`;
  `express_queues` 为 `0..3` 的队列号集合(可为空,表示全部可抢占)。

## 设计取舍

- **事件驱动**而非逐 tick:可发性只在到达、窗口边沿、链路空闲时刻变化,
  跳转这些事件点即可,结果与逐 ns 仿真一致(测试中用独立逐 tick 参考交叉验证)。
- **抢占交叉验证**:抢占核心同样事件驱动;测试中用独立的逐字节离散参考
  (`tests/reference.py` 的 `reference_preemptive`,不导入被测核心)做随机对照,
  并由 `verify_occupancy` 核验链路占用:无重叠发送、无重复/丢失字节、
  无凭空空闲(每个空闲 ns 都不存在可发的已到达帧)。
- **永不可发预先证明**:`时长+guard > 该队列最长窗口` 是充要条件(窗口周期重复),
  因此可在仿真前判定,从机制上排除死循环。
- **全整数运算**:唯一除法是序列化时长的向上取整,无浮点参与。

## 误差口径

本模块不使用浮点:所有时间为整数纳秒,序列化时长为精确整数上取整
`ceil(size_bytes*8/rate)`,输出不存在浮点误差,测试按精确相等断言。

## 已知限制

- 只实现本题契约的有限子集,**不宣称兼容完整 IEEE 802.1Qbv/802.1Qbu**:
  抢占为本题定义的简化模型(固定片段、恢复开销按字节计、无校验和/尾帧处理),
  无逐队列 guard、无多跳/多链路、无时钟漂移(仅固定偏差)、无控制面。
- guard 为全局单值;deadline 违例只报告不阻止发送。
- 等待原因为定性标签(出现过的原因集合),不输出逐段等待时间线。

## 运行

```bash
python -m unittest discover -s tests -v   # 单元测试 + 与独立逐 tick 参考的随机对照
python demo.py                            # 演示:门控造成的错过窗口 + 非法输入拒绝
python demo_iteration.py                  # 演示:抢占降低紧急帧延迟的取舍 + 同刻边界
```

两条命令均在数秒内完成,全程离线。
