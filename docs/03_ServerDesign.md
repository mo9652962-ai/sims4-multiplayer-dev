# SimSync 服务器设计（V1.0）

> 对应 ARCHITECTURE.md §4.3，详细说明服务器端架构

## 1. 架构概览

SimSync 采用 **Listen Server（房主托管）** 模式——房主既是玩家也是服务器。

```
┌──────────────────────────────────────┐
│         房主电脑                      │
│  ┌────────────┐  ┌────────────────┐  │
│  │ 启动器      │  │ 游戏 + TS4 Mod │  │
│  │ RoomServer │  │ HostServer     │  │
│  │ (TCP 7660) │  │ (TCP 7655)     │  │
│  └─────┬──────┘  └───────┬────────┘  │
│        │                 │            │
└────────┼─────────────────┼────────────┘
         │                 │
    ┌────▼────┐       ┌────▼────┐
    │ 加入者   │       │ 加入者   │
    │ 启动器   │       │ 游戏 Mod │
    └─────────┘       └─────────┘
```

**为什么用 Listen Server 而非 Dedicated Server？**
- 研究（2026-08-05）：Unity 文档确认 **"Listen servers are best suited for smaller group (< 12)"**
- 我们的场景：2-12 人 LAN 朋友联机 → 不需要独立服务器部署
- 零成本：房主电脑即服务器，无需额外 VPS

## 2. 两阶段服务器

### 2.1 阶段 1：启动器房间（RoomServer，端口 7660）

**职责**：房间管理（不依赖游戏）

| 功能 | 实现 |
|:-----|:-----|
| 房间创建 | `RoomServer.start()` 开 TCP 7660，生成 6 位房间码 |
| 加入/离开 | `join`/`leave` 消息 + 成员表广播 |
| 准备状态 | `ready` 消息 + 全员 ready 检测（Nakama all_ready 模式） |
| 存档同步 | `save_sync_start`/`save_chunk`/`save_sync_done`（base64 分块，64KB） |
| 开始游戏 | `start_game` 广播 → 双方启动器拉起游戏进程 |

**状态机**：
```
waiting → ready（全员准备）→ syncing（存档同步）→ synced → launching
```

**源码**：`room_protocol.py`（独立于 GUI/游戏，可单元测试）

### 2.2 阶段 2：游戏内网络（HostServer，端口 7655）

**职责**：游戏内状态同步（mod 初始化后自动连接）

| 功能 | 实现 |
|:-----|:-----|
| 多客户端管理 | `_clients` dict（player_id → socket） |
| 消息广播 | `broadcast()` 排除发送者 |
| 帧协议 | 44 字节头（长度/CRC32/HMAC）+ pickle payload |
| 优先级队列 | 3 级（高优=交互事件，中优=位置，低优=需求） |
| 批处理 | 同一 tick 内消息合并发送 |
| 心跳 | 5s 间隔，15s 超时断开 |

**源码**：`src/multimod/network.py`（`_server_thread` / `_handle_client`）

## 3. 协议分层

```
┌─────────────────────────┐
│  应用层：JSON 消息        │  ← 启动器房间（room_protocol.py）
│  {type, ts, payload}     │
├─────────────────────────┤
│  应用层：pickle 帧        │  ← 游戏内同步（network.py）
│  {type, sim_id, ...}     │
├─────────────────────────┤
│  安全层：HMAC-SHA256      │  ← 消息认证（v9.16）
│  44B 头 + HMAC 标签      │
├─────────────────────────┤
│  传输层：TCP + NODELAY    │  ← 取消 Nagle 算法
│  TCP_NODELAY + keepalive │
└─────────────────────────┘
```

## 4. 安全设计

| 层级 | 机制 | 引入版本 |
|:-----|:-----|:--------|
| 消息完整性 | CRC32 校验（44B 帧头） | v9.12 |
| 消息认证 | HMAC-SHA256（共享密钥） | v9.16 |
| 密钥交换 | HKDF 派生（per-client 密钥） | v9.16 |
| 防重放 | 序列号递增检测 | v9.12 |
| 输入校验 | 消息长度上限 8MB / 名字长度 16 字符 | v9.12 |

**为什么不用 TLS？**
- Sims4 嵌入式 Python 3.7 可能缺少完整 ssl 模块
- LAN 环境信任度高（非公网）
- HMAC 已提供消息认证（防篡改）

## 5. 性能设计

| 优化 | 实现 |
|:-----|:-----|
| TCP_NODELAY | 取消 Nagle 算法，立即发送小包 |
| 批处理 | 同一 tick 内消息合并（减少 TCP 分段） |
| 优先级队列 | 交互事件优先于位置更新 |
| 频率控制 | 位置 10Hz / 需求 1Hz / 事件即时 |
| 自适应间隔 | 根据网络延迟动态调整位置广播频率 |

## 6. 目录结构

```
server/
├── room_protocol.py    # 启动器房间协议（RoomServer/RoomClient）
├── network.py          # 游戏内网络层（帧协议/HMAC/广播）
├── lobby.py            # 大厅管理（成员列表/房间码）
├── sync.py             # 位置同步调度
├── clock_sync.py       # 时间速度同步
├── stats_sync.py       # 需求/技能同步
├── money_sync.py       # 家庭资金同步
├── mood_sync.py        # 心情同步
├── interaction_sync.py # 交互队列同步
├── inventory_sync.py   # 背包同步
├── relationship_sync.py# 关系同步
└── buy_sync.py         # Buy 家具同步
```

## 7. 故障处理

| 场景 | 处理 |
|:-----|:-----|
| 客户端断开 | 30s 检测 → 清理 socket + 成员表 → 广播更新 |
| 服务端崩溃 | 所有客户端断开 → 各自恢复单机模式 |
| 端口占用 | 启动时检测 → 提示用户关闭占用程序 |
| 消息超长 | 8MB 上限 → 自动截断 + 日志警告 |
| HMAC 验证失败 | 断开连接 + 安全日志 |

## 8. 监控

| 指标 | 获取方式 |
|:-----|:---------|
| 连接数 | `len(_clients)` |
| 消息速率 | 每 30s 计数（`mp_debug.log`） |
| 带宽 | psutil 网络 IO 差值 |
| 延迟 | 心跳 RTT（`time.time()` 差值） |
| 错误率 | CRC/HMAC 失败计数 |