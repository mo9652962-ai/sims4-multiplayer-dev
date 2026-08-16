# 《SimSync 协议规范 v2.0》

> 版本：PROTO_VERSION = 2（pickle 帧时代）
> 对应代码：`src/multimod/network.py`（游戏内协议）、`room_protocol.py`（启动器房间协议）、`src/multimod/lobby.py`（消息处理）
> 文档基准：v9.21/v9.22 代码实态。本规范描述**代码实际实现的行为**，与旧 `PROTOCOL.md` 的差异处已标注。

---

## 1. 概述

SimSync 联机体系由**两层协议 + 一个发现机制**组成：

| 层 | 端口 | 传输 | 编码 | 运行环境 |
|:---|:-----|:-----|:-----|:---------|
| 启动器房间协议 | TCP 7660 | 行分隔 JSON（`\n` 结尾） | json | 启动器进程（launcher.py） |
| 游戏内同步协议 | TCP 7655 | 长度前缀二进制帧 | pickle + CRC32 + HMAC-SHA256 | 游戏进程（TS4 脚本 mod） |
| 局域网发现 | UDP 7661（启动器）/ 7656（游戏内） | UDP 广播 | json | 两者各自独立实现 |

架构角色：

- **Host（房主）**：唯一权威源（Host Authority）。运行房间服务 + 游戏内服务端，pid = 0。
- **Client（客机）**：连接房主，pid 由房主分配（≥1，可复用）。
- 游戏不运行专用服务器，房主机器同时承担服务器职能（listen server 模式）。

设计原则（摘自 ARCHITECTURE.md，仍然有效）：

1. 只同步事件和必要状态，不同步整个游戏内存。
2. Host 为唯一权威，客机只发请求/上报。
3. 事件驱动 + 阈值触发，非全量轮询。

---

## 2. 游戏内协议帧格式（TCP 7655）

### 2.1 帧布局

```
+----------------+----------------+------------------+-----------------------+
| 长度 N (8B 大端) | CRC32 (4B 大端) | HMAC-SHA256 (32B) | pickle 数据 (N 字节) |
+----------------+----------------+------------------+-----------------------+
<-------------------------- 44 字节固定帧头 -------------------------------->
```

- 总帧长 = 44 + N；接收端 `while len(buf) >= 44` 循环拆帧，一个 TCP 包可含多帧、一帧可跨多包。
- `N > MAX_FRAME_SIZE (8 MB)` → 直接关闭连接（防内存 DoS）。
- CRC32 = `binascii.crc32(pickle数据) & 0xFFFFFFFF`（Ethernet FCS 同款多项式）。
- HMAC = `HMAC-SHA256(会话密钥, pickle数据)`。握手前无密钥时填 **32 字节全零占位**（保证帧头恒 44 字节，收端固定偏移解析）。

### 2.2 接收端处理顺序（Encrypt-then-MAC）

```
收到完整帧
  ├─ 1. HMAC 验签（key = 本连接会话密钥；无 key 时跳过——握手阶段）
  │     失败 → 丢帧，记日志 "frame HMAC mismatch"，继续收下一帧
  ├─ 2. CRC32 校验
  │     失败 → 丢帧，记日志 "frame CRC mismatch"，继续收下一帧
  ├─ 3. pickle.loads 反序列化
  │     异常 → 丢帧，记日志 "unpickle error"
  ├─ 4. 未认证策略（会话密钥尚未派生时）
  │     帧 > 4KB 或 消息类型不在白名单 → 丢帧
  │     host 侧白名单：hello / heartbeat / ping / pong
  │     client 侧白名单：welcome / join_rejected / version_mismatch / kicked / lobby / ping / pong
  ├─ 5. 入站队列上限检查（MAX_INCOMING_QUEUE = 4096）
  │     超限 → 丢最新消息，记日志
  └─ 6. 入队 (msg, sender_pid)，等游戏主线程 alarm/on_tick（500ms）消费
```

> ⚠️ 已知限制（v2 无法根除）：第 3 步在验签通过**或无密钥**时都会执行 `pickle.loads`。
> 未认证阶段通过白名单把暴露面收窄到"≤4KB + 预期类型"，但 pickle 反序列化本身仍是
> 代码执行面。根治方案见 §9 协议 v3（消息类型提升到帧头明文区 + 认证前禁用 pickle）。

### 2.3 发送端约束

- 一帧由**两次 `sendall`**（44B 帧头 + N 字节数据）写出，必须整帧串行化：
  按 socket 粒度加锁（`_send_locks[id(sock)]`），防多线程并发产生撕裂帧。
  连接断开时 `_drop_send_lock` 清理锁条目防泄漏。
- TCP_NODELAY 已启用（禁用 Nagle，低延迟优先）。
- SO_KEEPALIVE 已启用（Windows SIO_KEEPALIVE_VALS：10s 起探，3s 间隔）。

### 2.4 QoS 优先级——**文档与实现的偏差声明**

`_send_json(sock, payload, prio)` 签名接受 prio（0=紧急直发 / 1=普通 / 2=低优可批处理），
**但函数体当前完全不使用该参数**；`_send_batch()`（batch 合并发送）已定义但**无任何调用方**。
即：接收端有完整的 batch 拆包与防护逻辑，发送端从不打包。优先级/批处理在 v2 中
是**未生效的预留设计**，生效计划见路线图 v9.8x。收发双方对 prio 值无兼容性依赖。

---

## 3. 安全层：握手与密钥交换

### 3.1 密钥派生（HKDF 思路，RFC 5869）

```
client 生成 client_nonce = os.urandom(16).hex()   （32 字符 hex）
host    生成 host_nonce    = os.urandom(16).hex()   （每连接独立，存 _conn_nonces[pid]）

会话密钥 key = HMAC-SHA256(
    key     = room_password 的 UTF-8 字节（无密码则为空字节串）,
    message = client_nonce || host_nonce
)

双方各自独立派生，密钥本身永不在线路传输。
此后该连接所有帧携带 HMAC-SHA256(key, pickle数据) 签名。
```

多客机各自独立 key：host 侧按目标 pid 反查 `_hmac_keys[pid]` 签名/验签；
client 侧按本机 `_my_player_id` 查 key。

> ⚠️ **已知缺陷 KNOWN-S1**：client 侧派生时使用的是本模块全局 `lobby.ROOM_PASSWORD`，
> 该全局在客机上恒为空串——私密房（密码非空）时双方派生出不同 key，握手后所有帧互丢。
> 完整分析见《已知问题与改进清单》S-1。规范层面正确的派生输入应是
> **client 握手时实际使用的密码**。

### 3.2 密钥生命周期

| 事件 | 动作 |
|:-----|:-----|
| host 收到合法 hello | `_hmac_keys[pid] = key`（版本、密码校验通过后） |
| client 收到 welcome | `_hmac_keys[my_pid] = key` |
| 连接断开（recv 线程收尾） | `_hmac_keys.pop(pid)`、`_conn_nonces.pop(pid)`、`_sock_pid_index.pop(id(sock))` |
| 同 IP 踢旧接新 | 同上（旧 pid 全套清理） |
| host 迁移（`_become_new_host`） | **当前实现不清理**——已知缺陷 S-3 |

---

## 4. 消息目录（游戏内协议，全量 41 种）

约定：方向 H→C 表示房主发给客机（实际为广播或定向）；H↔C 双向。
"处理"列为接收端实际分发目标。标注 ⚠️dead 的消息：分发表中存在但无 handler，静默丢弃。
标注 ⚠️route 的消息：在 `network._process_incoming` 中被直接路由，不进入 `lobby.process_message`。

### 4.1 房间/生命周期（15）

| type | 方向 | 字段 | prio | 处理 | 引入 |
|:-----|:-----|:-----|:-----|:-----|:-----|
| `hello` | C→H | `name:str`（≤16）、`password:str`、`proto_version:int`、`client_nonce:str(hex)` | 0 | lobby：版本→密码→入成员→派生 key | v9.12/v9.16 |
| `welcome` | H→C | `player_id:int`、`proto_version:int`、`host_nonce:str(hex)` | 0 | lobby：记 pid、派生 key、起心跳 | v9.16 |
| `lobby` | H→C | `members:[{player_id,name,ready,in_lot,online,last_seen,is_host,ip}]`、`save_sync_phase:str`、`start_granted:bool`、`room_code:str`、`room_visibility:str` | 1 | lobby：重建本地成员表 | v9.0 |
| `ready` | C→H | `ready:bool` | 0 | lobby：更新成员准备态 | M3d |
| `in_lot` | C→H | `in_lot:bool` | 1 | lobby：进图状态上报 | M3d |
| `heartbeat` | C→H | （无字段，收到即刷新 last_seen） | 1 | lobby：on_heartbeat | M3d |
| `leave` | C→H | — | 1 | lobby：on_leave | M3d |
| `kicked` | H→C | `reason:str` | 1 | lobby：清本地房间态 | M3d |
| `join_rejected` | H→C | `reason:str` | 1 | lobby：通知 | v9.16 |
| `version_mismatch` | H→C | `client_ver:int`、`host_ver:int` | 0 | lobby：通知更新 | v9.12 |
| `start_game` | H→C | — | 1 | lobby：客机进入开局流程 | M3d |
| `game_start` | H→C | — | — | ⚠️dead（无 handler） | — |
| `members` | H→C | — | — | ⚠️dead（无 handler，成员同步实际走 lobby 消息） | — |
| `members_ack` | C→H | — | — | ⚠️dead | — |
| `lobby_join` | C→H | — | — | ⚠️dead | — |

> 注：`welcome` 实际会发送**两次**——TCP 建连时 network 层先发一份（含 host_nonce），
> hello 校验通过后 lobby.on_hello 再发一份。客机两次都处理（第二次重复派生 key + 
> 重复启动心跳线程，见已知问题 M-1）。

### 4.2 旅行/场景切换（6）

| type | 方向 | 字段 | prio | 处理 | 说明 |
|:-----|:-----|:-----|:-----|:-----|:-----|
| `travel_req` | H→C | `ts:float` | 1 | ⚠️route→lobby.on_travel_req | 房主发起；客机默认自动回 ack |
| `travel_ack` | C→H | `ts:float` | 0 | ⚠️route→lobby.on_travel_ack | 客机确认就绪 |
| `travel_go` | H→C | `ts:float` | 1 | ⚠️route→lobby.on_travel_go | 全员放行，进入旅行锁定 |
| `travel_arrived` | C→H | `player:int`、`zone_id` | 1 | ⚠️route→lobby.on_travel_arrived | 进图后自动上报（v9.22） |
| `travel_all_arrived` | H→C | `ts:float` | 1 | lobby：解除锁定 + 刷新 world_snapshot | v9.11 |
| `travel_missing` | H→C | `missing:[pid]` | 1 | lobby：超时解锁提示 | v9.11 |

旅行定时策略（host 侧）：发起后 10s 强制放行；go 后 30/60s 广播进度、90s 超时解锁。

### 4.3 同步数据（11）

| type | 方向 | 字段 | 说明 |
|:-----|:-----|:-----|:-----|
| `sim_pos` | H↔C | `sim_id:int`、`position:[x,y,z]`（绝对，首包/重置基准）**或** `delta_q:[dx,dy,dz]`（量化增量，×100 取整 0.01m 精度）、`ts:float`、`seq:int` | 位置同步；变化 > POS_THRESHOLD 才发；prio=2（未生效，见 §2.4） |
| `chat` | H↔C | `from:str`、`text:str` | 游戏内聊天；接收端弹通知 + 写 lobby state |
| `clock` | H→C | `speed:int`（0=暂停 1/2/3=倍速） | HOST_ONLY；客机 hook 本地时钟 |
| `mood` | H↔C | `sim_id:int`、`mood:str`、`intensity:float` | 心情；⚠️房主侧实际不广播（KNOWN-N8） |
| `money_sync` | H↔C | `funds:int`、`ts:float` | 家庭资金；变化 ≥1 才发 |
| `stats_sync` | H↔C | `stats:{名字:值}`、`ts:float` | 需求/技能；变化 >5 才发 |
| `interaction` | H↔C | `sim_id:int`、`affordance:int(guid)`、`target_id:int`、`ts:float` | 交互队列；接收端 push_super_affordance，3s 回环抑制 |
| `inventory` | H↔C | `sim_id:int`、`items:[...]`、`ts:float` | 背包 |
| `relationship` | H↔C | `sim_a:int`、`sim_b:int`、`score:float`、`bits:[...]`、`ts:float` | 关系 |
| `object_pos` | H↔C | `obj_id:int`、`x,y,z:float`、`rot:float`、`ts:float` | Buy 家具（移动阈值 ~2°/格） |
| `world_snapshot` | H→C | `ts`、`clock_speed:int?`、`positions:{...}?`、`funds:int?` | HOST_ONLY；新成员/旅行完成后的全量对齐 |

### 4.4 连接健康（2）

| type | 方向 | 字段 | 说明 |
|:-----|:-----|:-----|:-----|
| `ping` | H↔C | `ts:float` | 每 3s 主动发；对端原样回 pong |
| `pong` | H↔C | `ts:float` | 收到后算 RTT（0<rtt<5000ms 有效），滚动窗口 20 样本 → 健康评分 0-100 |

### 4.5 存档传输（6）

| type | 方向 | 字段 | 说明 |
|:-----|:-----|:-----|:-----|
| `save_sync_req` | H→C | — | HOST_ONLY；要求客机准备接收 |
| `save_sync_ack` | C→H | `ok:bool`、`filename?:str` | 客机确认（写盘/校验后） |
| `save_sync_done` | H→C | — | HOST_ONLY；阶段完成 |
| `save_chunk` | H→C | `filename:str`、`index:int`、`total:int`、`data:base64`、`sha256?:str` | HOST_ONLY；64KB 分块 |
| `save_chunk_done` | H→C | `filename:str`、`total_bytes:int`、`sha256:str` | HOST_ONLY；收尾，触发客机拼装+校验+写盘 |
| `save_resend_req` | C→H | `filename:str`、`missing:[index]`（≤20）、`total:int` | 缺块重传（⚠️死局缺陷 H-1） |

### 4.6 批处理（1）

| type | 方向 | 字段 | 说明 |
|:-----|:-----|:-----|:-----|
| `batch` | H↔C | `msgs:[dict]` | 接收端防护：拒绝嵌套 batch、条数上限 256（超出截断）、展开遵守队列上限 4096 |

### 4.7 消息权限（HOST_ONLY_TYPES）

以下类型在房主侧收到时，若 `sender_pid ∉ {None, 0}` **直接丢弃**（防客机冒充房主）：

```
welcome, kicked, start_game, clock, save_sync_req, save_chunk,
save_chunk_done, save_sync_done, travel_go, travel_all_arrived,
travel_missing, host_migrated, version_mismatch, join_rejected, world_snapshot
```

> ⚠️ 遗漏（KNOWN-N3）：`lobby`、`travel_req`、`save_resend_req` 不在名单内，
> 恶意客机可下发 `lobby` 毒化主机成员表 / 提前解锁旅行。修复见已知问题清单 S-5/M-12。
> 名单中的 `host_migrated` 当前无任何发送方（预留）。

---

## 5. 启动器房间协议（TCP 7660，JSON 行）

帧 = 一行 UTF-8 JSON + `\n`。单行上限 8MB。用于**游戏启动前**的房间阶段。

### 5.1 消息目录（12）

| type | 方向 | 字段 | 说明 |
|:-----|:-----|:-----|:-----|
| `join` | C→H | `name`、`room_code`、`password?`、`ip`（自报） | 请求加入 |
| `joined` | H→C | `player_id`、`members:[...]`、`room_code`、`state` | 接纳 + 全量状态 |
| `join_rejected` | H→C | `reason` | 房间码/密码错误 |
| `ready` | C→H | `ready:bool` | 准备状态 |
| `members` | H→ALL | `members:[...]`、`state` | 成员/状态广播 |
| `save_sync_start` | H→C | `filename`、`size`、`total`、`sha256` | 存档同步开始 |
| `save_chunk` | H→C | `index`、`total`、`data:base64` | 64KB 分块 |
| `save_sync_done` | H→ALL | `sha256`、`filename` | 传输完成 |
| `save_sync_ack` | C→H | — | ⚠️定义了但客户端从不发送（死消息） |
| `start_game` | H→ALL | `host_ip`、`game_port`、`save_name` | 拉起游戏 |
| `leave` | C→H | — | 离开 |
| `left` | H→C | — | 确认离开 |

房间状态机：`waiting → ready（全员 ready）→ syncing → synced → launching`。

### 5.2 UDP 发现（7661）

- 客户端广播魔数 `SIMSYNC_DISCOVER` → 房主回 `{name, room_code, players, has_password}`。
- 游戏内 mod 另有独立发现（UDP 7656，魔数 `S4S_DISC`，房主每 5s 广播）。

### 5.3 启动器 ↔ 游戏 mod 文件桥

| 文件 | 方向 | 内容 |
|:-----|:-----|:-----|
| `Mods/mp_launcher_config.json` | 启动器→mod | mode(host/join)、host、port、name、password、visibility、auto_sync |
| `Mods/mp_cmd.json` | 启动器→mod | `{ts, cmd}`：mp_ready/mp_unready/mp_syncsave/mp_start/mp_leave/mp_travel/mp_travel_auto（按 ts 去重，执行后删除文件） |
| `Mods/mp_lobby_state.json` | mod→启动器 | 成员/准备/进图/save_sync_phase/start_granted/chat/travel/room_code（原子写：tmp+replace） |
| `Mods/Sims4Multiplayer/chat_cmd.txt` | 启动器→mod | 聊天注入（mp_poll 消费） |
| `Mods/mp_debug.log` | mod→启动器 | 调试日志 |

---

## 6. 时序图（文字版）

### 6.1 加入握手（游戏内协议）

```
Client                                   Host
  | ---- TCP connect (7655) ------------> |
  |                                       | 分配 pid（优先复用 _released_pids）
  |                                       | 生成 host_nonce → _conn_nonces[pid]
  | <--- welcome #1 --------------------- |  {player_id, proto_version, host_nonce}
  | 生成 _my_nonce                        |
  | --- hello --------------------------> |  {name, password, proto_version, client_nonce}
  |                                       | ① proto_version != 2 ?
  |                                       |   是 → version_mismatch，return
  |                                       | ② private 且 password 不符 ?
  |                                       |   是 → join_rejected，return
  |                                       | ③ 入成员表（pid, name, ready=False）
  |                                       | ④ key = HMAC-SHA256(password, c_nonce+h_nonce)
  |                                       |   → _hmac_keys[pid]
  | <--- welcome #2 --------------------- |  （重复一份，见 M-1）
  | <--- lobby -------------------------- |  全量成员状态广播（发给所有人）
  | <--- world_snapshot ----------------- |  clock_speed + positions + funds
  | key = HMAC(password, my_nonce+host_nonce) → _hmac_keys[my_pid]
  | start_heartbeat()（5s 间隔线程）       |
  | ======= 此后所有帧双向 HMAC 签名 ======= |
```

### 6.2 大厅 → 存档同步 → 开始（游戏内协议）

```
Client                                   Host
  | --- ready {ready:true} -------------> | 更新成员表 → 广播 lobby
  | ......（全员 ready）......             |
  | <--- save_sync_req ------------------ | phase: idle → waiting_ack
  | --- save_sync_ack {ok:true} --------> | 收齐 ack
  | <--- save_chunk ×N ------------------ | 64KB/块，filename+index+total+data
  | <--- save_chunk_done {sha256} ------- | 客机拼装 → SHA256 校验 → .bak 备份 → 写盘
  | --- save_sync_ack {ok,filename} ----> |（⚠️ host 发送完成即置 done，不等此 ack——H-2）
  | <--- save_sync_done ---------------- | phase: done, start_granted=True
  | <--- lobby {start_granted:true} ----- |
  | <--- start_game --------------------- | 双端进入游戏
```

### 6.3 位置同步（delta 量化）

```
发送端（本机 sim 移动时，0.2~0.5s 自适应间隔检查）
  | pos 变化 > POS_THRESHOLD ?
  |   否 → 不发
  |   是 → seq += 1
  |         首包/基准重置: {sim_id, position:[x,y,z], ts, seq}
  |         其余:          {sim_id, delta_q:[Δx×100取整,...], ts, seq}
  v
接收端
  | position → 重置基准
  | delta_q  → 基准 += delta_q/100 累加，插值平滑上屏
  | （⚠️ 量化误差累加无周期性绝对值重同步——KNOWN-N9）
```

### 6.4 共同旅行（v9.22）

```
Host                                     Clients
  | mp_travel / 启动器"共同旅行"
  | _travel_pending=True, acks={0}
  | --- travel_req {ts} ---------------> |
  |                                     | auto_ack 开(默认): 立即回 ack
  |                                     | auto_ack 关: 等玩家 mp_travel_ack
  | <-- travel_ack --------------------- |（全员到齐 或 10s 超时）
  | _travel_force_start():
  |   pending=False, active=True（锁定位置同步）
  |   arrived={0}, 全员 in_lot=False
  | --- travel_go {ts} ----------------> | active=True
  | [30s] 广播进度（在等谁）             |
  | [60s] 广播进度                      |
  | <-- travel_arrived {player,zone_id} | 进图检测(in_lot False→True)自动上报
  | arrived ⊇ members ?                 |
  |   是 → active=False                 |
  | --- travel_all_arrived ------------> | 解锁 + 重发 world_snapshot 对齐新场景
  |   否 → [90s] active=False           |
  | --- travel_missing {missing:[..]} -> | 超时解锁
```

### 6.5 心跳与断线重连

```
Client                          Host
| -- heartbeat (每5s) --------> | 刷新 last_seen
|                               | check_heartbeats（每5s）:
|                               |   now-last_seen>15s → 标记 offline + 广播
| x----- 连接断开 -----x        |
| recv 线程收尾:                | recv 线程收尾: 清 _clients/_hmac_keys/
|   _client_socket=None         |   _conn_nonces/_sock_pid_index，pid 入复用池
|   schedule_reconnect_with_backoff()
|     退避: 1.5s 起 ×1.5，上限 60s，最多 20 次
| -- reconnect → hello -------> |（⚠️ 重连 hello 缺 client_nonce/密码——S-2）
```

### 6.6 主机迁移（当前实现，存在已知缺陷）

```
客机们检测到 host 掉线（on_host_disconnect）
  → 各自写 claim 文件 host_migration_{room_code}.claim
  → 等待 2-5s（随机 settle）后读取所有 claim
  → candidates = 本地视图的在线非房主成员
  → 赢家 = min(player_id)
  → 赢家: _become_new_host() → 开 7655 服务端、重建房间、广播
  → 非赢家: _schedule_reconnect() 重连
  ⚠️ S-3/S-4/M-2：迁移后网络残留不清理、重连目标仍是旧主机 IP、可能脑裂
```

### 6.7 存档缺块重传（当前为死局）

```
Client                              Host
| 收齐 total 块前 save_chunk_done 到达？
|   计算缺失 missing[]
|   retries += 1（<3 时）
| --- save_resend_req {missing[:20]} --> |
|                                     | _resend_save_chunks: 重发缺失块
| <-- save_chunk ×len(missing) -------- |
|（⚠️ 重传后再无收尾触发——补齐后没人调拼装/写盘，流程死局 H-1）
```

---

## 7. 错误处理

### 7.1 帧层

| 错误 | 检测点 | 行为 |
|:-----|:-------|:-----|
| 超大帧（N > 8MB） | recv 循环 | 关闭连接（不解析） |
| HMAC 不匹配 | 验签 | 丢帧不崩，记日志，继续 |
| CRC32 不匹配 | 校验 | 丢帧不崩，记日志，继续 |
| pickle 反序列化异常 | loads | 丢帧，记 "unpickle error" |
| 未认证帧超限/类型不符 | pre-auth 策略 | 丢帧，记 "pre-auth frame dropped" |
| 撕裂帧（并发写交错） | —— | 发送端 per-socket 锁预防；万一发生表现为 CRC/HMAC 失败 |
| 队列满（>4096） | 入队前 | 丢**新**消息保旧消息，记日志 |
| batch 炸弹 | 拆包时 | 嵌套拒绝 / >256 截断 / 展开遵守队列上限 |

### 7.2 握手/应用层

| 错误 | 消息 | 后续 |
|:-----|:-----|:-----|
| 协议版本不匹配 | `version_mismatch {client_ver, host_ver}` | 客机提示更新（连接保持——已知缺陷 S-8 之一） |
| 密码错误 | `join_rejected {reason}` | 客机提示（连接保持，无失败计数——S-8） |
| 未知消息类型 | — | 记 "unknown message"，丢弃不崩 |
| handler 异常 | — | 每条消息独立 try/except，单条失败不影响后续 |
| 心跳超时 | — | 仅标记 offline + 广播（不移除、不断连） |
| 旅行超时 | `travel_missing` | 90s 解锁，流程可继续 |

### 7.3 错误处理设计原则（从代码归纳）

1. **坏帧跳过不崩溃**：接收循环任何单帧错误都不退出循环。
2. **处理隔离**：主线程消费时每条消息独立异常边界。
3. **静默降级**：大量错误只写日志（`mp_debug.log`），不打断游戏。
4. 已知代价：部分关键失败（HMAC mismatch、存档校验失败）**过于**静默，
   用户只看到"不同步"，排障依赖日志文件。

---

## 8. 版本兼容策略

### 8.1 当前规则（v2）

- `PROTO_VERSION = 2`。hello 携带客户端版本，host 不等即拒：回 `version_mismatch`，**不兼容不互通**。
- v1（JSON 行协议）已废弃，无迁移路径。
- **同版本内的收端强化原则**（v9.15~v9.21 实践）：CRC/HMAC/流控/白名单等均为
  接收端校验，不改帧格式与消息语义 → `PROTO_VERSION` 保持不变，新旧客户端互通。
  老客户端发来的帧没有 HMAC（v9.16 前）时：新收端因无该 pid 的 key 而放行——
  即安全增强是"软升级"，不破坏互通。

### 8.2 兼容性矩阵

| 收\发 | v9.16+（HMAC） | v9.15（CRC） | v9.12-（裸帧） |
|:------|:---------------|:-------------|:---------------|
| v9.16+ host | 完整 | 放行（无 key） | 放行（无 key） |
| v9.15 收 v9.16+ 帧 | 忽略 HMAC 字段（按旧 12B 头解析会错位——实际不互通） | — | — |

> 严格说 v9.16 的帧头从 12B 扩到 44B 是**硬变更**：v9.15 及更早收端会错位解析。
> 因此 v9.16 起的事实兼容下限是 v9.16（双方都认 44B 头）。
> 建议 v3 起把帧头长度变更视为必须 bump PROTO_VERSION 的硬变更。

### 8.3 协议 v3 展望（v10.0，见路线图）

1. 消息类型提升到帧头明文区：`[版本1B][类型2B][长度4B][CRC4B][HMAC32B][payload]`，
   未认证阶段完全不反序列化 payload。
2. 认证前 payload 仅允许 JSON 子集（禁 pickle）。
3. 帧头变更 → `PROTO_VERSION = 3`，v2 客户端收到明文版本号即可给出明确升级提示。
4. 向后协商：v3 host 检测首字节非 0x03 帧头时回退 v2 解析（过渡期一个端口双协议）。

---

## 9. 附录

### 9.1 常量表

| 常量 | 值 | 位置 |
|:-----|:---|:-----|
| DEFAULT_PORT（游戏） | 7655 | network.py |
| ROOM_PORT（启动器房间） | 7660 | room_protocol.py |
| DISCOVERY_PORT（启动器/游戏） | 7661 / 7656 | room_protocol.py / network.py |
| PROTO_VERSION | 2 | network.py |
| MAX_FRAME_SIZE | 8 MB | network.py |
| MAX_BATCH_MSGS | 256 | network.py |
| MAX_INCOMING_QUEUE | 4096 | network.py |
| MAX_RELEASED_PIDS | 64 | network.py |
| _PRE_AUTH_MAX_FRAME | 4 KB | network.py |
| SAVE_CHUNK_SIZE | 64 KB | room_protocol.py / lobby.py |
| HEARTBEAT_INTERVAL / TIMEOUT | 5s / 15s | lobby.py |
| PING_INTERVAL | 3s | network.py |
| 消息消费频率（alarm/on_tick） | 500ms | network.py |
| 旅行定时 | 10s 强制 / 30/60/90s 检查 | lobby.py |
| 重连退避 | 1.5s×1.5 → 60s，20 次 | lobby.py |
| RTT 滚动窗口 | 20 样本 | network.py |

### 9.2 player_id 规则

- 0 = 房主；客机从 1 递增。
- 断开时 pid 进入 `_released_pids` 复用池（上限 64，FIFO 淘汰最旧），
  新连接优先取池内 pid —— 保证重连者身份（与 `_travel_arrived`、`_save_sync_acks` 等
  按 pid 记账的状态）尽量延续。
- 同 IP 新连接触发"踢旧接新"：旧 pid 全套状态（clients/hmac/nonce/索引）清理后复用。

### 9.3 状态机汇总

```
房间（lobby）:   idle → waiting_ack → done          （save_sync_phase）
成员（member）:  offline/online × ready/in_lot
旅行（travel）:  空闲 → pending(等ack) → active(锁定) → 完成/超时解锁
启动器房间:      waiting → ready → syncing → synced → launching
连接（client）:  connecting → connected → disconnected → backoff-reconnect(×20)
```
