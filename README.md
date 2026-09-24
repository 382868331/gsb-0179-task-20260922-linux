# TSN 门控发送验证(tsn_gate)

离线、确定性的时间敏感网络(TSN)门控调度工具集,全部基于整数纳秒、仅标准库:

1. **单跳门控重放**(`tsn_gate.py`):给定帧到达序列与周期门控表,
   计算每帧的发送开始/结束时刻、等待原因与 deadline 违例;任何窗口都容纳不下的帧
   被证明并标记为“永不可发”,不会死循环。
2. **多跳存储转发排程合成**(`tsn_synth.py`,本次新增):给定固定路径、整数周期、
   帧大小与端到端截止期,枚举合成超周期内逐链路、互不冲突的可重放门表;
   限 4 条流、每条 3 跳、短超周期。可行 / 已证明无解 / 预算不足三种结论严格分开。

## 环境

- Python 3.14.7(仅标准库,无需安装任何第三方依赖,Linux 原生运行)
- 核心、测试、演示全部离线;示例数据由仓库内确定性代码生成

## 接口一:单跳重放(tsn_gate)

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

## 接口二:多跳排程合成(tsn_synth)

```python
from tsn_synth import Link, FlowSpec, synthesize, replay_synthesis, verify_synthesis

l0, l1, l2 = Link(0), Link(1), Link(2)   # 默认 8 bit/ns(1 字节=1ns)、guard=0
flows = [
    # flow_id, 固定路径(1..3 条 Link), 周期, 字节数, 相对截止期(<= 周期)
    FlowSpec(10, (l0, l1, l2), period_ns=10, size_bytes=2, deadline_ns=6),
    FlowSpec(11, (l0, l2),     period_ns=10, size_bytes=2, deadline_ns=10),
]
result = synthesize(flows, max_nodes=1_000_000)
```

流在每个 `k*period_ns` 释放一帧;跳时长 `d = ceil(size_bytes*8 / link.rate)`。
合成器对每流每跳选择周期内整数偏移,满足存储转发(`跳h开始 >= 跳h-1结束`)、
`d+guard <= 周期`、末跳结束 `<= 释放+deadline`;同一链路上任意两个
`[开始, 结束+guard)` 区间(含跨周期实例)不得重叠,半开区间端点相接允许。
超周期 `H = lcm(各周期)`,排程在 `[0,H)` 内合成并循环。

### 输出 `SynthesisResult`

| 字段 | 含义 |
| --- | --- |
| `status` | 目前仅 `SynthesisStatus.FEASIBLE`;无解与预算不足通过异常表达 |
| `hyperperiod_ns` | 超周期 H(ns) |
| `flow_plans` | 每条流的 `FlowPlan`:按输入下标分配的 `queue`、逐跳 `HopPlan`(链路、时长、相对偏移) |
| `transmissions` | 超周期内每个发送实例:释放/到达/开始/结束时刻、绝对 deadline |
| `link_gates` | 每条链路的 `LinkGatePlan`:合成门表(`GateSchedule`,周期=H)+ 自洽的 `Frame` 到达序列 |

**可重放**:`replay_synthesis(result)` 调用既有 `tsn_gate.replay` 逐链路重放;
`verify_synthesis(result)` 额外逐实例比对开始/结束时刻,并独立核对存储转发、
截止期与同链路区间不相交。`synthesize` 返回前已自动调用 `verify_synthesis`。

### 三种结论(严格分开)

- 可行:返回 `SynthesisResult`;
- **已证明无解**:抛 `InfeasibleScheduleError`(`TSNGateError` 子类)。
  搜索前先给出结构性证明(makespan > deadline、单跳+guard > 周期、
  链路占用率含 guard > 100%);其余无解通过枚举完整偏移空间证明;
- **预算不足**:抛 `SynthesisBudgetExhaustedError`(同时是
  `tsn_gate.BudgetExhaustedError` 的子类)。枚举节点预算 `max_nodes`
  默认 1 000 000;耗尽只代表搜索被截断,既非可行也非无解。

### 输入上限与校验

- 流数 1..4(`MAX_FLOWS`),每流路径 1..3 跳(`MAX_HOPS`);
- `period_ns >= 1`、`size_bytes >= 1`、`1 <= deadline_ns <= period_ns`;
  `Link.rate_bits_per_ns >= 1`、`Link.guard_ns >= 0`、`link_id >= 0`;
- 超周期 `lcm(周期) <= 10_000ns`(`MAX_HYPERPERIOD_NS`),
  超周期内发送实例总数 <= 10_000(`MAX_TOTAL_INSTANCES`),超出按非法输入拒绝;
- 同一 `link_id` 在所有流中必须具有相同 rate/guard;`flow_id` 不得重复;
- 整数校验规则与单跳接口一致(拒绝 bool、NaN/Infinity、整值 float),
  错误消息带 `flows[i].path[j]` 等字段定位;校验全部通过后才开始计算,
  非法批次整体抛错,不产生部分结果、不修改入参。

## 语义约定(接口一)

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

## 输入上限

- 队列:最多 4 个,编号 `0..3`(`MAX_QUEUES`)。
- `cycle_ns >= 1`,`link_rate_bits_per_ns >= 1`,`guard_ns >= 0`,`size_bytes >= 1`,
  `arrive_ns >= 0`,`deadline_ns >= 0`;`clock_offset_ns` 可为任意整数。
- 窗口数量与到达帧数无硬上限;事件预算 `max_events` 默认 1 000 000
  (正常输入下每帧只产生常数个事件,预算仅是防错安全阀)。

## 设计取舍

- **事件驱动**而非逐 tick:可发性只在到达、窗口边沿、链路空闲时刻变化,
  跳转这些事件点即可,结果与逐 ns 仿真一致(测试中用独立逐 tick 参考交叉验证)。
- **永不可发预先证明**:`时长+guard > 该队列最长窗口` 是充要条件(窗口周期重复),
  因此可在仿真前判定,从机制上排除死循环。
- **全整数运算**:唯一除法是序列化时长的向上取整,无浮点参与。
- **合成用枚举而非启发式**:题面限定 <=4 流、<=3 跳、短超周期,偏移空间可穷尽;
  每流偏移链惰性生成,跨流 DFS 按字典序找首个解。周期实例冲突判定有两种
  独立实现(小规模直接展开;大规模用 CRT 相位解析判定),测试中互相交叉验证。
- **合成结果必须可重放**:门表与到达序列用既有 `replay` 逐实例核对,
  另做不依赖重放的结构核对(存储转发、截止期、区间不相交)。

## 误差口径

本项目不使用浮点:所有时间为整数纳秒,序列化时长为精确整数上取整
`ceil(size_bytes*8/rate)`,利用率比较用 `fractions.Fraction` 精确判定,
输出不存在浮点误差,测试按精确相等断言。

## 已知限制

- 只实现本题契约的有限子集,**不宣称兼容完整 IEEE 802.1Qbv**:无帧抢占、
  无逐队列 guard、无时钟漂移(仅固定偏差)、无控制面、无在线重配置。
- 单跳接口:guard 为全局单值;deadline 违例只报告不阻止发送;
  等待原因为定性标签(出现过的原因集合),不输出逐段等待时间线。
- 合成接口:无传播时延、无 jitter 缓冲建模;队列按输入下标静态分配
  (4 流 ≤ 4 队列);无保护带联合优化、无最优性证书(字典序首个可行解即返回);
  截止期仅支持 `<= 周期` 的约束截止期;枚举法受显式上限(流/跳/超周期)约束,
  超范围输入直接拒绝而非降级搜索。

## 运行

```bash
python -m unittest discover -s tests -v   # 单跳测试 + 合成测试(均含独立参考对照)
python demo.py                            # 演示:单跳重放 + 合成正常/边界/无解/预算/非法
```

两条命令均在数秒内完成(实测演示约 0.1 秒),全程离线。
