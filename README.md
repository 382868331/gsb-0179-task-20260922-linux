# TSN 门控发送验证(tsn_gate)+ 多跳排程合成(hop_schedule)

本仓库包含两个离线、确定性的纯整数纳秒模块(仅标准库):

1. **tsn_gate**:单链路门控发送重放。给定帧到达序列与周期门控表,
   计算每帧的发送开始/结束时刻、等待原因与 deadline 违例;任何窗口都容纳不下的帧
   被证明并标记为“永不可发”,不会死循环。
2. **hop_schedule**(本次新增):多跳存储转发排程合成。给定固定路径的周期流
   (整数周期、发送时长、端到端截止期),完整枚举合成逐跳发送时刻,
   同一链路任意周期实例不冲突;成功时返回可用 tsn_gate.replay 逐链路重放的
   门表排程;无解与预算不足是两种不同的结论,严格分开。

## 环境

- Python 3.14.7(仅标准库,无需安装任何第三方依赖,Linux 原生运行)
- 核心、测试、演示全部离线;示例数据由仓库内确定性代码生成

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

## 误差口径

两个模块均不使用浮点:所有时间为整数纳秒,序列化时长为精确整数上取整
`ceil(size_bytes*8/rate)`,输出不存在浮点误差,测试按精确相等断言。

## 已知限制

- 只实现本题契约的有限子集,**不宣称兼容完整 IEEE 802.1Qbv**:无帧抢占、
  无逐队列 guard、无多跳/多链路、无时钟漂移(仅固定偏差)、无控制面。
- guard 为全局单值;deadline 违例只报告不阻止发送。
- 等待原因为定性标签(出现过的原因集合),不输出逐段等待时间线。

# 多跳存储转发排程合成(hop_schedule)

给定固定路径的周期流集合与链路速率,合成每条流逐跳的发送时刻,
使得:存储转发先后有序、各流末跳在截止期前结束、同一链路上任意两条流的
任意周期实例互不重叠。找到则返回**可重放**排程(每条链路附带一个
`GateSchedule` + 帧列表,用 `tsn_gate.replay` 重放必然复现合成时刻,
合成后已逐链路实测校验);无解与预算不足分别抛出不同异常。

## 接口

```python
from hop_schedule import Flow, Network, Link, synthesize

network = Network(
    links=(Link("A", "B"), Link("B", "C")),   # 有向链路 "源->目的"
    default_rate_bits_per_ns=8,               # 缺省链路速率(8 = 1B/ns)
    default_guard_ns=0,                       # 缺省 guard(只影响重放窗口长度)
    # link_rates_bits_per_ns={"B->C": 4},     # 可选:逐链路覆盖
    # link_guards_ns={"A->B": 5},
)
flows = [
    Flow(name="f0", path=("A", "B", "C"),     # 固定路径,1..3 个传输跳
         period_ns=100,                       # 整数周期
         deadline_ns=60,                      # 相对截止期:末跳结束 <= offset+deadline
         sizes_bytes=10,                      # 每跳字节数:int 或逐跳元组
         offset_ns=0),                        # 首发释放时刻(默认 0)
]
schedule = synthesize(flows, network)         # 可加 max_search_ns= / max_nodes=
```

`MultiHopSchedule` 主要内容:

| 字段/方法 | 含义 |
| --- | --- |
| `hyperperiod_ns` | 超周期 `lcm(所有周期)`;排程按此周期无限重复 |
| `flows[i].hops[h]` | `HopSchedule(flow, hop_index, link, start_ns, end_ns, duration_ns)` |
| `links` / `hops` | 实际使用的链路;全部跳按 `(开始,链路,流,跳序)` 排序 |
| `link_replays[link]` | `LinkReplay`:该链路的 `GateSchedule`、帧、期望时刻与实测 `ReplayResult` |
| `replay_link(link)` / `replay_all()` | 用 tsn_gate.replay 重新重放(纯计算,不改排程) |

另有便捷入口 `replay_schedule(schedule)` 等价于 `schedule.replay_all()`。

## 语义约定

- 所有时间为整数纳秒,时从 0 开始;跳 h 的序列化时长 =
  `ceil(size_bytes[h]*8 / rate_link)`,精确整数。
- 存储转发:第 h 跳最早在第 h-1 跳结束时刻开始(第 0 跳最早为 `offset_ns`)。
- 周期实例:每跳在 t 发送后,后续实例在 `t + k*period` 发送;两条周期区间列
  是否冲突用模运算判据 O(1) 判定(`r=(t1-t2) mod gcd(p1,p2)` 落在对方时长
  窗口内即冲突),与“超周期内逐实例展开比较”数学等价(测试中与独立展开对拍)。
- 流的相邻周期实例不得自我重叠:任一跳时长 > 该流周期时直接判定无解。
- 可重放门表:每条链路一个 `GateSchedule`,每个 (流,链路) 恰好一个窗口
  `[t, t+时长+guard)`,窗口在排程起点才打开;按窗口起点归纳可证 replay 中
  每帧恰在 t 发送,合成后逐链路实测复核,不符即报内部错误。

## 结论与错误语义(三种结论严格分开)

- **成功**:返回 `MultiHopSchedule`(含可重放门表)。
- **无解**:`InfeasibleError`——完整枚举穷尽后的证明。枚举是完备的:
  每跳候选起点稠密取遍 `[存储转发最早, 截止期允许最晚]` 内每个整数 ns,
  DFS 穷尽即不存在可行排程。
- **预算不足**:`BudgetExhaustedError`——超周期或最晚截止时刻超过
  `max_search_ns`(默认 200 000),或搜索节点超过 `max_nodes`
  (默认 200 000)。搜索被截断或未执行,**不是**无解的证明;可显式调大重试。
- **非法输入**:`TSNGateError`,消息带 `flows[i].field` 定位;先完整校验
  再搜索,任何失败路径都不产生部分结果,也不修改入参。

## 输入上限

- 流最多 `MAX_FLOWS=4` 条;每条路径 2..4 个节点(即 `MAX_HOPS=3` 个传输跳),
  路径不得成环;路径使用的链路必须已在 `Network.links` 声明。
- `period_ns >= 1`,`offset_ns >= 0`,`deadline_ns >= 1`,`sizes_bytes >= 1`;
  整数参数拒绝 `bool`/浮点/NaN/Infinity(与 tsn_gate 同口径)。
- 超周期 `lcm(周期)` 与最晚绝对截止时刻均不得超过 `max_search_ns`
  (题面限定“短超周期”;默认 200 000ns)。

## 设计取舍与已知限制

- **完备枚举而非启发式**:4 流 × 3 跳 × 短超周期内逐 ns 枚举全部候选,
  找到的第一个可行解即返回(不优化任何目标,不保证空隙最小或最早完成)。
- 不做在线配置切换、保护带联合优化、最优性证书;guard 只用于生成重放窗口,
  不参与冲突判定(冲突按发送区间 `[t, t+时长)` 判定,半开、相切不冲突)。
- 流的首个实例之前的瞬态按同一周期模式保守处理(稳态超周期模型)。
- 稠密枚举的成本随时间轴长度增长;病态输入(超大超周期/截止期)由
  `max_search_ns`/`max_nodes` 预算拦截,不会失控。

## 运行

```bash
python -m unittest discover -s tests -v   # 全部测试:旧重放 + 新合成(含独立参考对拍)
python demo.py                            # 演示:正常合成+重放、边界、无解/预算/非法输入
```

两条命令均在数秒内完成(实测 < 1s),全程离线。
