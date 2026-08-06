# -*- coding: utf-8 -*-
"""M3d 虚拟测试：房间系统（lobby）协议验证

验证点:
1. 房主创建房间 → 成员列表含自己
2. 客户端 hello → 加入成员列表
3. 客户端 ready → 状态更新 + all_clients_ready
4. 房主 syncsave 需全部已准备
5. ack 收集 → save_sync_done → start_granted
6. 进图门槛：未全进图 → start 被拒
"""
import sys
import os
import json

# ---- Mock 游戏环境 ----
class _Commands:
    CommandType = type("CT", (), {"Live": 1, "Cheat": 2})
    def Command(self, name, command_type=None):
        def deco(fn):
            return fn
        return deco
    def CheatOutput(self, conn):
        return lambda msg: print("  [cheat] " + str(msg))

class _Sims4:
    commands = _Commands()

sys.modules["sims4"] = _Sims4()
sys.modules["sims4.commands"] = _Sims4.commands
sys.modules["services"] = type("services", (), {})
sys.modules["autonomy"] = type("autonomy", (), {})
sys.modules["autonomy.settings"] = type("settings", (), {})

# ---- 导入被测模块 ----
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from multimod import network

# network 打桩
network._log = lambda msg: print("  [log] " + str(msg))
network._notify = lambda msg: print("  [notify] " + str(msg))
network._is_host = True
network._my_player_id = 0
network._client_socket = object()  # 非 None（模拟已连接）
network._clients = {}
network._broadcast = lambda payload, exclude=None: print("  [broadcast] " + json.dumps(payload, ensure_ascii=False)[:120])
network._send_json = lambda sock, payload: print("  [send] " + json.dumps(payload, ensure_ascii=False)[:120])
network._network_thread = None

from multimod import lobby

pass_count = 0
fail_count = 0

def check(name, cond):
    global pass_count, fail_count
    if cond:
        pass_count += 1
        print("  ✅ " + name)
    else:
        fail_count += 1
        print("  ❌ " + name)

# ---- 测试 1: 房主创建房间 ----
print("\n[测试1] 房主创建房间")
lobby.add_host_self()
check("成员列表含房主", 0 in lobby._members)
check("房主 is_host=True", lobby._members[0]["is_host"])

# ---- 测试 2: 客户端 hello 加入 ----
print("\n[测试2] 客户端加入")
network._clients[1] = (object(), ("192.168.0.162", 12345))
lobby.process_message({"type": "hello", "name": "测试机"}, sender_pid=1)
check("成员列表含客户端", 1 in lobby._members)
check("客户端名正确", lobby._members[1]["name"] == "测试机")
check("客户端 is_host=False", lobby._members[1]["is_host"] == False)

# ---- 测试 3: 准备状态 ----
print("\n[测试3] 准备状态")
check("未准备时 all_clients_ready=False", lobby.all_clients_ready() == False)
lobby.on_ready(1, True)
check("准备后 all_clients_ready=True", lobby.all_clients_ready() == True)
lobby.on_ready(1, False)
check("取消后 all_clients_ready=False", lobby.all_clients_ready() == False)

# ---- 测试 4: 同步存档需全部准备 ----
print("\n[测试4] 同步存档门槛")
ok = lobby.host_start_save_sync()
check("未准备时 syncsave 被拒", ok == False)
lobby.on_ready(1, True)
ok = lobby.host_start_save_sync()
check("全准备后 syncsave 成功", ok == True)
check("phase=waiting_ack", lobby._save_sync_phase == "waiting_ack")

# ---- 测试 5: ack 收集 → done ----
print("\n[测试5] 存档同步完成")
check("未确认前 start_granted=False", lobby._start_granted == False)
lobby.on_save_sync_ack(1)
check("全部确认后 phase=done", lobby._save_sync_phase == "done")
check("全部确认后 start_granted=True", lobby._start_granted == True)

# ---- 测试 6: 进图门槛 ----
print("\n[测试6] 开始游戏门槛")
ok = lobby.host_start_game()
check("成员未进图 → start 被拒", ok == False)
lobby.on_in_lot(1, True)
ok = lobby.host_start_game()
check("成员已进图 → start 成功", ok == True)

# ---- 测试 7: 房主进图检查（门槛通知）----
print("\n[测试7] 房主进图门槛")
lobby.on_in_lot(1, False)  # 成员退出
# 模拟房主进图 → 应通知等待
print("  (房主进图，成员未进 → 应输出等待通知)")
lobby.report_my_in_lot(True)
check("成员未进图 → all_clients_in_lot=False", lobby.all_clients_in_lot() == False)

print("\n" + "=" * 40)
print("结果: {} 通过, {} 失败".format(pass_count, fail_count))
sys.exit(1 if fail_count else 0)
