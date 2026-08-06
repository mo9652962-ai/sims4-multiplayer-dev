# -*- coding: utf-8 -*-
"""Sims4Multiplayer v9.19 - core_services.on_tick hook (S4MP CoreServicesHooks style)

S4MP decompile confirmed (2026-08-06):
  CoreServicesHooks replaces sims4.core_services.on_tick
  Every game tick fires EventHook → TimedTask subscribers execute

SimSync usage:
  Replaces add_alarm_real_time (TimeService dependency → alarm errors in main menu)
  Game runs ~30fps → we throttle to 500ms equivalent via timer
"""
import time as _time
from functools import wraps as _wraps

_tick_hook_installed = False
_original_on_tick = None
_last_proc_time = 0
_PROC_INTERVAL = 0.5  # 500ms between processing cycles


def _log(msg):
    try:
        from multimod import network
        network._log(msg)
    except Exception:
        import os
        p = os.path.join(os.path.expanduser("~"), "Documents",
                         "Electronic Arts", "The Sims 4", "Mods", "mp_debug.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(_time.strftime("%H:%M:%S"), msg))


def install_on_tick_hook():
    """安装 core_services.on_tick 钩子（参考 S4MP CoreServicesHooks.setup_hooks）"""
    global _tick_hook_installed, _original_on_tick
    if _tick_hook_installed:
        return
    try:
        from sims4 import core_services
        _original_on_tick = core_services.on_tick

        @_wraps(_original_on_tick)
        def on_tick_with_hooks(*args, **kwargs):
            try:
                _on_game_tick()
            except Exception:
                pass
            return _original_on_tick(*args, **kwargs)

        core_services.on_tick = on_tick_with_hooks
        _tick_hook_installed = True
        _log("core_services.on_tick hook installed (S4MP style)")
    except Exception as e:
        _log("on_tick hook failed: {}".format(e))


def remove_on_tick_hook():
    """恢复原始 on_tick（参考 S4MP CoreServicesHooks.stop_hooks）"""
    global _tick_hook_installed, _original_on_tick
    if not _tick_hook_installed:
        return
    try:
        from sims4 import core_services
        if _original_on_tick is not None:
            core_services.on_tick = _original_on_tick
        _tick_hook_installed = False
        _log("core_services.on_tick hook removed")
    except Exception:
        pass


def _on_game_tick():
    """每次游戏 tick 调用，节流到 500ms 处理周期"""
    global _last_proc_time
    now = _time.time()
    if now - _last_proc_time < _PROC_INTERVAL:
        return
    _last_proc_time = now
    try:
        from multimod import network
        network._on_tick_callback()
    except Exception:
        pass
