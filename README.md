# TSN 门控发送验证(tsn_gate)

离线、确定性的时间敏感网络(TSN)门控发送重放模块。给定帧到达序列与周期门控表,
计算每帧的发送开始/结束时刻、等待原因与 deadline 违例;任何窗口都容纳不下的帧
被证明并标记为“永不可发”,不会死循环。

本迭代新增**帧抢占**(可选):express(紧急)与 preemptable(普通)两类帧,
普通帧按固定字节片段发送、可在片段边界被紧急帧抢占,暂停后保留剩余字节、
恢复时计入额外的片段开销。不启用抢占时行为与上一迭代完全一致。

## 环境

- Python 3.14.7(仅标准库,无需安装任何第三方依赖,Linux 原生运行)
- 核心、测试、演示全部离线;示例数据由仓库内确定性代码生成
- 如需隔离环境:`python3.14 -m venv .venv && . .venv/bin/activate`;
  因无第三方依赖,直接使用系统 `python3.14` 亦可,requirements.txt 为空注释

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
from tsn_gate import (
    Frame, GateWindow, GateSchedule, PreemptionConfig, TrafficClass, replay,
)

schedule = GateSchedule(
    cycle_ns=1000,
    windows=(GateWindow(0, 600, frozenset({0})),),
    link_rate_bits_per_ns=8,
    preemption=PreemptionConfig(          # 不传或传 None = 不启用抢占(旧行为)
        fragment_bytes=20,                # 普通帧片段载荷上限,>= 1
        fragment_overhead_bytes=4,        # 每片段附加开销(暂停恢复也计入),>= 0
    ),
)
arrivals = [
    Frame(queue=0, arrive_ns=0, size_bytes=100),                  # 默认普通帧
    Frame(queue=0, arrive_ns=55, size_bytes=10,
          traffic_class=TrafficClass.EXPRESS),                    # 紧急帧
]
result = replay(arrivals, schedule)
```

抢占语义(仅 `preemption` 启用时):

- 普通帧(PREEMPTABLE,默认类别)按片段发送:每片载荷至多 `fragment_bytes`,
  线上时长 = `ceil((载荷 + fragment_overhead_bytes) * 8 / rate)`;
  只能在下一片段边界暂停,紧急帧到达后等到当前片段结束(下一个合法边界)再发,
  已开始的片段不会被截断。
- 紧急帧(EXPRESS)原子发送、不分片,也必须整体满足自己队列的门
  (时长+guard 落入当前窗口剩余部分),否则照常等待。
- 门关闭不会截断已开始的片段,但剩余窗口容不下 片段时长+guard 时不能再开
  新片段;被门拦住的普通帧在窗口重开后从剩余字节继续。
- 多个暂停帧按 (原开始时刻, 到达序号) 顺序恢复;暂停帧优先于尚未开始的普通帧;
  紧急帧优先于一切普通帧。同类别竞争仍按 (队列号升序, 到达序号升序)。
- 同一时刻的事件处理次序固定:片段完成 -> 门状态切换(半开区间自然定义)
  -> 帧到达 -> 调度决策。所有时间仍为整数纳秒。
- 抢占模式下“永不可发”的判定:紧急帧 时长+guard > 最长窗口;
  普通帧 最大片段(含开销)+guard > 最长窗口(长帧可跨窗口续传,不再一律判死)。

抢占模式下 `FrameResult` 新增字段(旧字段含义不变):

| 字段 | 含义 |
| --- | --- |
| `fragments` | 各片段 `(start_ns, end_ns)`,按时间升序;未发送为 `()` |
| `preempted_count` | 暂停期间被紧急帧插队的次数 |
| `overhead_ns` | 片段化引入的额外线上时长(各片段时长和 − `duration_ns`) |

`WaitReason` 新增 `PREEMPTED`(`"preempted"`,被紧急帧在片段边界暂停)。

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
- `Frame.traffic_class` 只接受 `TrafficClass` 枚举成员(拒绝字符串);
  `GateSchedule.preemption` 只接受 `PreemptionConfig` 或 `None`;
  `fragment_bytes >= 1`,`fragment_overhead_bytes >= 0`。
- `BudgetExhaustedError` 是 `TSNGateError` 的子类,可单独捕获。

## 输入上限

- 队列:最多 4 个,编号 `0..3`(`MAX_QUEUES`)。
- `cycle_ns >= 1`,`link_rate_bits_per_ns >= 1`,`guard_ns >= 0`,`size_bytes >= 1`,
  `arrive_ns >= 0`,`deadline_ns >= 0`;`clock_offset_ns` 可为任意整数。
- 抢占配置:`fragment_bytes >= 1`,`fragment_overhead_bytes >= 0`,均为整数。
- 窗口数量与到达帧数无硬上限;事件预算 `max_events` 默认 1 000 000
  (正常输入下每帧只产生常数个事件,预算仅是防错安全阀)。

## 设计取舍

- **事件驱动**而非逐 tick:可发性只在到达、窗口边沿、链路空闲时刻变化,
  跳转这些事件点即可,结果与逐 ns 仿真一致(测试中用独立逐 tick 参考交叉验证)。
- **抢占 correctness 由逐字节参照兜底**:`tests/reference_preempt.py` 以逐 ns
  朴素仿真重述抢占语义,并从核心输出的片段序列独立重建链路占用,核对
  无重叠发送、无重复/丢失字节、无凭空多出的空闲;随机用例与核心逐片段对拍。
- **片段开销建模**:每个片段(含被暂停后恢复的片段)都计
  `fragment_overhead_bytes` 开销,抢占越频繁普通帧线上开销越大,
  以此体现“紧急帧延迟降低 vs 普通帧额外开销”的取舍。
- **永不可发预先证明**:`时长+guard > 该队列最长窗口` 是充要条件(窗口周期重复),
  因此可在仿真前判定,从机制上排除死循环。
- **全整数运算**:唯一除法是序列化时长的向上取整,无浮点参与。

## 误差口径

本模块不使用浮点:所有时间为整数纳秒,序列化时长为精确整数上取整
`ceil(size_bytes*8/rate)`,输出不存在浮点误差,测试按精确相等断言。

## 已知限制

- 只实现本题契约的有限子集,**不宣称兼容完整 IEEE 802.1Qbv/802.1Qbu**:
  无逐队列 guard、无多跳/多链路、无时钟漂移(仅固定偏差)、无控制面。
- 抢占也是简化模型:紧急帧若当前不满足自己的门,普通帧可继续发片段
  (不实现 802.1Qbu 的 hold/release 预约);片段开销按固定字节计,
  不建模具体物理层编码。
- guard 为全局单值;deadline 违例只报告不阻止发送。
- 等待原因为定性标签(出现过的原因集合),不输出逐段等待时间线。

## 运行

```bash
python -m unittest discover -s tests -v   # 单元测试 + 与独立逐 tick 参考的随机对照
python demo.py                            # 上一迭代演示:门控造成的错过窗口 + 非法输入拒绝
python demo_iteration.py                  # 本迭代演示:抢占取舍 + 门关闭边界情况
```

两条命令均在数秒内完成,全程离线。
