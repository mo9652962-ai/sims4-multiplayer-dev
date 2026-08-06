# Sims4Multiplayer — 自制模拟人生联机 mod

> 从零开始的自制 Sims 4 联机 mod（Python 3.7 脚本 + TCP 网络层）
> 项目启动：2026-08-03 · 开发者：sora + k (Hermes)

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
