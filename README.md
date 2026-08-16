# Sims4Multiplayer — 自制模拟人生联机 mod

> **Sims4Multiplayer 是自研的《模拟人生 4》联机 mod（SimSync），用 Python 3.7 脚本 + TCP 网络层为单人游戏加入多人同步——从零反编译游戏 API、自制协议、无第三方库依赖。**
> 项目启动：2026-08-03 · 开发者：sora + k (Hermes)

<p>
  <img src="https://img.shields.io/badge/status-开发中-2563EB?style=flat-square" alt="开发中">
  <img src="https://img.shields.io/badge/python-3.7-3776AB?style=flat-square" alt="Python 3.7">
  <img src="https://img.shields.io/badge/protocol-TCP--JSON-16A34A?style=flat-square" alt="TCP/JSON">
</p>

---

## 📊 项目进度

| 里程碑 | 状态 | 说明 |
|--------|:---:|------|
| M0 环境搭建 | ✅ | Python 3.7.9 + uncompyle6 + 游戏库反编译 |
| M0 Hello World | ✅ | 游戏内弹通知 + mp_hello 命令（已验证打包链路）|
| M1 聊天同步 | ✅ | TCP + JSON 协议，mp_host/mp_join/mp_say（协议已本机验证）|
| M2 Sim 位置同步 | 🔧 骨架 | mp_sync/mp_target 已写，游戏内 API 待测试迭代 |

## 📁 目录结构

```
D:\Sims4-Multiplayer-Dev\
├── src/multimod/        ← mod 源码（Python）
│   ├── __init__.py      ← 入口，导入各模块
│   ├── network.py       ← M1: TCP 网络层（host/join/say）
│   └── sync.py          ← M2: 位置同步（骨架）
├── python/              ← 游戏 API（反编译参考）
│   ├── base/            ← base.zip 解压 (420 pyc)
│   ├── core/            ← core.zip 解压 (121 pyc)
│   └── simulation/      ← simulation.zip 解压 (2724 pyc)
├── docs/
│   ├── api/             ← 反编译出的关键 API 源码
│   └── 学习笔记.md      ← 反编译/API 学习记录
├── tools/
│   ├── python37-embed/  ← 免安装 Python 3.7.9（开发用）
│   └── python-3.7.9-amd64.exe（安装版备份）
├── build/               ← 打包产物
│   └── Sims4Multiplayer.ts4script
└── build.sh             ← 一键打包脚本
```

## 🔧 技术栈

| 组件 | 版本 | 说明 |
|------|------|------|
| 游戏 | The Sims 4 v1.126.73.1030 | anadius 版，34 DLC |
| 游戏脚本 | Python 3.7.9 | 内置解释器（game/bin/python37_x64.dll）|
| mod 格式 | .ts4script | 本质是 zip 包，内含 .pyc |
| 网络 | TCP + JSON | 主机 :7655，消息换行分隔 |
| 反编译 | uncompyle6 3.9.3 | 游戏 pyc → 可读源码 |

## 🚀 使用方式

### 游戏内命令（Ctrl+Shift+C 打开控制台）

```
mp_hello              # 验证 mod 加载（弹通知）
mp_host               # 主机模式：开启 TCP server :7655
mp_join <IP>          # 客机模式：连接主机
mp_say <文本>          # 发送聊天消息（对方弹通知）
mp_status             # 查看连接状态
mp_sync               # M2: 开始位置广播
mp_target <sim_id>    # M2: 设置同步目标
```

### 联机流程

1. 两台电脑都装好 mod（Mods 目录有 Sims4Multiplayer.ts4script）
2. 房主：游戏内 `mp_host` → 看到"等待连接"通知
3. 客机：游戏内 `mp_join 主机IP` → 看到"已连接"通知
4. 双方 `mp_say 你好` → 对方能看到

> 局域网直接连；异地需 Radmin VPN（虚拟局域网）

### 重新打包

```bash
cd D:\Sims4-Multiplayer-Dev && bash build.sh
# 自动编译 → 打包 → 部署到 Mods 目录
```

## 🧪 已验证

- ✅ 打包链路：py → pyc → ts4script（字节码 magic 420d0d0a 匹配游戏）
- ✅ 网络协议：TCP + JSON 双端通信（本机模拟验证）
- ✅ 反编译：base/core/simulation 共 3265 pyc，关键 API 已反编译成功
- ⏳ 游戏内加载：需重启游戏测试（S4MP 当前占用 Mods 目录，测试时需移出）

## ⚠️ 待办 / 已知问题

1. **游戏内测试**：重启游戏验证 M0 通知 + M1 聊天（需要先移出 S4MP 避免冲突）
2. **M2 位置 API**：`zone.get_active_sim()` / `sim.position` 需游戏内实测确认
3. **消息轮询**：目前 `mp_poll` 手动处理队列，未来应接入游戏 tick
4. **防火墙**：主机需放行 7655 端口（入方向）
5. **与 S4MP 共存**：测试时把 S4MP_release.ts4script 移出 Mods（避免脚本冲突）

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

---

*Sims4Multiplayer · 从 S4MP 启发自制 · 2026-08-03*

## 常见问题（FAQ）

**Q: 需要什么游戏版本？**
完整版《模拟人生 4》（非阉割版）+ 对应版本的游戏 API。项目基于解压出的 base/core/simulation zip 反编译。

**Q: 怎么启动联机？**
运行 （或 launcher.py）→ 主机用 、加入者用 。

**Q: 协议是自己写的吗？**
是。TCP + JSON 自定义协议（room_protocol.py）——参考 S4MP 思路，但实现完全自研。

**Q: 目前能同步什么？**
M1 聊天已完整同步（mp_say）；M2 位置同步在骨架阶段（mp_sync/mp_target 已写，游戏内 API 待迭代）。

**Q: 代码可以商用吗？**
本项目为学习用途（GPL-3.0）——涉及游戏文件仅供研究，请勿传播游戏本体。

## 更新日志

- 2026-08-16（v9.23）：稳定性研究版（千轮研究落地——搜索研究真实游戏联机稳定性模式并工程化）——① 周期性权威状态刷新：主机每 30s 重播时钟速度，任何丢失的 clock 更新自动自愈（eventual consistency，幂等 apply 零副作用）；② 主线程消息处理预算 200 条/tick：突发洪泛不再卡游戏帧，余量顺延；③ 旅行时钟锁定：travel_go 广播 PAUSED、全员到齐/超时自动恢复原速——防场景加载期间时间漂移（S4MP 加载黑屏场景的经典对策）；④ 网络线程看门狗：server/client 线程意外死亡 10s 内自动重启，杜绝"房主还在但没人能连"僵死；⑤ 状态文件新增连接健康度（RTT+评级），启动器房间页显示 📶 连接质量
- 2026-08-16（v9.22.1）：启动器体验版——① 修复关键分发问题：`启动联机.bat` 原优先启动 dist 里的旧打包 exe，源码的所有新功能被掩盖；现源码优先（含依赖探测，失败自动回退 exe）；② 启动器美化：窗口首次屏幕居中 + 关闭记忆窗口几何、中文统一微软雅黑字体；③ API 审计：核对 sync/mood/interaction/buy 全部游戏 API 对照反编译 pyc（Vector3/Location.clone/get_mood/add_buff/si_state 等均实证存在），api_shape 测试扩至 30 断言
- 2026-08-16（v9.22）：修复+强化大版本——① P1 安全修复：HOST_ONLY 时钟白名单生效（原 clock_sync 笔误）、握手前帧策略（未认证连接限 4KB+类型白名单）、per-connection nonce（并发握手不再互踩）、客机端 HMAC 验签修复（原查 key 恒空）；② 共同旅行增强（S4MP 无此功能）：客机自动确认 + 进图自动到达上报 + 30/60/90s 分级超时 + 全员抵达后快照刷新 + 启动器旅行状态板；③ 性能：sock→pid 反向索引 O(1)；④ 启动器：更新源指向正确仓库、新增共同旅行/自动确认按钮；⑤ 新增 API 形状校验测试（扫描反编译 pyc 防虚拟绿/游戏炸）；auto_sync 等待 60s→150s
- 2026-08-16（v9.21.1）：修复时钟同步根因——`services.get_game_clock_service` 在真实游戏不存在（正确名 `game_clock_service`），客机永不变速；同步修复 6 个模块的 `from sims4 import services`（模块不存在）
- 2026-08-07：M2 位置同步骨架 + 协议演进（帧头升级连锁 + HMAC 握手）
- 2026-08-05：M1 聊天同步完整（TCP/JSON + mp_say）
- 2026-08-03：M0 环境搭建 + Hello World 验证打包链路
