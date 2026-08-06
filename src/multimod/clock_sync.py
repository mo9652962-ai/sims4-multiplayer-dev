# -*- coding: utf-8 -*-
"""Sims4Multiplayer M3c - 时间同步（主机掌控，对齐 S4MP clock_manager）

功能目标（对齐 S4MP clock 逻辑）:
- 主机掌控时间：客机不能自己暂停/开始（对齐 "You must wait for the other players before starting time!"）
- 主机的速度变化 → 广播给客机 → 客机应用相同速度
- 效果：两端游戏时间永远一致，只有主机能控制开始/暂停/倍速

S4MP 反编译确认（2026-08-04）:
- ClockSpeedMode: PAUSED=0, NORMAL=1, SPEED2=2, SPEED3=3
- GameClock.set_clock_speed 被 Override：未 ready 时拦截 + 弹通知
- clock.request_pause/unrequest_pause 命令客机端被吞
- 主机 TravelFinished 后 set_clock_speed(NORMAL) 恢复

我们的简化实现（无旅行）:
- 双端 hook GameClock.set_clock_speed
- 主机：调用原函数 + 广播 {type: "clock", speed: N}
- 客机：非来自主机的速度修改 → 拦截 + 通知；来自主机的 → 应用
- 命令 mp_clock: 查看/开关时间同步
"""

import sims4.commands

from multimod import network

# ============ 全局状态 ============
_clock_sync_enabled = True   # mp_clock 开关
_is_host = False             # 当前角色（由 mp_host/mp_join 设置）
_applying_remote = False     # 正在应用主机广播的速度（避免拦截自己）

# ============ GameClock.set_clock_speed Hook ============
_orig_set_clock_speed = None


def _install_clock_hook():
    """替换 GameClock.set_clock_speed（双端安装）"""
    global _orig_set_clock_speed
    if _orig_set_clock_speed is not None:
        return  # 已安装
    try:
        from clock import GameClock, ClockSpeedMode  # noqa: F401
        _orig_set_clock_speed = GameClock.set_clock_speed

        def _patched_set_clock_speed(self, speed, source=None, reason="", immediate=False):
            global _applying_remote
            try:
                if _clock_sync_enabled:
                    if _is_host:
                        # 主机：正常执行 + 广播给客机
                        result = _orig_set_clock_speed(self, speed, source=source, reason=reason, immediate=immediate)
                        network._send_clock_broadcast(speed)
                        return result
                    else:
                        # 客机：只允许应用主机广播的速度，其余拦截
                        if _applying_remote:
                            # 来自主机的广播 → 放行
                            return _orig_set_clock_speed(self, speed, source=source, reason=reason, immediate=immediate)
                        # 客机自己点暂停/播放 → 拦截 + 通知
                        if _orig_set_clock_speed is not None:
                            # 恢复原来的速度（不改变）
                            pass
                        network._log("clock intercept: 客机试图修改速度 speed={}".format(speed))
                        network._notify("⏸ 时间由主机控制，请让主机开始/暂停")
                        return False
                # 未启用同步 → 原逻辑
                return _orig_set_clock_speed(self, speed, source=source, reason=reason, immediate=immediate)
            except Exception as e:
                network._log("clock hook error: {}".format(e))
                try:
                    return _orig_set_clock_speed(self, speed, source=source, reason=reason, immediate=immediate)
                except Exception:
                    return False

        GameClock.set_clock_speed = _patched_set_clock_speed
        network._log("clock hook installed")
    except Exception as e:
        network._log("clock hook install failed: {}".format(e))


def set_host_flag(is_host):
    """由 network.mp_host/mp_join 调用，设置当前角色"""
    global _is_host
    _is_host = is_host
    network._log("clock role: {}".format("host" if is_host else "client"))


def apply_remote_clock(speed):
    """客机应用主机广播的速度（在 alarm 主线程调用）"""
    global _applying_remote
    try:
        from clock import ClockSpeedMode
        import services
        gcs = services.get_game_clock_service()
        if gcs is None:
            network._log("clock apply FAIL: game_clock_service is None")
            return
        current = int(gcs.clock_speed)
        target = int(speed)
        if current == target:
            return  # 已一致
        network._log("clock apply: speed {} -> {}".format(current, target))
        _applying_remote = True
        try:
            mode = ClockSpeedMode(target)
            gcs.set_clock_speed(mode)
            network._log("clock applied OK: speed={}".format(target))
        except Exception as e:
            network._log("clock apply set_clock_speed FAIL: {}".format(e))
        finally:
            _applying_remote = False
    except Exception as e:
        network._log("apply remote clock error: {}".format(e))


def process_message(data):
    """处理 clock 类型消息（由 network._process_incoming 分发）"""
    if data.get("type") == "clock":
        speed = data.get("speed")
        if speed is not None:
            apply_remote_clock(speed)


@sims4.commands.Command('mp_clock', command_type=sims4.commands.CommandType.Live)
def mp_clock(action="status", _connection=None):
    """时间同步控制（M3c，主机掌控时间）

    mp_clock on     开启时间同步（默认开）
    mp_clock off    关闭时间同步
    mp_clock status 查看状态
    """
    global _clock_sync_enabled
    output = sims4.commands.CheatOutput(_connection)
    action = str(action).lower().strip()

    if action == "on":
        _clock_sync_enabled = True
        output("时间同步已开启（主机掌控时间）")
        network._log("mp_clock on")
        return
    if action == "off":
        _clock_sync_enabled = False
        output("时间同步已关闭")
        network._log("mp_clock off")
        return

    # status
    try:
        import services
        gcs = services.get_game_clock_service()
        speed = int(gcs.clock_speed) if gcs else -1
    except Exception:
        speed = -1
    output("时间同步: {} | 角色: {} | 当前速度: {}".format(
        "开" if _clock_sync_enabled else "关",
        "主机" if _is_host else "客机",
        speed))
    network._log("mp_clock status: enabled={} is_host={} speed={}".format(
        _clock_sync_enabled, _is_host, speed))


# ============ v9.17: 登录快照支持 ============
def get_current_speed():
    """返回当前游戏速度（快照用）"""
    try:
        from sims4 import services
        return services.get_game_clock_service().clock_speed
    except Exception:
        return None
