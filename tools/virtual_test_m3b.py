# -*- coding: utf-8 -*-
"""M3b 虚拟测试：验证 AI 控制逻辑（mock autonomy API）

验证点:
1. _disable_sim_autonomy 调用 set_setting(LIMITED_ONLY, group) ✓
2. _enable_sim_autonomy 调用 set_setting(UNDEFINED, group) ✓
3. process_message 收到 sim_pos 时自动关 AI（只做一次）✓
4. mp_ai off 跳过 active sim ✓
5. 恢复后 _disabled_autonomy_sims 清空 ✓
"""
import sys
import os

# ---- Mock 游戏环境 ----
class MockAutonomySettings:
    def __init__(self):
        self.calls = []
    def set_setting(self, value, group):
        self.calls.append((value, group))

class MockSim:
    def __init__(self, sim_id, name="测试小人"):
        self._id = sim_id
        def _get_instance(self):
            return sim
        sim = self
        self.sim_info = type("SI", (), {
            "id": sim_id, "full_name": name,
            "get_sim_instance": _get_instance,
        })()
        self.autonomy_settings = MockAutonomySettings()
        self.location = type("Loc", (), {"clone": lambda self, translation: None})()
        self.position = type("Pos", (), {"x": 1.0, "y": 1.0, "z": 1.0})()
    def get_autonomy_settings_group(self):
        return 0  # DEFAULT

# ---- Mock services / autonomy ----
# ---- Mock services / autonomy ----
# ⚠️ type() 创建类的方法会被绑定（self 传入），lambda 必须带 self 参数
sys.modules["services"] = type("services", (), {
    "client_manager": lambda: type("CM", (), {
        "get_first_client": lambda self: type("C", (), {"active_sim": ACTIVE_SIM})()
    })(),
    "sim_info_manager": lambda: type("SIM", (), {
        "get": lambda self, sid: type("SI", (), {"get_sim_instance": lambda self: SIMS.get(sid, None)})()
    })(),
    "current_zone": lambda: type("Z", (), {})(),
    "active_household": lambda: type("HH", (), {
        "sim_info_gen": lambda self: iter([s.sim_info for s in SIMS.values()])
    })(),
})
sys.modules["autonomy"] = type("autonomy", (), {})
sys.modules["autonomy.settings"] = type("settings", (), {
    "AutonomyState": type("AS", (), {"UNDEFINED": -1, "DISABLED": 0, "LIMITED_ONLY": 1, "FULL": 3})
})
# ---- Mock sims4 / commands ----
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

# 全局 sim 表
ACTIVE_SIM = MockSim(100, "我控制的小人")
REMOTE_SIM = MockSim(200, "对方的小人")
SIMS = {100: ACTIVE_SIM, 200: REMOTE_SIM}

# ---- 导入被测模块 ----
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from multimod import sync

# network mock（_log 打印）
class _Net:
    def _log(self, msg):
        print("  [log] " + str(msg))
    _client_socket = object()
    _send_json = lambda *a, **k: None
sync.network = _Net()

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

# ---- 测试 1: disable autonomy 调用正确 ----
print("\n[测试1] _disable_sim_autonomy")
ok = sync._disable_sim_autonomy(REMOTE_SIM)
check("返回 True", ok)
check("调用 set_setting(LIMITED_ONLY=1, group)", REMOTE_SIM.autonomy_settings.calls[-1] == (1, 0))

# ---- 测试 2: enable autonomy 调用正确 ----
print("\n[测试2] _enable_sim_autonomy")
ok = sync._enable_sim_autonomy(REMOTE_SIM)
check("返回 True", ok)
check("调用 set_setting(UNDEFINED=-1, group)", REMOTE_SIM.autonomy_settings.calls[-1] == (-1, 0))

# ---- 测试 3: process_message 自动关 AI（只做一次）----
print("\n[测试3] process_message 自动关远端 AI")
sync._disabled_autonomy_sims.clear()
REMOTE_SIM.autonomy_settings.calls.clear()
sync.process_message({"type": "sim_pos", "sim_id": 200, "position": [5.0, 1.0, 5.0]})
check("sim 200 加入已禁用集合", 200 in sync._disabled_autonomy_sims)
check("调用了 set_setting(LIMITED_ONLY)", REMOTE_SIM.autonomy_settings.calls[-1][0] == 1)

# 第二条消息（位置变化）→ 不再重复设置
calls_before = len(REMOTE_SIM.autonomy_settings.calls)
sync.process_message({"type": "sim_pos", "sim_id": 200, "position": [6.0, 1.0, 5.0]})
check("第二次消息不再重复关 AI", len(REMOTE_SIM.autonomy_settings.calls) == calls_before)

# ---- 测试 4: mp_ai off 跳过 active sim ----
print("\n[测试4] mp_ai off 跳过自己控制的 sim")
sync._disabled_autonomy_sims.clear()
ACTIVE_SIM.autonomy_settings.calls.clear()
REMOTE_SIM.autonomy_settings.calls.clear()
sync.mp_ai("off", _connection=1)
check("active sim(100) 未被禁用", 100 not in sync._disabled_autonomy_sims)
check("remote sim(200) 被禁用", 200 in sync._disabled_autonomy_sims)

# ---- 测试 5: mp_ai on 恢复并清空集合 ----
print("\n[测试5] mp_ai on 恢复所有")
sync.mp_ai("on", _connection=1)
check("集合已清空", len(sync._disabled_autonomy_sims) == 0)
check("remote sim 恢复了 UNDEFINED", REMOTE_SIM.autonomy_settings.calls[-1][0] == -1)

print("\n" + "=" * 40)
print("结果: {} 通过, {} 失败".format(pass_count, fail_count))
sys.exit(1 if fail_count else 0)
