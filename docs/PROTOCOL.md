# 📡 Sims4Multiplayer 协议消息目录（v9.13）

> 借鉴: Kafka Schema Registry——集中管理消息 schema，强制兼容规则
> 传输: TCP + pickle 二进制帧（8 字节大端长度前缀 + pickle）
> 版本: PROTO_VERSION=2（1=JSON 时代, 2=pickle 帧）

## 帧格式

```
[8 字节大端长度 N][4 字节 CRC32][32 字节 HMAC-SHA256][N 字节 pickle 数据]
```

- 一包可含多帧（接收端 `while len(buf) >= 44` 循环拆帧）
- 帧上限 8MB（超限拒绝连接，防 DoS）
- v9.13: `_send_batch()` 可合并多条消息为一个 batch 帧
- v9.15: **CRC32 帧校验**（Ethernet FCS 标准）——坏帧丢弃不崩，防错解
- v9.16: **HMAC-SHA256 消息签名**（RFC 2104）——跨网防伪造/篡改
  - 握手 nonce 交换（HKDF RFC 5869 思路）→ 派生会话密钥
  - 接收端先验签（HMAC）再反序列化（Encrypt-then-MAC 顺序）
  - 未握手（hello/welcome）用 32 字节零签名占位，帧头恒 44 字节

## 消息优先级（QoS，v9.15）

借鉴 pvigier 多流分离思路，发送端分级：

| 优先级 | 值 | 消息 | 处理 |
|:-------|:---|:-----|:-----|
| 紧急 | 0 | hello/welcome/ready/travel_* | 直发，不批处理 |
| 普通 | 1 | chat/clock/money/stats/mood | 默认 |
| 低优 | 2 | sim_pos（位置）| 可 batch 合并 |



## 握手密钥交换（v9.16，跨网安全）

借鉴 HKDF（RFC 5869）思路——主密钥 → 会话密钥，双方不传输密钥：

```
1. client 生成 client_nonce（随机 16B hex）→ hello 携带
2. host 生成 host_nonce → welcome 携带
3. 双方独立派生: key = HMAC-SHA256(房间密码, client_nonce + host_nonce)
4. 之后所有帧携带 HMAC-SHA256(key, pickle数据) 签名
5. 接收端先验签再反序列化——无密钥的伪造/篡改帧被丢弃
```

- 相同输入 → 相同 key（无需传输密钥，防中间人伪造）
- 多客户端各自独立 key（`_hmac_keys[pid]`，互不通用）
- 房间无密码时 key 由 nonce 唯一决定（防重放）
## 消息总表（32 种）

### 房间/生命周期（14）
| 类型 | 方向 | 字段 | 用途 |
|:-----|:-----|:-----|:-----|
| `hello` | C→H | name, password, proto_version, client_nonce | 加入房间握手（v9.12 加版本；v9.16 加 client_nonce 密钥交换）|
| `welcome` | H→C | player_id, proto_version, host_nonce | 分配 ID + 协议确认（v9.16 加 host_nonce 密钥交换）|
| `lobby` | H→C | members, save_sync_phase, start_granted, room_code | 房间状态广播 |
| `ready` | C→H | ready | 准备状态 |
| `in_lot` | C→H | in_lot | 进图状态 |
| `heartbeat` | C→H | pid | 心跳保活 |
| `leave` | C→H | - | 离开房间 |
| `kicked` | H→C | reason | 被踢 |
| `join_rejected` | H→C | reason | 加入被拒（密码错）|
| `version_mismatch` | H→C | client_ver, host_ver | 协议版本不兼容（v9.12）|
| `start_game` | H→C | - | 开始游戏 |
| `game_start` | H→C | - | 游戏开始广播 |
| `members` / `members_ack` | H↔C | - | 成员同步确认 |
| `lobby_join` | C→H | - | 加入请求 |

### 旅行/场景切换（6）
| 类型 | 方向 | 字段 | 用途 |
|:-----|:-----|:-----|:-----|
| `travel_req` | H→C | ts | 发起旅行（双端确认）|
| `travel_ack` | C→H | ts | 确认旅行就绪 |
| `travel_go` | H→C | ts | 全员放行 |
| `travel_arrived` | C→H | player, zone_id | 抵达新场景报告（v9.11）|
| `travel_follow` | H→C | zone_id | 主机旅行跟随（v9.24）：主机 zone 变化 → 广播全员自动跟随 |
| `travel_all_arrived` | H→C | ts | 全员抵达确认（v9.11）|
| `travel_missing` | H→C | missing | 旅行超时提示（v9.11）|

> **v9.22 共同旅行增强**（S4MP 无此功能）：
> - 客机收到 `travel_req` 默认**自动确认**（`mp_travel_auto` 可关，启动器可切）
> - 进图时（in_lot False→True）且旅行锁定中**自动上报** `travel_arrived`
>   （原实现只能手动 `mp_travel_arrived`，实际对局没人输）
> - go 后 **30/60/90s 分级检查**：30/60s 广播进度（在等谁），90s 才超时解锁
> - 全员抵达后向所有客机**刷新 world_snapshot**（新场景位置/资金基准对齐）
> - `lobby` 广播与状态文件新增 `travel` 状态板（arrived/pending/auto_ack）
>
> **v9.23 旅行时钟锁定**：`travel_go` 时主机广播 `clock PAUSED` 并记住原速，
> 全员到齐 / 90s 超时后自动恢复原速——场景加载期间客机无法处理 tick，
> 锁时钟防止加载后时间漂移。
>
> **v9.23 周期状态自愈**：主机每 30s（`STATE_REFRESH_INTERVAL`）重播当前
> 时钟速度——丢失的 clock 更新由下一周期重播自愈（客机 apply 幂等）。
> 另：主线程每 tick 最多处理 `MAX_MSGS_PER_TICK=200` 条消息（防洪泛卡帧，
> 余量顺延下一 tick）；网络线程看门狗每 10s 检查线程存活，死亡自动重启。

### 同步数据（6）
| 类型 | 方向 | 字段 | 用途 |
|:-----|:-----|:-----|:-----|
| `sim_pos` | H↔C | sim_id, position \| delta \| delta_q, ts, seq | 位置同步（v9.12 量化）|
| `chat` | H↔C | from, text | 聊天 |
| `clock` | H→C | speed | 时间同步 |
| `mood` | H↔C | sim_id, mood, intensity | 心情同步 |
| `money_sync` | H↔C | funds | 金钱同步 |
| `stats_sync` | H↔C | stats | 需求/技能同步 |

### 连接健康（2）
| 类型 | 方向 | 字段 | 用途 |
|:-----|:-----|:-----|:-----|
| `ping` | H↔C | ts | 应用层 ping（v9.14 RTT 测量）|
| `pong` | H↔C | ts | ping 回包（带原时间戳 → RTT）|

### 存档传输（5）
| 类型 | 方向 | 字段 | 用途 |
|:-----|:-----|:-----|:-----|
| `save_sync_req` | H→C | - | 存档同步请求 |
| `save_sync_ack` | C→H | - | 确认同步 |
| `save_sync_done` | H→C | - | 同步完成 |
| `save_chunk` | H→C | filename, index, total, data, sha256 | 存档分块 |
| `save_chunk_done` | H→C | filename, total_bytes, sha256 | 传输完成 |
| `save_resend_req` | C→H | filename, missing | 缺块重传请求（v9.5）|

### 批处理（1）
| 类型 | 方向 | 字段 | 用途 |
|:-----|:-----|:-----|:-----|
| `batch` | H↔C | msgs | 多条消息合并帧（v9.13）|

**batch 防护（v9.21）**：
- 嵌套 `batch` 一律拒绝（防指数展开）
- `msgs` 必须是 list，条数上限 `MAX_BATCH_MSGS = 256`（超出截断）
- 展开时遵守入站队列上限，队列满则丢弃剩余

## 消息权限与流控（v9.21）

### 房主专属消息（HOST_ONLY_TYPES）
房主收到下列消息时，若 `sender_pid` 不是 0/None（即来自客机）则**直接丢弃**，
防恶意客机冒充房主踢人 / 强改时钟 / 伪造存档同步完成：

`welcome`、`kicked`、`start_game`、`clock`、`save_sync_req`、`save_chunk`、
`save_chunk_done`、`save_sync_done`、`travel_go`、`travel_all_arrived`、
`travel_missing`、`host_migrated`、`version_mismatch`、`join_rejected`、`world_snapshot`

> v9.22 修复：原列表中 `clock_sync` 是笔误（线上消息类型为 `clock`）——
> 白名单此前从未对时钟消息生效，恶意客机可改主机时间。

### 握手前帧上限（v9.22）
会话密钥派生**之前**的连接（未认证态）只接受 **≤4KB** 的帧（`_PRE_AUTH_MAX_FRAME`），
超大帧直接丢弃并记日志。说明：消息类型须反序列化后才能看到，类型白名单挡不住
pickle 反序列化本身；大小上限才能限制注入载荷规模。语义级防护由 HOST_ONLY
白名单在 `_process_incoming` 兜底。完整修复需协议 v3 将消息类型提升到帧头明文区。

### 会话密钥生命周期（v9.22 更新）
- host 的 `host_nonce` **按连接独立生成**（`_conn_nonces[pid]`）——原实现用模块级
  全局 `_my_nonce`，两客机并发握手时后一个连接覆盖前一个的 nonce，先完成握手的
  客机派生出错误 key → HMAC mismatch 静默丢帧
- 客机端验签修复：原实现查 `_hmac_keys.get(None)` 恒为空 → 客机对主机帧的
  HMAC 验证从未真正生效；现按本机 `player_id` 查 key
- `_hmac_keys[pid]` 与 `_conn_nonces[pid]` 在断开 / 踢旧接新时同步清理

### 流控上限
| 常量 | 值 | 作用 |
|:-----|:---|:-----|
| `MAX_FRAME_SIZE` | 8 MB | 单帧上限（v9.3，防超大帧内存 DoS）|
| `MAX_BATCH_MSGS` | 256 | 单个 batch 最多展开条数（v9.21）|
| `MAX_INCOMING_QUEUE` | 4096 | 入站队列上限，超限丢新消息并记日志（v9.21）|
| `MAX_RELEASED_PIDS` | 64 | player_id 复用池上限（v9.21）|

### 帧写入串行化（v9.21）
一帧由两次 `sendall`（44 字节帧头 + pickle 数据）写出，多线程并发发往同一
socket 会交错拼出撕裂帧（收端 CRC/HMAC 失败静默丢帧）。`_send_json` 按
socket 粒度加锁（`_get_send_lock`），连接结束时 `_drop_send_lock` 清理。

## 版本兼容规则

| 版本 | 变更 |
|:-----|:-----|
| 2 (当前) | pickle 帧 + proto_version + player_id 复用 + 位置量化 + batch |
| 1 | JSON 行协议（旧，不再兼容）|

**规则**：PROTO_VERSION 不匹配 → hello 被拒（version_mismatch），客户端提示更新。

> v9.21 的强化均为**同协议版本内的收端校验与流控**（不改帧格式、不改消息语义），
> 因此 `PROTO_VERSION` 保持 2，与 v9.20.x 客户端互通。

| `world_snapshot` | host→client | 登录全量快照（时间/位置/资金），新成员加入时立即对齐 |
