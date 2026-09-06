# Sims4Multiplayer — 自制模拟人生联机 mod（SimSync）

> **SimSync 是自研的《模拟人生 4》联机 mod：Python 3.7 游戏内脚本 + 自研 TCP 协议 + Windows 启动器，支持房间大厅、存档同步与 9 类游戏状态同步——从零反编译游戏 API 完成接口设计，无第三方运行时依赖。**
> 项目启动：2026-08-03 · 开发者：sora + k (Hermes) · 协议版本 v2（HMAC 验签）

<p>
  <img src="https://img.shields.io/badge/version-v9.28-7C3AED?style=flat-square" alt="v9.28">
  <img src="https://img.shields.io/badge/python-3.7-3776AB?style=flat-square" alt="Python 3.7">
  <img src="https://img.shields.io/badge/protocol-TCP--JSON--HMAC-16A34A?style=flat-square" alt="TCP/JSON/HMAC">
  <img src="https://img.shields.io/badge/license-GPL--3.0-EA580C?style=flat-square" alt="GPL-3.0">
</p>

---

## 📊 项目进度

| 模块 | 状态 | 说明 |
|------|:---:|------|
| 状态同步层 | ✅ | 聊天 / 位置 / 时钟 / 金钱 / 物品 / 心情 / 关系 / 属性 / 购买，9 类同步 |
| 房间大厅系统 | ✅ | 建房 / 密码房 / 准备-就绪 / 踢人 / 晋升主机 / 存档同步 / 一键开局 |
| 共同旅行 | ✅ | 主机正常旅行全员自动跟随（游戏原生 API）、分级超时、时钟锁定防漂移 |
| Windows 启动器 | ✅ | 房间流程 GUI、源码优先自启动、直启游戏按钮、连接质量显示 |
| 网络层稳定性 | ✅ | 看门狗自愈、主线程消息预算、周期性权威刷新（eventual consistency） |
| 安全加固 | ✅ | HMAC 握手 / 握手前帧策略 / pickle 白名单 / 路径穿越防护 / 存档块校验（Gemini 多轮安全复审） |
| 真机双机回归 | 🔧 | v9.30 计划：私密房 / 断线重连 / 主机迁移 / 存档同步四链路回归 |

规划全貌见 [docs/ROADMAP_V10.md](docs/ROADMAP_V10.md)（v9.30 修复波 → v9.40 房间 2.0 → v9.50 Mod 同步 → … → v10.0 正式版）。

## 📁 目录结构

```
D:\Sims4-Multiplayer-Dev\
├── src/multimod/          ← mod 源码（Python，16 个模块）
│   ├── __init__.py        ← 入口：模块加载 + 服务注册
│   ├── network.py         ← TCP 网络层（QoS / 帧策略 / 看门狗）
│   ├── lobby.py           ← 房间大厅（建房/准备/踢人/晋升/存档同步）
│   ├── sync.py            ← Sim 位置同步
│   ├── clock_sync.py      ← 时钟同步（权威刷新 + 旅行锁定）
│   ├── money_sync.py      ← 金钱同步
│   ├── inventory_sync.py  ← 物品同步
│   ├── mood_sync.py       ← 心情同步
│   ├── relationship_sync.py ← 关系同步
│   ├── stats_sync.py      ← 属性同步
│   ├── buy_sync.py        ← 购买模式同步
│   ├── interaction_sync.py← 互动同步
│   ├── local_ops_filter.py← 本地操作过滤（防重复执行）
│   ├── event_system.py    ← 事件系统
│   └── reload_service.py  ← 热重载服务
├── launcher.py            ← Windows 启动器（房间流程 GUI）
├── room_protocol.py       ← 启动器侧协议实现
├── 启动联机.bat           ← 一键启动（源码优先，失败回退打包 exe）
├── build.sh               ← 一键打包（py → pyc → .ts4script）
├── Sims4Multiplayer.spec  ← PyInstaller 打包配置
├── tools/                 ← 虚拟测试套件（协议/大厅/API 形状校验，千轮级）
└── docs/                  ← 设计文档：协议规范 / 服务端设计 / 路线图 / 已知问题 / 双机验证清单
```

## 🔧 技术栈

| 组件 | 版本 | 说明 |
|------|------|------|
| 游戏 | The Sims 4 v1.126.73.1030（PC 版） | 完整版即可，需对应版本游戏 API |
| 游戏脚本 | Python 3.7.9 | 游戏内置解释器（python37_x64.dll）|
| mod 格式 | .ts4script | 本质是 zip 包，内含 .pyc |
| 网络 | TCP + JSON | 主机 :7655，换行分隔帧，PROTOCOL_VERSION=2 |
| 认证 | HMAC-SHA256 | 握手验签 + 恒定时间比对 |
| 启动器 | Python 3.11+ / PyInstaller | Tkinter GUI，可打包单文件 exe |

## 🚀 使用方式

### 启动器流程（推荐）

1. 两台电脑都装好 mod（`Mods/Sims4Multiplayer.ts4script`）并运行 `启动联机.bat`
2. 房主「创建房间」（可选密码）→ 成员「加入房间」（输入房间码）
3. 全员「准备」→ 房主「同步存档」（分块传输 + SHA 校验）→「开始游戏」自动拉起
4. 进入游戏后房间状态自动衔接（大厅模式接管）

### 游戏内命令（Ctrl+Shift+C 打开控制台）

**基础联机**

```
mp_host                # 主机模式：开启 TCP server :7655
mp_join <IP>           # 客机模式：连接主机
mp_status              # 查看连接状态
mp_say <文本>          # 聊天（对方弹通知）
mp_leave               # 离开房间
```

**状态同步**

```
mp_sync / mp_target <sim_id>   # 位置同步
mp_clock <速度>                # 时钟控制
mp_money <金额>                # 金钱同步
mp_invsync / mp_intsync        # 物品 / 互动同步
mp_mood / mp_relsync / mp_statssync  # 心情 / 关系 / 属性
mp_buysync                     # 购买模式同步
```

**房间大厅**

```
mp_lobby / mp_code             # 大厅信息 / 房间码
mp_ready / mp_unready          # 准备 / 取消
mp_start                       # 开始游戏
mp_kick <pid> / mp_promote     # 踢人 / 晋升主机
mp_saves / mp_syncsave         # 存档列表 / 存档同步
```

**共同旅行**

```
mp_travel <地点>               # 发起共同旅行
mp_travel_auto                 # 开关自动跟随
mp_follow_travel               # 立即跟随主机旅行
```

> 局域网直接连；异地推荐 Radmin VPN（虚拟局域网）

### 重新打包

```bash
cd D:\Sims4-Multiplayer-Dev && bash build.sh
# 自动编译 → 打包 → 部署到 Mods 目录
```

## 🔐 安全设计

- **认证**：HMAC-SHA256 握手验签，`hmac.compare_digest` 恒定时间比对（防时序攻击）
- **帧策略**：握手前限 4KB + 类型白名单；per-connection nonce 防并发握手互踩
- **反序列化**：pickle 白名单机制，未认证连接禁 pickle
- **文件传输**：文件名白名单（防路径穿越 RCE）、存档块序号校验 + 长度上限 + 重复块幂等
- **并发**：成员表最小锁、广播锁、Timer 代次机制（防竞态）
- 多轮独立安全复审（Gemini）驱动修复：v9.19 协议 9 项 → v9.22 P1 → v9.25 路径穿越 → v9.27 架构 → v9.28 存档校验

## 🧪 验证

- ✅ 虚拟测试套件（tools/）：协议 / 大厅 / API 形状校验（30 断言防"虚拟绿、游戏炸"），千轮级稳定性研究工程化
- ✅ API 审计：全部游戏 API 对照反编译 pyc 实证存在（Vector3 / Location.clone / get_mood / send_travel_switch_to_zone_op 等）
- ⏳ 真机双机回归：v9.30 里程碑（见 docs/双机验证清单.md）

## ⚠️ 已知问题

见 [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md)：基于 v9.22 全量审查（16 模块 + 启动器，约 8600 行），P0×16 / P1×28 / P2×40+，v9.19–v9.28 已消化其中大部分，剩余项在 v9.30 修复波清零。

## 📚 学习笔记要点

### Sims 4 mod 原理
- 游戏内置 Python 3.7.9，启动时 import Mods 下所有 .ts4script（zip 格式）
- mod 代码可以 import 游戏的 base/core/simulation 模块
- 服务注册：`from sims4.service_manager import Service` + 子类化
- 命令注册：`@sims4.commands.Command('名字', command_type=CommandType.Live)`
- 通知弹窗：`ui.ui_dialog_notification.UiDialogNotification.TunableFactory()`

### 反编译方法
```
# 解压游戏库
unzip base.zip core.zip simulation.zip
# 反编译单个文件
uncompyle6.exe simulation/services/cheat_service.pyc > out.py
```

## 更新日志

- 2026-09-01：工程化收尾——gitignore 忽略备份文件（\*.bak\*/\*.old），备份保留本地可回滚
- 2026-08-20（v9.28）：存档块接收校验（Gemini 安全复审）——块序号范围校验 / 64KB 数据长度上限 / 重复块幂等，配合 MAX_FRAME_SIZE / HMAC 先验签 / pickle 白名单 / 队列 4096
- 2026-08-20（v9.27）：Gemini 架构审查落地——① 离线级联结算：补 on_client_disconnect 级联清理（旅行 ack / 存档缓存），剩余成员全确认自动推进旅行；② 主机迁移保留房间设置（私密房迁移后不丢密码）；③ 成员表最小锁（检查-修改原子化）
- 2026-08-17（v9.25）：连续旅行竞态修复——旅行分级 Timer 统一走代次机制（`_travel_gen` + `_cancel_travel_timers`）：新旅行开始代次自增令旧 Timer 作废、四处集中取消（新旅行/到齐/超时/重置）、Timer daemon 化；`travel_go` 与 `travel_follow` 两条路径统一。修复连续旅行/快速切场景时的：旅行中途被错误打断、超时提示乱跳、时钟状态错乱
- 2026-08-17（v9.24）：游戏内旅行自动跟随 + 启动器直启恢复——① **真正的共同旅行**：主机在游戏里正常旅行（手机选地点/点地图/拜访）即被自动检测（每 tick 监控 `current_zone_id`），成员通过**游戏原生 API** `sim_info.send_travel_switch_to_zone_op` 自动跟随到同一地点（反编译 `sims.visit_target_sim` 实证），全程零命令，链路自动衔接 v9.22 到达上报与 v9.23 时钟锁定/快照刷新；`mp_follow_travel` 可关；`travel_follow` 加入房主专属白名单防伪造；② 启动器连接页新增"🎮 直接启动游戏"按钮
- 2026-08-16（v9.23）：稳定性研究版（千轮研究落地）——① 周期性权威状态刷新：主机每 30s 重播时钟速度，丢失更新自动自愈；② 主线程消息处理预算 200 条/tick：突发洪泛不卡游戏帧；③ 旅行时钟锁定：travel_go 广播 PAUSED、全员到齐/超时自动恢复——防场景加载期间时间漂移；④ 网络线程看门狗：server/client 线程意外死亡 10s 内自动重启；⑤ 状态文件新增连接健康度（RTT+评级），启动器显示 📶 连接质量
- 2026-08-16（v9.22.1）：启动器体验版——① 修复 `启动联机.bat` 优先启动 dist 旧 exe 的分发问题（现源码优先 + 依赖探测回退）；② 启动器美化：首次居中 + 记忆窗口几何 + 统一微软雅黑；③ API 审计扩至 30 断言
- 2026-08-16（v9.22）：修复+强化大版本——① P1 安全修复：HOST_ONLY 时钟白名单生效、握手前帧策略（未认证限 4KB+类型白名单）、per-connection nonce、客机端 HMAC 验签修复；② 共同旅行增强（S4MP 无此功能）：客机自动确认 + 进图自动到达上报 + 30/60/90s 分级超时 + 全员抵达快照刷新 + 启动器旅行状态板；③ 性能：sock→pid 反向索引 O(1)；④ API 形状校验测试上线；auto_sync 等待 60s→150s
- 2026-08-16（v9.21.1）：修复时钟同步根因——`services.get_game_clock_service` 在真实游戏不存在（正确名 `game_clock_service`），客机永不变速；同步修复 6 个模块的 `from sims4 import services`
- 2026-08-07：M2 位置同步骨架 + 协议演进（帧头升级连锁 + HMAC 握手）
- 2026-08-05：M1 聊天同步完整（TCP/JSON + mp_say）
- 2026-08-03：M0 环境搭建 + Hello World 验证打包链路

## 常见问题（FAQ）

**Q: 需要什么游戏版本？**
完整版《模拟人生 4》PC 版，且游戏版本需与本项目的 API 基准（v1.126.73.1030）匹配——游戏更新后 API 可能变动，需重新反编译对照。

**Q: 怎么启动联机？**
推荐 `启动联机.bat` 走启动器全流程（建房 → 加入 → 准备 → 同步存档 → 开始游戏）；也可游戏内控制台 `mp_host` / `mp_join <IP>`。

**Q: 协议是自己写的吗？**
是。TCP + JSON 自定义协议（`room_protocol.py` + `src/multimod/network.py`，PROTOCOL_VERSION=2，HMAC 验签）——参考 S4MP 思路，实现完全自研。

**Q: 目前能同步什么？**
聊天 / 位置 / 时钟 / 金钱 / 物品 / 心情 / 关系 / 属性 / 购买共 9 类状态同步 + 房间大厅（含存档同步）+ 共同旅行。断线重连与主机迁移的架构已落地，真机回归是 v9.30 里程碑。

**Q: 代码可以商用吗？**
本项目为学习用途（GPL-3.0）——反编译产物仅限本地研究，请勿分发游戏文件或游戏本体。

---

*Sims4Multiplayer（SimSync）· 从 S4MP 启发自制 · 2026-08-03*
