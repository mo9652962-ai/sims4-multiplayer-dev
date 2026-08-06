# SimSync —— The Sims 4 联机框架设计文档（V1.1）

> 基于 V1.0 + 搜索引擎验证后的改进版
> 改进点标注 🆕

## 修订历史

| 版本 | 日期 | 变更 |
|:-----|:-----|:-----|
| V1.0 | 2026-08-05 | 初稿，完整架构设计 |
| V1.1 | 2026-08-05 | 🆕 搜索引擎验证：Tick 降频、Header 字段排序、ADR 格式、Minecraft wiki.vg 协议格式参考、验收标准 |

---

## 1. 项目概述

### 1.1 项目名称
**SimSync**

### 1.2 项目目标
SimSync 是《The Sims 4》的多人联机框架，目标是在不修改游戏核心逻辑的前提下，实现多个玩家共同游玩同一个存档。

**本项目仅同步必要的游戏状态和玩家操作，不同步整个游戏内存。**

---

## 2. 设计原则

> 🆕 采用 ADR（Architecture Decision Record）格式记录关键设计决策

### ADR-001: 简单（Simple）
- **Status**: Accepted
- **Context**: 单人开发，需要降低维护成本
- **Decision**: 保持模块职责单一；每个模块只做一件事
- **Consequences**: 模块数量多但每个都很小，易于测试和替换

### ADR-002: 可扩展（Extensible）
- **Status**: Accepted
- **Context**: 未来需要增加更多同步内容
- **Decision**: 采用 Replicator 模式（每类对象一个 Serialize/Apply/Diff）；消息类型用 ID 表扩展
- **Consequences**: 新增同步类型只需加一个 Replicator + 注册消息 ID；无需改动底层

### ADR-003: Host Authority
- **Status**: Accepted
- **Context**: 多人游戏需要唯一权威源防止状态冲突
- **Decision**: 服务器（或房主）拥有最终世界状态；客户端仅发送请求，不直接修改世界
- **Consequences**: 防作弊、防冲突；代价是客户端操作需等服务器确认（延迟）

### ADR-004: Event Driven
- **Status**: Accepted
- **Context**: 同步全部状态带宽不可行
- **Decision**: 同步事件（玩家操作 + 状态变化），而不是同步所有数据
- **Consequences**: 带宽远低于全量同步；代价是需要处理事件丢失/乱序

---

## 3. 总体架构

```
┌──────────────────────┐
│       Launcher       │
└──────────┬───────────┘
           │
┌──────────▼───────────┐
│    Network Client    │
└──────────┬───────────┘
           │
┌──────────▼───────────┐
│  Replication Layer   │
└──────────┬───────────┘
           │
┌──────────▼───────────┐
│   TS4 Script Mod     │
└──────────┬───────────┘
           │
┌──────────▼───────────┐
│     The Sims 4       │
└──────────────────────┘
```

**服务器侧**：
```
        Server
          │
  ┌───────┴───────┐
  │               │
Client A       Client B
  │               │
  └───────┬───────┘
          │
     Game World
```

---

## 4. 模块设计

### 4.1 Launcher
**负责**：
- 玩家名设置 / 登录
- 🆕 房间管理（创建房间 → 房间码 → 加入 → 准备 → 同步存档 → 开始游戏）——**不启动游戏，所有房间操作在启动器完成**
- 自动更新 / 版本检查 / Mod 检查
- 游戏启动（房间阶段结束后才启动游戏进程）

### 4.2 Client
**负责**：
- 网络连接（TCP 7660 房间 / 7655 游戏）
- Packet 收发（JSON 行协议 / pickle 帧协议）
- 心跳（5s/15s 超时）
- 断线重连（指数退避 + jitter）
- Packet Queue（消息队列）

### 4.3 Server
**负责**：
- Room（房间创建/加入/准备/离开）
- Player（成员管理）
- Tick（🆕 10Hz 高频 / 1Hz 中频）
- Save Hash（SHA256 校验）
- Host Authority（请求验证）
- Packet Broadcast（广播）

> **Server 不运行游戏。Server 仅维护世界状态。**

### 4.4 TS4 Script Mod
**负责**：
- **监听**：Sim Interaction / Object / BuildBuy / Inventory / Needs / Relationships / Time / Buff
- **执行**：收到 Packet 后执行对应游戏操作

### 4.5 Shared Library
**保存**：
- Packet 定义 / Enum 枚举 / Serializer 序列化 / Version 版本号

> 客户端和服务器共用。

---

## 5. 数据流

```
玩家点击「做饭」
      │
      ▼
TS4 Event（Mod 监听到）
      │
      ▼
Packet（序列化）
      │
      ▼
Client → Server
      │
      ▼
Server 验证 → Broadcast
      │
      ▼
Other Clients → Apply Interaction
      │
      ▼
对端小人开始做饭
```

---

## 6. Packet 协议

### 6.1 Packet Header
> 🆕 字段按频次/重要性排序（参考 IPv4 header 设计）

```
[Version:1B] [PacketType:1B] [Sequence:2B] [Tick:4B] [Length:4B]
```

| 字段 | 大小 | 说明 |
|:-----|:-----|:-----|
| Version | 1B | 协议版本（递增，不兼容拒绝） |
| PacketType | 1B | 消息类型 ID（见 §7） |
| Sequence | 2B | 包序号（递增，用于丢包检测/排序） |
| Tick | 4B | 🆕 服务器 tick 计数（事件排序用，非实时游戏不需要严格 lockstep） |
| Length | 4B | Payload 长度（不含 header） |

### 6.2 Payload 示例

**交互**：
```json
{
  "type": "interaction",
  "sim": 1024,
  "interaction": "cook",
  "object": 888
}
```

**移动**：
```json
{
  "type": "move",
  "sim": 1024,
  "x": 15.1,
  "y": 20.8,
  "level": 0
}
```

---

## 7. Packet 类型

> 🆕 ID 0=Heartbeat（最频繁），ID 1=Join（连接第一包），参考 Minecraft wiki.vg 协议格式

| ID | 名称 | 方向 | 说明 |
|:--:|:-----|:-----|:-----|
| 0 | Heartbeat | 双向 | 心跳（5s 间隔） |
| 1 | Join | C→S | 加入房间（含玩家名/房间码） |
| 2 | Leave | C→S | 离开房间 |
| 3 | Move | 双向 | Sim 位置/旋转同步 |
| 4 | Interaction | C→S→All | 交互事件（做饭/聊天/睡觉） |
| 5 | Needs | S→All | 需求/技能值同步 |
| 6 | Relationship | S→All | 关系分数/bits 同步 |
| 7 | Inventory | S→All | 背包物品同步 |
| 8 | Money | S→All | 家庭资金同步 |
| 9 | Clock | S→All | 时间速度/暂停 |
| 10 | Weather | S→All | 天气同步（预留） |
| 11 | BuildBuy | C→S→All | 家具放置/旋转/移动 |
| 12 | Save | S→All | 存档同步（分块+校验） |
| 13 | Chat | 双向 | 聊天消息 |
| 14 | WorldSnapshot | S→C | 🆕 登录全量快照（新成员加入时对齐） |

---

## 8. Replication System

```
ReplicationManager
├── SimReplicator          (Serialize/Apply/Diff)
├── ObjectReplicator
├── HouseholdReplicator
├── LotReplicator
├── ClockReplicator
└── WeatherReplicator
```

每个 Replicator 实现三个方法：
| 方法 | 说明 |
|:-----|:-----|
| `Serialize()` | 收集当前状态 → 可序列化 dict |
| `Apply(packet)` | 收到远端状态 → 应用到本地 |
| `Diff(prev, cur)` | 🆕 增量/全量/阈值策略选择（变化 >阈值 才广播） |

> 🆕 Diff 策略：全量（登录快照首次）/ 增量（位置 delta）/ 阈值（需求变化 >5 才广播，接收端差异 >15 才应用——防双向震荡）

---

## 9. Tick System

> 🆕 降频：20Hz → 10Hz——Sims4 是慢节奏模拟（Chess 类比 1-10Hz），Apex FPS 才 20Hz

| 频率 | 同步内容 | 说明 |
|:-----|:---------|:-----|
| **10Hz**（100ms）| Position / Rotation / Animation | 🆕 高频位置（含插值平滑） |
| **1Hz**（1s）| Needs / Buff / Mood | 中频状态 |
| **事件驱动** | Interaction / Money / Build / Inventory / Relationship | 变化时立即发送 |

---

## 10. Host Authority

```
Client → Request → Server → Validate → Broadcast → Clients Apply
```

**示例**：
```
玩家 A：拿起椅子
  ↓
Server：检查椅子是否已被占用
  ├── 允许 → 广播"玩家 A 拿起椅子" → 所有客户端执行
  └── 拒绝 → 返回"椅子已被占用"
```

---

## 11. Save 同步

```
Host Save → SHA256 → Server → Client Compare
                                  ↓
                            Need Download?
                           ↙ Yes        No ↘
                     Replace Save     继续
```

**Host 是唯一保存者，避免存档分叉。**

> 🆕 备份策略：写入前自动 .bak 备份（带时间戳），客户端可回退

---

## 12. 网络建议

| 阶段 | 协议 | 适用内容 |
|:-----|:-----|:---------|
| **初期**（当前）| TCP | 全部（WoW/Minecraft/Terraria 先例——慢节奏游戏纯 TCP 足够） |
| **后期**（可选）| TCP + UDP | TCP：登录/房间/Save/Chat；UDP：Position/Animation |

> 🆕 决策依据：pvigier blog 确认"Many successful games use TCP"；UDP 是 FPS 级需求，Sims4 纯 TCP 够用

---

## 13. 项目目录

```
SimSync/
├── launcher/          # 启动器（GUI + 房间管理）
├── server/            # 服务器
│   ├── room.py        # 房间管理
│   ├── world.py       # 世界状态
│   ├── packet.py      # Packet 收发
│   ├── replication.py # Replication Manager
│   └── tick.py        # Tick 时钟
├── client/            # 客户端
│   ├── network.py     # 网络连接
│   ├── packet.py      # Packet 收发
│   └── sync.py        # 同步模块
├── shared/            # 🆕 共享库（客户端/服务器共用）
│   ├── serializer.py  # 序列化
│   ├── protocol.py    # 协议定义
│   ├── enums.py       # 枚举
│   └── version.py     # 版本号
├── mod/               # TS4 Script Mod
│   ├── events/        # 游戏事件监听
│   ├── hooks/         # 游戏 Hook
│   ├── replication/   # 状态复制
│   ├── interaction/   # 交互同步
│   └── networking/    # 网络层
├── docs/              # 文档
│   ├── ARCHITECTURE.md     # 🆕 本文件
│   ├── PROTOCOL.md         # 协议规范
│   └── TS4-MOD-DEV.md      # TS4 Script Mod 开发指南
└── tests/             # 测试
```

---

## 14. Roadmap

> 🆕 每个 Phase 加验收标准（"MVP 1 done = 什么"）

| Phase | 内容 | 🆕 验收标准 | 状态 |
|:-----:|:-----|:-----|:----:|
| 1 | 服务器 + 客户端连接 + 聊天 | **2 台电脑能在同一房间看到对方聊天** | ✅ |
| 2 | 时间 + 暂停 + 倍速同步 | 主机点暂停，客机同步暂停 | ✅ |
| 3 | Sim 位置 + 动画同步 | 对方小人在屏幕上移动 | ✅ |
| 4 | Interaction Queue 同步 | 对方小人做饭/聊天，本端看到 | ✅ |
| 5 | Needs / Money / Inventory / Relationships | 双方需求/资金/背包/关系一致 | ✅ |
| 6 | BuildBuy / Lot / Objects | 家具放置/移动双方同步 | ✅ |
| 7 | Save Hash + Recovery | 存档同步 SHA256 一致 + 断线不掉进度 | ✅ |
| 8 | 优化：重连 / 版本兼容 / 增量 / 压缩 | 断线自动恢复 + 旧版提示更新 | 🟡 |

---

## 15. 后续扩展

- 插件系统 / AI NPC 同步 / 语音聊天 / 云存档
- 房间大厅（公开服务器列表）/ NAT 穿透 / 自动更新
- Replay / Debug Console / Packet Recorder

---

## 16. MVP（最小可运行版本）

**第一阶段建议实现**：
1. 建立服务器 → 客户端成功连接
2. 创建房间 → 房间码加入
3. 同步游戏时间 + 暂停状态
4. 同步 Sim 位置
5. 同步交互事件（"坐下""做饭"）
6. 基础断线重连

> 🆕 **MVP 验收**：2 台电脑 LAN 联机，房主建房 → 朋友加入 → 双方看到对方小人移动/做饭 → 聊天正常 → 断线自动重连成功

---

## 17. 总结

SimSync 的核心理念：
- **不同步整个游戏，而是同步事件和必要状态**
- **以 Host 为唯一权威，避免状态冲突**
- **使用模块化设计，便于扩展和维护**
- **采用逐步迭代开发方式，从 MVP 开始，不断增加同步能力**
