# -*- coding: utf-8 -*-
"""Sims4Multiplayer v9.19 - 事件系统（参考 S4MP EventHook + NetworkClient 生命周期）

S4MP 反编译确认 (2026-08-06):
  - EventHook: += 注册 / -= 取消 / fire(*args) 触发
  - NetworkClient.on_connect_finished: 连接完成事件
  - NetworkClient.on_close: 连接关闭事件  
  - CoreServicesHooks.on_tick: 游戏主循环 tick（不依赖 TimeService！）

用途: 替代 alarm_add_real_time → 解决主菜单阶段 time_service=None 的 alarm error
"""
import time as _time


class EventHook:
    """S4MP 同款事件钩子 —— 解耦注册/触发
    
    用法:
        on_connect = EventHook()
        on_connect += my_handler       # 注册
        on_connect.fire(player_id)     # 触发所有注册的回调
        on_connect -= my_handler       # 注销
    """
    def __init__(self):
        self._handlers = []

    def __iadd__(self, handler):
        if handler not in self._handlers:
            self._handlers.append(handler)
        return self

    def __isub__(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
        return self

    def fire(self, *args, **kwargs):
        for handler in self._handlers[:]:  # 拷贝一份，防止 handler 内部注销自己
            try:
                handler(*args, **kwargs)
            except Exception:
                pass  # 静默忽略，防止一个 handler 崩溃影响其他

    def clear(self):
        self._handlers.clear()


# ============ 全局连接生命周期事件 ============
on_host_started = EventHook()       # 主机启动（含 player_id）
on_client_connected = EventHook()   # 客户端连接成功（含 player_id）
on_disconnected = EventHook()       # 断开连接（含 player_id）

# ============ tick 驱动循环（替代 alarm） ============
_tick_handlers = []                 # [(interval_sec, last_call_ts, callback), ...]
_tick_registered = False
_tick_handle = None


def _on_game_tick():
    """游戏每 tick 调用（注册到 core_services on_tick 或 alarm）"""
    global _tick_handlers
    now = _time.time()
    for i, (interval, last_call, callback) in enumerate(_tick_handlers):
        if now - last_call >= interval:
            _tick_handlers[i] = (interval, now, callback)
            try:
                callback()
            except Exception:
                pass  # 静默忽略单个回调错误


def register_tick(interval_sec, callback):
    """注册周期性回调（替代 add_alarm_real_time）
    
    Args:
        interval_sec: 间隔秒数（如 0.5 = 500ms）
        callback: 无参回调函数
    
    回调在游戏主循环中执行，不依赖 TimeService。
    """
    global _tick_handlers
    item = (float(interval_sec), 0.0, callback)
    if item not in _tick_handlers:
        _tick_handlers.append(item)
        from multimod import network
        network._log("tick registered: interval={}s total={}".format(
            interval_sec, len(_tick_handlers)))


def unregister_tick(callback):
    """注销周期性回调"""
    global _tick_handlers
    _tick_handlers = [(i, l, c) for (i, l, c) in _tick_handlers if c is not callback]


def ensure_tick_loop():
    """确保 tick 循环已启动（用 alarm 作为后备——on_tick API 需要通过 EA Override 注入）"""
    global _tick_registered, _tick_handle
    if _tick_registered:
        return
    try:
        import alarms
        from date_and_time import TimeSpan
        _tick_handle = alarms.add_alarm_real_time(
            _tick_handle,  # 复用 owner
            TimeSpan(500),  # 500ms
            lambda _: (_on_game_tick(), True)[1],  # 返回 True 保持循环
            repeating=True,
            cross_zone=True,
        )
        _tick_registered = True
        from multimod import network
        network._log("tick loop started (alarm fallback, 500ms)")
    except Exception as e:
        from multimod import network
        network._log("tick loop error: {}".format(e))
        _tick_handle = None
