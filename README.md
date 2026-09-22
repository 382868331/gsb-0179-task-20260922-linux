# TSN 门控发送验证模块（`tsn_gating`）

离线、确定性的“时间敏感网络时间感知门控（IEEE 802.1Qbv 思路的有限子集，**不宣称标准兼容**）”
发送回放模块。给定帧到达序列与周期门表，逐事件精确判定每个帧何时可以开始发送、
何时结束、为何等待，以及是否错过 deadline；对“任何周期窗口都装不下”的帧给出
**可证明的永不可发**结论，而不是死循环。

## 运行环境

- Python **3.14.7**（仅使用标准库，`dataclasses` 都不需要；Python 3.10+ 语法即可运行）
- Linux 原生运行，**无需安装任何第三方依赖**，无需网络、账号、密钥、数据库、中间件、云 API 或 Docker
- 核心、测试、演示全部离线、确定性

## 准确命令

```bash
# 必要测试（独立逐 tick 参考 + 250 个确定性随机场景 + 边界/错误语义）
python -m unittest discover -s tests -v

# 演示（约毫秒级完成，远小于 8 秒；真实计算，无 sleep、无预录结果）
python demo.py
```

## 公开接口

```python
from tsn_gating import replay, InvalidInputError, BudgetExhaustedError

result = replay(arrivals, gate_schedule)
```

### `gate_schedule`（对象/mapping）

| 字段 | 类型 | 含义 |
|---|---|---|
| `period_ns` | int，1..1_000_000 | 门表周期（本地时钟，纳秒） |
| `link_rate_bps` | int，1..10^12 | 链路速率（比特/秒），整数 |
| `guard_ns` | int，0..1_000_000，默认 0 | 门控保护带；要求窗口结束前额外留出的余量 |
| `clock_offset_ns` | int，-10^9..10^9，默认 0 | 本地时钟相对参考时钟的固定偏差：`local = reference + offset` |
| `windows` | list，0..64 项 | 半开开门区间，按 `period_ns` 循环 |
| `max_run_ns` | int，1..10_000_000，可选 | 搜索时域（从首个到达起）；缺省自动给出确定性的充足上界 |

每个 window：`{"start_ns": int(0..period-1), "duration_ns": int(1..period),
"queues": [0..3 的非空无重复列表]}`，`start+duration <= period`。
原始窗口之间不得重叠（相邻允许，同队列相邻段自动合并）。

### `arrivals`（列表，最多 1000 项）

| 字段 | 类型 | 含义 |
|---|---|---|
| `frame_id` | str/int，可选 | 默认 `frame_{序号}`；不得重复 |
| `queue` | int 0..3 | 队列号；**0 优先级最高** |
| `length_bits` | int 1..10^9 | 帧长；序列化时长 `length_bits*1e9/link_rate_bps` 必须为整数纳秒，否则拒绝 |
| `arrival_ns` | int 0..10^12 | 到达时刻（**参考时钟**，纳秒） |
| `deadline_ns` | int 0..10^12，可选 | 截止时刻（参考时钟）；不得早于到达 |

### 返回值

```python
{
  "frames": [{
    "frame_id", "queue", "arrival_ns", "length_bits", "tx_duration_ns",
    "status": "sent" | "never_sendable",
    "start_ns", "end_ns",            # 参考时钟；never_sendable 时为 None
    "deadline_ns", "deadline_violation": bool,
    "wait_intervals": [{"start_ns", "end_ns", "reason"}, ...],  # 参考时钟，半开
  }, ...],                            # 与输入同序
  "sent_count", "never_sendable_count", "deadline_violation_count",
  "simulated_until_ns", "clock_offset_ns",
}
```

等待原因（公开词汇表）：

- `gate_closed`：本队列车头帧遇到门关闭；
- `window_too_short`：门开着，但当前剩余开门长度 `< tx + guard`；
- `transmitter_busy`：发送器被不可抢占的在途帧占用；
- `queued_behind_head`：同队列前车未发，本帧不能越过它。

原因按**状态快照优先级**记录（同一 tick 只记一个原因）：发送器忙时所有
已到达未发帧记 `transmitter_busy`；空闲时非车头帧记 `queued_behind_head`；
车头帧在门关时记 `gate_closed`，门开但剩余长度不足时记 `window_too_short`。
区间为半开且首尾相接，恰好覆盖每帧从到达至开始发送之间的全部 tick（不含
开始 tick）。

## 语义契约（关键判定）

1. **半开区间循环**：窗口为 `[start, start+duration)`，每 `period_ns` 重复；
   时刻恰好在关闭点上视为**关闭**；序列化区间为 `[start, end)`，在 `end`
   时刻发送器恰好空闲。
2. **不可抢占 + guard**：帧只能在时刻 `t` 开始，当且仅当存在该队列的一个开门
   圆弧实例使 `t + tx_ns + guard_ns <= 窗口结束`（**等号允许**）。开始后即使门在
   发送途中关闭也不打断（跨周期相邻同队列开门段合并为一个圆弧，故跨周期边界发送
   合法）。
3. **调度选择**：发送器空闲且多个队列的车头帧同时满足条件时，按队列优先级
   （0 最高），再按到达时刻，再按输入序号。同队列严格 FIFO，不允许队头阻塞下的越级。
4. **时钟偏差**：`clock_offset_ns` 是输入；门表按本地时钟解释，到达/deadline/输出
   按参考时钟，换算仅为整数加减。
5. **deadline**：发送帧以 `end_ns > deadline_ns` 判违例；`never_sendable` 帧若给了
   deadline，则必然违例。
6. **永不可发是证明，不是超时**：当一个队列周期内所有开门圆弧的最大长度都
   `< tx + guard` 时，该帧在**任何周期、任何到达时刻**都不可能开始，直接标记
   `never_sendable`，不进入事件循环。
7. **搜索预算耗尽是另一回事**：帧本身可发，但在 `max_run_ns`（缺省为自动上界）
   或内部事件上限（1_000_000 次状态变化）内未能完成判定时，抛
   `BudgetExhaustedError`（含 `cutoff_ns` 与 `pending_frame_ids`）。它绝不被伪装成
   `never_sendable`，反之亦然。

## 错误语义

- 所有数值输入拒绝 `NaN`/`+Infinity`/`-Infinity`；整数参数拒绝 `bool`
  （`True/False` 不会被当作 1/0），也拒绝即便整数值的 `float`（本模块只接受精确
  整数纳秒）。错误类型为 `InvalidInputError`（同时是 `ValueError`），消息带字段
  定位路径，如 `arrivals[3].length_bits`、`gate_schedule.windows[1].start_ns`。
- 未知字段、窗口重叠/越界、队列重复、frame_id 重复、deadline 早于到达、序列化时长
  非整数纳秒等一律拒绝。
- 所有校验在任何模拟之前完成：**失败时不产生任何部分结果**；`replay` 是纯函数，
  重复调用结果完全一致。

## 数值与误差口径

- 时间、长度、速率、偏差全部为整数，序列化时长必须整除为整数纳秒；
  **核心算法不使用任何浮点运算，所有边界比较均为精确整数相等/不等比较**，
  因此不存在“宽容差掩盖边界错误”的问题。
- `demo.py` 中仅有的浮点用于打印毫秒级耗时（`time.perf_counter()`），不参与判定。

## 测试独立性说明

- `tests/test_tsn_gating.py` 内的 `tick_reference` 是**独立**的逐纳秒参考模型：
  直接用门表布尔数组枚举每个 tick 的门状态、用区间逐点扫描判定
  `tx+guard` 是否完整落在开门段内，并逐 tick 记录等待原因。它**不调用**
  `tsn_gating` 的任何函数来生成期望值，只用于比对。
- 覆盖：半开边界（恰合/恰不合）、guard 把发送推到下一周期、跨周期合并圆弧、
  相同到达的优先级与序号次序、长帧永不可发、deadline 违例、正/负时钟偏差、
  全部四种等待原因，以及 NaN/Infinity/bool/重叠窗口等错误语义与预算耗尽边界。
- 另有 250 个固定随机种子（`random.Random(20260922)`）小整数场景，将事件驱动核心
  与逐 tick 参考全量比对（含等待区间逐项相等）。模块内部几何自洽仅作补充测试。

## 设计取舍

- 单发送器、帧在队 FIFO、严格优先级非抢占——这是题意直接给出的最小确定性子集。
- 门表按“圆弧”预处理：同队列相邻开门段（含 `[0,a)` 与 `[b,period)` 跨边界相邻）
  合并，跨边界发送因此是普通情形，无需特判。
- 事件驱动（只跳到到达、发送结束、门开关三类时刻），而非逐 tick，以便在接口上限
  规模下仍然高效；正确性由逐 tick 独立参考在小整数场景全量锚定。
- 自动时域上界为“最后到达 + 2(n+2) 周期”：最后到达后每帧至多等待一个周期即轮到，
  系数 2 为安全余量；用户可显式给定 `max_run_ns` 来触发并观察预算耗尽。

## 已知限制（有限子集）

- 不实现 802.1Qbv 的 GateControlEntry 列表、配置变更、长度可变的 MAC/IPG 开销、
  帧分片、Credit-Based Shaper (CBS)、TSN 其他调度器；不读取 PCAP/配置文件。
- 链路速率与帧长必须使序列化时长恰为整数纳秒（例如 1 Gbps 下 `tx_ns = length_bits`，
  任意整数比特数都满足；而 3 bps 下 `length_bits*1e9` 不能被 3 整除则被拒绝）。
- 无时钟漂移模型，只有固定偏差；到达时刻为非负整数，不支持过去时刻的负时间
  （偏差为负时内部允许负本地时刻，但参考时间仍非负）。
- 最多 4 队列、1000 帧、64 条原始窗口（见上文常量，可在模块顶部审计）。
