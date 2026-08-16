# 《联机功能路线图》v9.21 → v10.0

> 基准：当前 v9.21（代码中 v9.22 强化已在途：旅行自动确认、连接级 nonce、握手前帧策略）。
> 配套数据结构：`docs/v10_data_structures.py`（所有新增结构的权威定义）。
> 前置依赖：《已知问题与改进清单》——v9.30 修复波必须先行，新功能不允许叠在带伤的地基上。

---

## 0. 总览

| 里程碑 | 主题 | 核心交付 | 预估 |
|:-------|:-----|:---------|:-----|
| **v9.30** | 修复波 + 真机验证 | 清单 P0/P1 全修，双机真机回归 | 2 周 |
| **v9.40** | 房间系统 2.0 | 房间大厅列表、席位保留、观战、权限分级 | 3 周 |
| **v9.50** | Mod 同步 | manifest 指纹对比、缺省 mod 分发、不匹配拦截 | 3 周 |
| **v9.60** | 语音提示 | 事件语音播报（TTS）+ 按住说话（PTT 语音聊天） | 2 周 |
| **v9.70** | 反作弊 | 服务器侧校验（移动/金钱/速率/重放）、信任分 | 3 周 |
| **v9.80** | 网络与性能 | QoS 真实现、压缩、增量快照、重连修复 | 2 周 |
| **v9.90** | 协议 v3 | 帧头明文类型、认证前禁 pickle、跨网（UPnP/STUN 打磨） | 3 周 |
| **v10.0** | 正式版 | 全量回归、文档定稿、自动更新 | 2 周 |

排序原则：修复 → 房间（多人体验骨架）→ mod 同步（一致性前提）→ 语音（体验）→
反作弊（对外服务前必须有）→ 性能 → 协议演进。反作弊放在 mod 同步之后，
因为校验规则依赖"双方 mod 集合一致"这一前提。

---

## 1. v9.30 —— 修复波 + 真机验证（前置里程碑）

**目标**：清掉《已知问题与改进清单》全部 P0（S-1~S-8、启动器方法覆盖）与 P1，
使"私密房、断线重连、主机迁移、存档同步"四条链路真正可用。

**技术方案**：见 KNOWN_ISSUES.md 逐项修复代码，此处不重复。要点：

1. HMAC 派生对称化（S-1/S-2）——协议层正确性，阻塞所有后续安全特性。
2. 启动器 5 处方法覆盖合并（KNOWN_ISSUES A-1~A-5）——启动器功能目前形同虚设。
3. 文件名白名单（S-6/S-7）——mod 同步要复用同一套分块传输，先堵路径穿越。
4. `_members` 加锁 + Timer 代次（H-3/H-4/H-9）——房间系统的状态机地基。

**验收**：
- 私密房（带密码）双机：位置/金钱/聊天全通，`mp_debug.log` 无 HMAC mismatch。
- 拔线 30s 内自动重连恢复同步；杀房主进程后迁移成功且客机连上**新**主机 IP。
- 启动器点"准备/同步存档/开始游戏/离开/聊天"在 mod 大厅模式下全部生效，不双开游戏。

---

## 2. v9.40 —— 房间系统 2.0

### 2.1 功能清单

| 功能 | 描述 |
|:-----|:-----|
| 房间大厅列表 | 局域网/中继服务器上的公开房间列表（名称/人数/密码/mod 状态），替代"手输 IP" |
| 席位保留 | 客机断线后保留席位与 pid 一段时长（如 120s），重连恢复 ready/进度，不重走存档同步 |
| 观战模式 | 加入者可选择 spectator：不参与同步门槛、不推送操作、只收状态 |
| 权限分级 | 房主可授予成员副房主（可踢人/可发起旅行），权限随 lobby 广播 |
| 房间设置持久化 | 房主崩溃重启后按 room_code 恢复房间（成员按席位保留自动回归） |

### 2.2 技术方案

**消息扩展**（游戏内协议，均走既有 pickle 帧通道）：

```
room_list_req   C→大厅服务   {}                    请求房间列表
room_list       大厅服务→C   {rooms:[RoomListEntry]}
spectator_join  C→H         {spectator:bool}       观战声明（hello 扩展字段亦可）
perm_grant      H→ALL       {player_id, role}      权限变更广播
seat_reserved   H→C         {player_id, ttl}       席位保留通知（断线时单发）
```

**大厅列表**：v1 复用现有 UDP 发现（7656/7661）聚合为列表——启动器每 3s 扫描，
`get_discovered_rooms()` 已有 20s TTL 过滤，补 `max_players/has_password/mods_hash`
三个字段即可；v2（v10.0 前后）增加可选集中式目录服务（一台廉价 VPS 跑
HTTP `GET /rooms`，房主心跳注册，纯 JSON，不参与游戏数据转发）。

**席位保留**：改造 `check_heartbeats`——超时不只标记 offline，同时写
`_reserved_seats[pid] = {"expire_ts": now+120, "member_snapshot": m}`；
`on_hello` 时若 `room_code + 玩家名` 命中保留席位则**沿用原 pid、恢复 ready/in_lot**
（同时修复 M-9 的 hello 不幂等问题）。保留期内心跳门槛判定（all_clients_ready 等）
跳过该席位。到期未归 → 移除并入 pid 复用池。

**观战模式**：hello 加 `role: "player"|"spectator"`。host 的 `_broadcast` 已支持
exclude，扩展为按 tags/role 过滤；同步模块发送前查 `lobby.is_spectator(pid)`。
观战者不发送任何同步消息（HOST_ONLY 校验之外再加一层 CLIENT_SEND 白名单）。

**权限分级**：`RoomMember.role: int`（0=房主 1=副房主 2=成员 3=观战）。
`kick_member`/`host_start_travel` 的权限判定改为 `role <= 1`；
`promote_member` 修复为真实迁移（当前只改标记，见 H-5）。

**房间恢复**：房主侧把 `RoomSettings` 周期性写入 `mp_room_persist.json`
（原子写，复用 `_write_state_file` 的 tmp+replace 模式）；重启建房时若
room_code 一致则恢复设置，成员靠席位保留机制回归。

### 2.3 验收

- 扫描页显示 ≥2 个并发房间，含人数/密码标记；选择加入全程无需输 IP。
- 客机拔线 60s 内重连：不重走存档同步、ready 状态保留、pid 不变。
- 观战者加入不阻塞"全员准备"门槛，且其本地无法影响世界状态。
- 副房主可踢人；被踢者连接被关闭且 60s 内同 IP 不得重新加入。

---

## 3. v9.50 —— Mod 同步

### 3.1 功能

1. 房主开房时扫描 Mods 目录 → 生成 manifest（文件名 + 大小 + SHA256 + 加载序）。
2. 客机加入时对比本地 manifest → 差异报告（缺哪些 / 多哪些 / 版本不同）。
3. 房主可一键分发缺失 mod（复用存档分块通道）；script mod 需重启游戏生效的明确提示。
4. **不匹配拦截**：非观战成员 mod 集不一致时禁止开始游戏（可由房主强制放行并自担风险）。

### 3.2 技术方案

**Manifest 生成**（房主侧，后台线程，避免阻塞）：

```
Mods/ 下所有 *.package / *.ts4script / Scripts/*.py：
    entry = (rel_path, size, sha256_1MB_prefix_or_full, load_order_hint)
Manifest = {room_code, game_version, entries[], manifest_hash}
manifest_hash = sha256(sorted(entries 的 "path:sha" 拼接))
```
大文件用前 1MB + 尾 1MB 采样哈希（`_hash_sample`），全量哈希仅 <5MB 文件——
扫描 10GB mods 目录控制在 30s 内。

**消息扩展**：

```
mods_manifest_req   C→H    {}                       加入时请求
mods_manifest       H→C    {Manifest}               指纹下发
mods_mismatch       C→H    {missing:[], extra:[], different:[]}  
mods_chunk_req      C→H    {rel_path}               请求分发缺失 mod
mods_chunk          H→C    {rel_path, index, total, data, sha256}
mods_chunk_done     H→C    {rel_path, sha256}
mods_sync_done      H→ALL  {manifest_hash}          全员一致确认
```

**分发通道**：完全复用存档分块的发送/接收/校验/`.bak` 备份骨架，但落盘目录改为
`Mods/SimSync_Stage/`（暂存区）——**不直接写 Mods 根目录**，用户在启动器确认后
由启动器进程（有文件占用处理能力）移动到 Mods 并提示重启。这样规避游戏运行中
覆盖正在使用的 package 文件导致的崩溃。

**拦截点**：`host_start_game` 增加 `mods_consistent` 检查——所有非观战成员
上报过 `mods_mismatch` 为空（或房主显式 `force=1`）。

**安全**：`rel_path` 走 S-6/S-7 修复引入的文件名白名单（basename + 后缀白名单
+ realpath 限定暂存目录），分发内容 sha256 校验后落盘。

### 3.3 验收

- 房主装 3 个 mod、客机缺 1 个：加入后 10s 内列出精确差异；分发后哈希一致。
- 差异未解决时"开始游戏"按钮置灰；强制放行走二次确认。
- 10GB mods 目录扫描 <30s 且游戏帧率无可感知下降（后台线程 + 采样哈希）。

---

## 4. v9.60 —— 语音提示

### 4.1 功能

1. **事件语音播报**（v1，纯本地）：成员加入/离开、被踢、旅行开始/抵达、存档同步
   完成、连接质量劣化——本地 TTS 播报，零网络开销。
2. **按住说话 PTT**（v2）：游戏内语音聊天，对讲机模式（按住 V 键讲话），
   UDP 传输、Opus 编码、环形缓冲 jitter。
3. **语音留言**（v3，v10 前后）：离线成员上线后可听离线期间的语音留言（文件化，
   走 mods 分发同款分块通道）。

### 4.2 技术方案

**事件播报**：Windows SAPI（`pywin32` 不可用于 ts4script 环境 → 用
`windll.user32` 不可行，改为**子进程方案**：mod 侧把事件写入
`mp_voice_events.jsonl`（追加式），由**启动器常驻托盘进程**消费并调
PowerShell `System.Speech` TTS 播报。零侵入游戏进程，避开游戏内音频 API 风险。
规则表 `VoicePromptRule`（事件→文案模板→优先级→冷却时间）可配置。

**PTT 语音聊天**：

```
音频链路（游戏进程外置！语音跑在启动器托盘进程，mod 只传按键事件）:
  游戏内 mod: 捕获 V 键（interaction hook 或 alarm 轮询键盘状态）
      → mp_voice_ctrl.json {talking:bool}
  启动器托盘: 读控制文件 → sounddevice 采集 48kHz→Opus 24kbps VBR
      → UDP 7662 发往房主 → 房主转发给其他成员（混流前转发，各端本地解码播放）
  接收端: jitter buffer 120ms → Opus 解码 → sounddevice 播放
```

设计决策：**语音数据完全不经过游戏进程**。理由：① 游戏内 Python（3.7，无 pip）
无法引入 sounddevice/pyogg；② 音频线程卡顿会影响游戏帧率；③ UDP 语音与 TCP
游戏数据分离，丢包不互相干扰。mod 与启动器之间只交换"谁在按住说话"的轻量控制
信号（聊天框头像高亮显示说话者）。

**网络**：UDP 7662，包格式 `VoiceChatPacket`（seq + ts + opus data ≤ 200B），
房主做 SFU 转发（4 人内星型拓扑足够，无需混流服务器）。NAT 场景沿用 TCP 游戏通道
的打洞结果（同一 UDP 目标端口回应即可保持映射）。

### 4.3 验收

- 拔插网线触发重连事件，3s 内本机听到"正在重新连接"播报。
- 双机 PTT 端到端延迟 <300ms（LAN），按住时聊天框头像高亮同步。
- 语音进程崩溃不影响游戏（托盘进程 watchdog 自动重启，游戏侧无感知）。

---

## 5. v9.70 —— 反作弊

### 5.1 功能

| 层 | 检测 |
|:---|:-----|
| 移动校验 | 位置变化速率上限（跑步速度上限 ×2 容差）、瞬移检测、delta_q 累加漂移纠偏 |
| 经济校验 | 金钱单次变动上限、负值/溢出拒绝、收支比对（卖出所得累计） |
| 速率限制 | 每消息类型令牌桶（防消息洪泛/刷屏/交互轰炸） |
| 重放防护 | seq 单调递增 + 时间戳窗口（防录制重放 sim_pos/interaction） |
| 信任分 | 违规记录 → 扣分 → 阈值降级（限制发言→踢出→拉黑房间） |

### 5.2 技术方案

**部署位置**：全部在**房主侧**（Host Authority 的落地——客机只上报，房主验证后才
广播）。核心是 `ValidationPolicy` + 每客机 `PlayerTrustRecord`。

**移动校验**：房主为每个远端 sim 维护 `last_pos + last_ts`。收到 `sim_pos`：
`speed = dist/dt`，`speed > MAX_SPEED(≈12 m/s，游戏跑姿上限×2)` → 丢弃该帧 +
记录违规；连续 3 帧超速 → 触发 `correction_req`（要求该端重发绝对 position）。
顺手修复 KNOWN-N9：每 30s 或累计误差 >0.5m 时要求发送端回传绝对坐标重置基准
（反作弊基础设施同时解决量化漂移）。

**经济校验**：money_sync 到达房主时不是直接广播，而是先记账：
`delta = funds_new - funds_old`；合法变化来源（工资/卖物/账单）都发生在**房主自己的
模拟里**，客机上报的 funds 只允许"向房主值收敛"，偏差 > 阈值时以房主值为准回发
纠正帧。客机直接改钱（CE/修改器）在房主侧表现为不可收敛的持续偏差 → 信任分扣减。

**令牌桶**：每 (pid, 消息类型) 一个桶。初始令牌 = burst，速率 refill。
超限消息丢弃 + `rate_exceeded` 事件计数。默认策略：
chat 10/min、sim_pos 10/s、interaction 5/s、money_sync 1/s。

**重放**：`sim_pos.seq` 接收端强制单调（`seq <= last_seq` → 丢弃）；
`ts` 与本地时钟偏差 >60s 的交互消息丢弃（弱校验，时钟不同步时降级为仅日志）。

**信任分**：初始 100。超速 -2/次、金钱不可收敛 -10/次、令牌桶溢出 -1/次。
`<60` 禁言（chat 桶置零）、`<30` 自动踢出 + 房间黑名单 30 分钟（持久化到
`mp_banlist.json`，按 IP+玩家名）。误伤申诉：房主可 `mp_trust <pid> <分数>` 手动
修正。所有扣分事件写 `mp_audit.log`（含证据快照）供房主复查。

**边界声明**：Sims4 是同好联机而非竞技游戏，本层目标是"挡住随手 CE 改钱与恶意
客户端"，不承诺对抗内存修改器级别的攻击——深度防御依赖 v9.50 mod 一致性
（确保没有改装过的 multimod 副本）+ v9.90 协议 v3 的认证强度。

### 5.3 验收

- CE 改钱 3 次内被纠正回房主值，第 3 次触发警告，第 5 次自动踢出。
- 洪发 sim_pos（1000 条/s 脚本）被令牌桶压制，房主帧率无可感知波动。
- 篡改 seq 回放旧位置帧：全部丢弃且记审计日志。

---

## 6. v9.80 —— 网络与性能

1. **QoS 落地**（KNOWN-N1）：`_send_json` 的 prio 生效——prio=2 的 sim_pos 进
   每 100ms 冲刷的合批器（真正调用 `_send_batch`）；prio=0 直发。指标：位置消息
   帧头开销占比从 ~40% 降到 <8%。
2. **压缩**：帧级 zlib（>1KB 的 payload 压缩，帧头加 1 字节标志位——同
   PROTO_VERSION 内软升级，老端解析新标志位为"无压缩"需 bump 小版本号，
   与 §8.2 原则一致的做法是只在 v3 实施；v2 内先做消息级：`stats_sync` 等
   大 payload 单独 gzip）。
3. **增量 world_snapshot**：登录全量快照 + 之后周期性 diff 快照（Replicator.Diff
   思路，ARCHITECTURE.md §8 的落地）。
4. **重连修复**：S-2/S-4 修复后补"重连后全量重对齐"（重连成功 → host 自动重发
   world_snapshot + lobby + 未完成的存档同步续传）。

**验收**：8 客机同地段，带宽 <120KB/s/端；位置同步 p95 端到端延迟 <250ms（LAN）。

---

## 7. v9.90 —— 协议 v3（PROTO_VERSION = 3）

帧格式变更（详见协议规范 §8.3）：

```
[ver:1B=3][type:2B 明文消息类型][flags:1B(压缩/加密位)][len:4B][crc:4B][hmac:32B][payload]
```

- 认证前（无会话密钥）：只接受 hello/welcome/版本协商，payload 强制 JSON（禁 pickle）。
- pickle 仅在 HMAC 建立后启用，且可协商为 msgpack（ts4script 可内置纯 Python 实现）。
- v3 host 对首字节非 3 的首帧回退 v2 解析（过渡互通一个版本周期）。
- 跨网：UPnP 映射已有（launcher.py），补 STUN/TURN 候选交换（hello/welcome 扩展
  `candidates` 字段），P2P 失败时走中继（目录服务器兼做 TURN，仅 TCP）。

---

## 8. v10.0 —— 正式版

1. 全量回归：17 套虚拟测试 + 新增 v10 集成套件（房间/mod/语音/反作弊各 1 套）+ 双机真机矩阵。
2. 自动更新：启动器检查 GitHub Release，差量下载（manifest 对比只下变更文件——
   复用 v9.50 的分发通道实现，吃自己的狗粮）。
3. 文档定稿：本路线图勾销、协议规范 v3.0 发布、KNOWN_ISSUES 清零 P0/P1。
4. 发布物：启动器 exe（PyInstaller spec 已有）+ multimod ts4script 包 + 首次安装指南。

---

## 9. 里程碑依赖图

```
v9.30 修复波 ──┬─> v9.40 房间 2.0 ──┬─> v9.60 语音 ──┐
               │                     │                ├─> v9.90 协议v3 ─> v10.0
               └─> v9.50 mod同步 ───┴─> v9.70 反作弊 ─┘
                                        v9.80 性能（可与 9.6/9.7 并行）
```

- 反作弊的"mod 一致性"依赖 v9.50。
- 语音 v1（事件播报）无依赖可提前；PTT 依赖 v9.30 的文件桥修复。
- 协议 v3 依赖反作弊的信任模型稳定（认证/会话语义不再变）。
