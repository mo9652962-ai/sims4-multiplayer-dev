# -*- coding: utf-8 -*-
"""Sims4Multiplayer v8.4 - 需求/技能同步

研究: sim_info.get_all_commodities() + set_value()（modthesims 确认）
功能: 广播本机 sim 的关键需求（饥饿/精力/娱乐等）与技能等级
      → 接收端应用（同一存档同家庭，需求应一致）
命令:
  mp_statssync  开始/停止需求技能同步
"""
import time

import sims4.commands

try:
    from multimod import network
except Exception:
    network = None

SYNC_INTERVAL = 8.0  # 需求变化较慢，8s 检查一次
# 关键需求（饥饿/精力/娱乐/卫生/社交/如厕）
KEY_COMMODITIES = {
    "hunger": 0x0002F7CD,  # hunger
    "energy": 0x0002F7CE,  # energy
    "fun": 0x0002F7CF,     # fun
    "social": 0x0002F7D0,  # social
    "hygiene": 0x0002F7D1, # hygiene
    "bladder": 0x0002F7D2, # bladder
}
_last_values = {}
_running = False


def _get_my_sim_info():
    """获取本机控制的 sim_info"""
    try:
        import services  # v9.21.1: 真实游戏无 sims4.services，顶层 services 才是游戏模块
        mgr = services.sim_info_manager()
        if mgr is None:
            return None
        # 找当前活跃 sim
        for sim_info in mgr.get_all():
            if sim_info is None:
                continue
            # 活跃 sim 通常有 commodity_tracker
            tracker = getattr(sim_info, "commodity_tracker", None)
            if tracker is not None:
                return sim_info
        return None
    except Exception:
        return None


def _collect_stats(sim_info):
    """收集关键需求值 + 技能值"""
    stats = {}
    try:
        tracker = getattr(sim_info, "commodity_tracker", None)
        if tracker is not None:
            for name, ctype in KEY_COMMODITIES.items():
                try:
                    c = tracker.get_statistic(ctype)
                    if c is not None:
                        stats[name] = round(float(c.get_value()), 1)
                except Exception:
                    pass
        # 技能（烹饪/魅力/健身/逻辑/绘画——常见联机游玩技能）
        stat_tracker = getattr(sim_info, "statistic_tracker", None)
        if stat_tracker is not None:
            skill_ids = {
                "cooking": 0x0000002D,
                "charisma": 0x00000030,
                "fitness": 0x00000037,
                "logic": 0x00000031,
                "painting": 0x00000034,
            }
            for name, sid in skill_ids.items():
                try:
                    s = stat_tracker.get_statistic(sid)
                    if s is not None:
                        stats["skill_" + name] = round(float(s.get_value()), 1)
                except Exception:
                    pass
    except Exception:
        pass
    return stats


def _broadcast_stats():
    """广播需求/技能（变化时）"""
    global _last_values
    try:
        sim_info = _get_my_sim_info()
        if sim_info is None:
            return
        stats = _collect_stats(sim_info)
        if not stats:
            return
        # 只在有变化时广播
        changed = False
        for k, v in stats.items():
            if abs(_last_values.get(k, -999) - v) > 5.0:
                changed = True
                break
        if not changed and _last_values:
            return
        _last_values = stats
        payload = {"type": "stats_sync", "stats": stats, "ts": time.time()}
        if network is None:
            return
        if network._is_host:
            network._broadcast(payload)
        else:
            network._send_json(network._client_socket, payload)
    except Exception:
        pass


def process_message(data):
    """处理需求同步消息（接收端应用）"""
    try:
        if data.get("type") != "stats_sync":
            return
        stats = data.get("stats")
        if not stats:
            return
        import services  # v9.21.1: 真实游戏无 sims4.services，顶层 services 才是游戏模块
        mgr = services.sim_info_manager()
        if mgr is None:
            return
        for sim_info in mgr.get_all():
            tracker = getattr(sim_info, "commodity_tracker", None)
            if tracker is None:
                continue
            applied = 0
            for name, val in stats.items():
                if name.startswith("skill_"):
                    continue  # 技能只读不写（避免冲突）
                ctype = KEY_COMMODITIES.get(name)
                if ctype is None:
                    continue
                try:
                    c = tracker.get_statistic(ctype)
                    if c is not None and abs(float(c.get_value()) - val) > 15.0:
                        c.set_value(val)
                        applied += 1
                except Exception:
                    pass
            if applied > 0 and network:
                network._log("stats sync: 应用 {} 个需求".format(applied))
            return
    except Exception as e:
        if network:
            network._log("stats sync error: {}".format(e))


def _stats_loop():
    """需求检查循环"""
    global _running
    while _running:
        try:
            _broadcast_stats()
        except Exception:
            pass
        time.sleep(SYNC_INTERVAL)


@sims4.commands.Command('mp_statssync', command_type=sims4.commands.CommandType.Live)
def mp_statssync(_connection=None):
    """开始/停止需求技能同步"""
    global _running, _last_values
    if _running:
        _running = False
        if network:
            network._notify("需求同步已停止")
        return
    _running = True
    _last_values = {}
    import threading
    threading.Thread(target=_stats_loop, daemon=True).start()
    if network:
        network._notify("需求同步已开始 (每 {}s 检查)".format(SYNC_INTERVAL))
        network._log("stats sync started")
