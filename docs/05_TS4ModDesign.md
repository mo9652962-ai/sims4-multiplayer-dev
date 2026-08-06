# 05 — TS4 Script Mod 设计

> SimSync 的 TS4 Script Mod 架构：事件监听 → 序列化 → 发送 → 接收 → 执行

## 模块架构

```
src/multimod/
├── __init__.py           # 入口：版本号 / 启动 / 命令注册
├── network.py            # 网络层：帧协议 / HMAC / 广播 / 接收
├── sync.py               # 位置同步：每 0.2-0.5s 广播 Sim 位置
├── clock_sync.py         # 时钟同步：时间速度 / 暂停
├── stats_sync.py         # 需求/技能同步
├── mood_sync.py          # 心情同步
├── money_sync.py         # 家庭资金同步
├── interaction_sync.py   # 交互队列同步（push_super_affordance）
├── inventory_sync.py     # 背包同步（player_try_add_object）
├── relationship_sync.py  # 关系同步（get/set_relationship_score）
├── buy_sync.py           # Buy 家具同步（set_location + yaw_to_quaternion）
├── lobby.py              # 大厅管理（成员/房间码/准备）
└── debug.py              # 调试工具（mp_debug / mp_self / mp_log）
```

## 游戏 API 参考（反编译确认）

> 以下 API 均从游戏 `.pyc` 反编译验证（`uncompyle6` + `dis`），2026-08-05

### Sim 操作

| API | 用途 | 确认文件 |
|:----|:-----|:---------|
| `sim.push_super_affordance(affordance, target, context)` | 执行交互（做饭/聊天/睡觉） | `si_state.pyc` |
| `sim.queue.add_interaction(interaction)` | 加入交互队列 | `si_state.pyc` |
| `sim.location` | 获取 Sim 位置 | `sim.pyc` |
| `sim.set_location(location)` | 设置 Sim 位置 | `sim.pyc` |
| `sim.routing_component` | 路由组件 | `sim.pyc` |

### 背包

| API | 用途 | 确认文件 |
|:----|:-----|:---------|
| `sim.inventory_component` | 获取背包组件 | `sim.pyc` |
| `inv.player_try_add_object(obj)` | 添加物品 | `inventory.pyc` |
| `inv.try_remove_object_by_id(obj_id)` | 移除物品 | `inventory.pyc` |
| `inv._items` | 物品列表（迭代） | `inventory.pyc` |

### 关系

| API | 用途 | 确认文件 |
|:----|:-----|:---------|
| `sim.relationship_tracker` | 获取关系追踪器 | `sim.pyc` |
| `tracker.get_relationship_score(target_id)` | 获取关系分数 | `relationship_tracker.pyc` |
| `tracker.set_relationship_score(target_id, value)` | 设置关系分数 | `relationship_tracker.pyc` |
| `tracker.add_relationship_bit(target_id, bit, force_add=True)` | 添加关系位 | `relationship_tracker.pyc` |

### 对象

| API | 用途 | 确认文件 |
|:----|:-----|:---------|
| `obj.location` | 获取对象位置 | `objects.pyc` |
| `obj.set_location(location)` | 设置对象位置 | `objects.pyc` |
| `obj.yaw_to_quaternion(yaw)` | 偏航角转四元数 | `objects.pyc` |
| `services.object_manager().create_new_object(obj)` | 创建新对象 | `object_manager.pyc` |

### 服务

| API | 用途 |
|:----|:-----|
| `services.sim_info_manager()` | Sim 信息管理（获取所有 Sim） |
| `services.object_manager()` | 对象管理 |
| `services.get_instance_manager(Types.OBJECT)` | 对象定义管理 |
| `services.active_household()` | 当前家庭 |
| `services.time_service()` | 时间服务（速度/暂停） |

### 游戏时间

| API | 用途 |
|:----|:-----|
| `services.time_service().sim_now` | 当前游戏时间 |
| `services.game_clock_service().set_clock_speed(speed)` | 设置速度（0=暂停, 1=正常, 2=2x, 3=3x） |

## 命令注册

mod 注册的游戏内命令（前缀 `mp_`）：

| 命令 | 用途 |
|:------|:-----|
| `mp_apply` | 读取启动器配置 → 自动连接（host/join） |
| `mp_connect <ip>` | 手动连接到指定 IP |
| `mp_host` | 手动启动主机模式 |
| `mp_sync` | 手动启动同步 |
| `mp_self` | 显示自己的 Sim ID |
| `mp_debug` | 切换调试日志 |
| `mp_cmd` | 发送自定义命令 |
| `mp_ready` / `mp_unready` | 准备/取消准备 |
| `mp_leave` | 离开房间 |
| `mp_status` | 查看房间状态 |

## 事件监听流程

```
游戏事件（Sim 开始做饭）
    ↓
Mod 监听器（interaction_sync.py）
    ↓
构造 Packet（{type, sim_id, interaction, target}）
    ↓
network.send() → 帧协议 → TCP → 房主
    ↓
房主 process_message → 广播 → 其他客户端
    ↓
其他客户端 receive → 解析 → interaction_sync.apply()
    ↓
sim.push_super_affordance(...) → 对端小人开始做饭
```

## 回环抑制

**问题**：房主发送交互 → 广播给所有人 → 房主自己也收到 → 重复执行

**解决**：消息带 `sender_pid` → 收到后检查 `if sender_pid == my_pid: return`（3s 窗口缓存）

## 迟滞阈值

**问题**：A 端需求 82 → 广播给 B → B 端需求 80 → 差异 2 → B 广播修正 → A 收到 → 差异 2 → A 再广播 → 双向震荡

**解决**：差异 <阈值（需求 5 / 关系 15 / 金钱 10）→ 不触达广播；接收端差异 >阈值才应用

## 兼容性

| 特性 | 最低版本 |
|:-----|:--------|
| 基础联机 | TS4 1.108+ |
| HMAC 安全 | 游戏 Python 3.7+（内置 `hashlib`） |
| pickle 协议 | Python 3.7 内置 |

## 开发环境

| 工具 | 用途 |
|:-----|:-----|
| `uncompyle6` | 反编译游戏 `.pyc` 文件 |
| `dis` | 字节码分析（确认方法签名） |
| `mp_debug.log` | mod 运行时日志 |
| `mp_debug` 命令 | 切换详细日志 |

## 打包

| 工具 | 用途 |
|:-----|:-----|
| `build.sh` | 打包 `.ts4script`（zip 压缩） |
| PyInstaller | 打包启动器 `.exe`（onedir 模式） |