# -*- coding: utf-8 -*-
"""Sims4Multiplayer v8.2 - 金钱同步（家庭资金）

研究: sims.modify_funds cheat + household.funds API
功能: 房主家庭资金变化 → 广播 → 客机应用（同一存档同家庭，资金应一致）
命令:
  mp_moneysync      开始/停止金钱同步
  mp_money <金额>   广播资金变更（房主用）
"""
import time

import sims4.commands

try:
    from multimod import network
except Exception:
    network = None

SYNC_INTERVAL = 5.0  # 资金检查间隔（秒）
_last_funds = None
_running = False


def _get_funds():
    """获取当前家庭资金（sim_info.household.funds 或 house_hold 资金）"""
    try:
        from sims4 import services
        sim_info_manager = services.sim_info_manager()
        if sim_info_manager is None:
            return None
        # 找本机控制的 sim 所属家庭
        from sims4.resources import Types
        for sim_info in sim_info_manager.get_all():
            if sim_info is None:
                continue
            household = getattr(sim_info, "household", None)
            if household is not None:
                funds = getattr(household, "funds", None)
                if funds is not None:
                    return int(funds)
        return None
    except Exception:
        return None


def _broadcast_funds():
    """广播当前资金（变化时）"""
    global _last_funds
    try:
        funds = _get_funds()
        if funds is None:
            return
        if _last_funds is not None and abs(funds - _last_funds) < 1:
            return  # 无变化不广播
        _last_funds = funds
        payload = {"type": "money_sync", "funds": funds, "ts": time.time()}
        if network is None:
            return
        if network._is_host:
            network._broadcast(payload)
        else:
            network._send_json(network._client_socket, payload)
    except Exception:
        pass


def process_message(data):
    """处理金钱同步消息（客机应用资金）"""
    try:
        if data.get("type") != "money_sync":
            return
        funds = data.get("funds")
        if funds is None:
            return
        from sims4 import services
        sim_info_manager = services.sim_info_manager()
        if sim_info_manager is None:
            return
        for sim_info in sim_info_manager.get_all():
            household = getattr(sim_info, "household", None)
            if household is not None:
                funds_obj = getattr(household, "funds", None)
                if funds_obj is not None:
                    # 只在差异 >50 时应用（避免微小抖动）
                    if abs(int(funds_obj) - int(funds)) > 50:
                        funds_obj.set(int(funds))
                        network._log("money sync: 应用资金 {}".format(funds))
                    return
    except Exception as e:
        if network:
            network._log("money sync error: {}".format(e))


def _money_loop():
    """资金检查循环"""
    global _running
    while _running:
        try:
            _broadcast_funds()
        except Exception:
            pass
        time.sleep(SYNC_INTERVAL)


@sims4.commands.Command('mp_moneysync', command_type=sims4.commands.CommandType.Live)
def mp_moneysync(_connection=None):
    """开始/停止金钱同步"""
    global _running, _last_funds
    if _running:
        _running = False
        if network:
            network._notify("金钱同步已停止")
        return
    _running = True
    _last_funds = None
    import threading
    threading.Thread(target=_money_loop, daemon=True).start()
    if network:
        network._notify("金钱同步已开始 (每 {}s 检查)".format(SYNC_INTERVAL))
        network._log("money sync started")


@sims4.commands.Command('mp_money', command_type=sims4.commands.CommandType.Live)
def mp_money(funds=None, _connection=None):
    """手动广播资金变更（房主用）"""
    global _last_funds
    try:
        _last_funds = None  # 强制广播
        _broadcast_funds()
        if network:
            network._notify("已广播当前资金")
    except Exception as e:
        if network:
            network._log("mp_money error: {}".format(e))


# ============ v9.17: 登录快照支持 ============
def collect_snapshot():
    """返回当前家庭资金（快照用，None=获取失败）"""
    try:
        from sims4 import services
        hh = services.active_household()
        if hh is None:
            return None
        return int(hh.funds)
    except Exception:
        return None
