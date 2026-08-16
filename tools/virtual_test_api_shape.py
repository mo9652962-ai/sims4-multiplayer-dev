# -*- coding: utf-8 -*-
"""API 形状校验（v9.22 P0-3）——防"虚拟测试全绿、真实游戏全炸"再次发生

背景: v9.21.1 修复的时钟 bug 根因是代码用了 get_game_clock_service（真实游戏
不存在，正确名 game_clock_service），而虚拟测试 mock 了这个错误名字——测试
跟着 bug 一起"绿"。本测试直接扫描反编译的游戏 pyc 二进制，校验 mod 用到的
API 名真实存在；并反向扫描 mod 源码，禁止引用不存在的模块/函数。

A. services/__init__.pyc 必须含 mod 依赖的全部函数名
B. clock.pyc 必须含 GameClock / ClockSpeedMode / set_clock_speed
C. python/core/sims4/ 下不存在 services 模块（禁 from sims4 import services）
D. mod 源码静态扫描：禁止 get_game_clock_service( 调用（fallback 除外）、
   禁止 from sims4 import services
"""
import os
import re
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
PY = os.path.join(ROOT, "python")
SRC = os.path.join(ROOT, "src", "multimod")

pc = fc = 0
failures = []

def check(name, cond, note=""):
    global pc, fc
    if cond:
        pc += 1
    else:
        fc += 1
        failures.append(name + (" | " + note if note else ""))

def pyc_text(path):
    """读 pyc 二进制里的可打印字符串（名字常量足够）"""
    with open(path, "rb") as f:
        raw = f.read()
    return raw.decode("latin-1")

# ============ A: services 模块函数名 ============
print('[A] services/__init__.pyc 函数名校验')
svc_path = os.path.join(PY, "simulation", "services", "__init__.pyc")
check('services/__init__.pyc 存在', os.path.exists(svc_path))
if os.path.exists(svc_path):
    svc = pyc_text(svc_path)
    for fn in ["game_clock_service", "client_manager", "sim_info_manager",
               "active_household", "current_zone", "current_zone_id", "object_manager"]:
        check('services.{} 存在'.format(fn), fn in svc)

# ============ B: clock 模块 ============
print('[B] clock.pyc 类/方法校验')
clock_path = os.path.join(PY, "simulation", "clock.pyc")
check('clock.pyc 存在', os.path.exists(clock_path))
if os.path.exists(clock_path):
    clk = pyc_text(clock_path)
    for name in ["GameClock", "ClockSpeedMode", "set_clock_speed"]:
        check('clock.{} 存在'.format(name), name in clk)

# ============ C: sims4 包无 services 子模块 ============
print('[C] sims4 包不存在 services 子模块')
sims4_dir = os.path.join(PY, "core", "sims4")
check('sims4 目录存在', os.path.isdir(sims4_dir))
if os.path.isdir(sims4_dir):
    bad = [f for f in os.listdir(sims4_dir) if f.startswith("services")]
    check('sims4/ 下无 services*', not bad, "found: {}".format(bad))

# ============ D: mod 源码静态扫描 ============
print('[D] mod 源码禁止使用不存在的 API')
for fname in os.listdir(SRC):
    if not fname.endswith(".py"):
        continue
    path = os.path.join(SRC, fname)
    with open(path, "r", encoding="utf-8") as f:
        code = f.read()
    # from sims4 import services → 永远 ImportError（真实游戏无此模块）
    if re.search(r"^\s*from\s+sims4\s+import\s+services", code, re.M):
        check('{} 无 from sims4 import services'.format(fname), False)
    # get_game_clock_service( 直接调用（clock_sync 的 fallback getattr 除外）
    for i, line in enumerate(code.splitlines(), 1):
        if "get_game_clock_service" in line:
            is_fallback = ('getattr' in line or 'fallback' in line or
                           line.strip().startswith("#") or '"' in line or "'" in line)
            if not is_fallback:
                check('{}:{} 无 get_game_clock_service 直调'.format(fname, i), False, line.strip())
check('静态扫描完成（违规项已列）', True)

# ============ B2: 位置/心情/交互 API（v9.22.1 扩展）============
print('[B2] math/sim_info/sim API 校验（位置/心情/交互同步依赖）')
math_path = os.path.join(PY, "core", "sims4", "math.pyc")
check('sims4/math.pyc 存在', os.path.exists(math_path))
if os.path.exists(math_path):
    mt = pyc_text(math_path)
    for name in ["Vector3", "Quaternion", "Location", "clone"]:
        check('sims4.math.{} 存在'.format(name), name in mt)
si_path = os.path.join(PY, "simulation", "sims", "sim_info.pyc")
check('sim_info.pyc 存在', os.path.exists(si_path))
if os.path.exists(si_path):
    si = pyc_text(si_path)
    for name in ["get_mood", "get_mood_intensity", "current_mood",
                 "inventory_component", "relationship_tracker",
                 "commodity_tracker", "statistic_tracker",
                 "send_travel_switch_to_zone_op"]:
        check('sim_info.{} 存在'.format(name), name in si)
sim_path = os.path.join(PY, "simulation", "sims", "sim.pyc")
check('sim.pyc 存在', os.path.exists(sim_path))
if os.path.exists(sim_path):
    sm = pyc_text(sim_path)
    for name in ["add_buff", "si_state"]:
        check('sim.{} 存在'.format(name), name in sm)

# ============ 结果 ============
print()
print("=" * 50)
if failures:
    print("失败项:")
    for f_ in failures:
        print("  ❌", f_)
print("结果: {} 通过, {} 失败".format(pc, fc))
sys.exit(1 if fc else 0)
