# -*- coding: utf-8 -*-
"""Sims4Multiplayer v9.17 - 背包同步（🥈 摸对方背包/共享物品）

研究（2026-08-05）:
- objects/components/inventory.pyc 反编译确认: InventoryComponent
  __iter__/__len__ 读物品 + player_try_add_object/system_add_object 写
  + try_remove_object_by_id 删除
- sims/sim.pyc 确认: sim.inventory_component 属性（SimInventoryComponent）
- 两端同存档 → 物品对象 ID 天然对齐

功能:
  轮询本机控制 sim 的背包物品列表（definition_id -> count）
  → 变化广播 {type:"inventory", sim_id, items:{def_id:count}}
  → 接收端对比 → 缺的补加 / 多的删除
  → 双方背包一致（拿到对方给的物品）

命令:
  mp_invsync  开始/停止背包同步
"""
import threading
import time

import sims4.commands

try:
    from multimod import network
except Exception:
    network = None

SYNC_INTERVAL = 3.0       # 背包变化较慢，3s 轮询
_APPLY_THRESHOLD = 3      # 接收端差异 >3 个物品才应用（防抖动）

_running = False
_thread = None
_last_items = {}          # sim_id -> {def_id: count} 上次广播的快照
_applied_ts = {}


def _out(msg, _connection=None):
    try:
        if _connection is not None:
            sims4.commands.CheatOutput(_connection)(msg)
    except Exception:
        pass
    if network is not None:
        try:
            network._log(msg)
        except Exception:
            pass


def _iter_sims():
    """遍历家庭内所有有背包的 sim 实例"""
    try:
        import services  # v9.21.1: 真实游戏无 sims4.services，顶层 services 才是游戏模块
        hh = services.active_household()
        if hh is None:
            return []
        sims = []
        for sim_info in hh.sim_info_gen():
            if sim_info is None:
                continue
            sim = sim_info.get_sim_instance()
            if sim is not None and getattr(sim, "inventory_component", None) is not None:
                sims.append(sim)
        return sims
    except Exception:
        return []


def collect_inventory(sim):
    """收集 sim 背包 → {def_id: count}"""
    items = {}
    try:
        inv = sim.inventory_component
        for obj in inv:
            try:
                if obj is None:
                    continue
                def_id = getattr(getattr(obj, "definition", None), "id", 0)
                if not def_id:
                    continue
                count = getattr(obj, "stack_count", 1) or 1
                items[def_id] = items.get(def_id, 0) + count
            except Exception:
                continue
    except Exception:
        pass
    return items


def _broadcast_loop():
    global _running
    while _running:
        try:
            for sim in _iter_sims():
                sim_id = getattr(getattr(sim, "sim_info", None), "id", 0)
                items = collect_inventory(sim)
                prev = _last_items.get(sim_id, {})
                if items != prev:
                    _last_items[sim_id] = items
                    _broadcast(sim_id, items)
        except Exception:
            pass
        for _ in range(int(SYNC_INTERVAL / 0.05)):
            if not _running:
                return
            time.sleep(0.05)


def _broadcast(sim_id, items):
    if network is None:
        return
    try:
        msg = {"type": "inventory", "sim_id": sim_id, "items": items, "ts": time.time()}
        if network._is_host:
            network._broadcast(msg)
        elif network._client_socket is not None:
            network._send_json(network._client_socket, msg)
    except Exception:
        pass


def _apply_inventory(sim, remote_items):
    """应用远端背包状态（缺的补、多的删）"""
    try:
        local = collect_inventory(sim)
        # 计算差异
        to_add = {k: v for k, v in remote_items.items() if local.get(k, 0) < v}
        to_remove = {k: v for k, v in local.items() if remote_items.get(k, 0) < v}
        if not to_add and not to_remove:
            return
        if len(to_add) + len(to_remove) < _APPLY_THRESHOLD:
            # 微小差异（可能是本地刚拾取）——反向广播修正而不是删
            _broadcast(getattr(getattr(sim, "sim_info", None), "id", 0), local)
            return
        inv = sim.inventory_component
        import services  # v9.21.1: 真实游戏无 sims4.services，顶层 services 才是游戏模块
        mgr = services.object_manager()
        # 删多余的
        for def_id, count in to_remove.items():
            try:
                for obj in list(inv):
                    if obj is not None and getattr(getattr(obj, "definition", None), "id", 0) == def_id:
                        inv.try_remove_object_by_id(obj.id, count=min(count, getattr(obj, "stack_count", 1) or 1))
                        break
            except Exception:
                continue
        # 补缺的
        for def_id, count in to_add.items():
            try:
                for _ in range(min(count, 5)):  # 单次最多补 5 个防风暴
                    new_obj = services.get_instance_manager(sims4.resources.Types.OBJECT).get(def_id)
                    if new_obj is None:
                        break
                    obj = services.get_instance_manager(sims4.resources.Types.OBJECT).get(def_id)
                    if obj is None:
                        break
                    new = mgr.create_new_object(obj)
                    if new is not None:
                        inv.player_try_add_object(new)
            except Exception:
                break
        network._log("inventory_sync: applied +{} -{} on sim {}".format(
            len(to_add), len(to_remove), getattr(getattr(sim, "sim_info", None), "id", 0)))
    except Exception as e:
        if network is not None:
            try:
                network._log("inventory_sync apply error: {}".format(e))
            except Exception:
                pass


def process_message(data):
    """收到远端背包状态 → 应用"""
    try:
        sim_id = int(data.get("sim_id", 0))
        items = data.get("items", {})
        if not isinstance(items, dict):
            return
        import services  # v9.21.1: 真实游戏无 sims4.services，顶层 services 才是游戏模块
        sim_info = services.sim_info_manager().get(sim_id)
        if sim_info is None:
            return
        sim = sim_info.get_sim_instance()
        if sim is None or getattr(sim, "inventory_component", None) is None:
            return
        _apply_inventory(sim, items)
    except Exception:
        pass


@sims4.commands.Command('mp_invsync', command_type=sims4.commands.CommandType.Live)
def mp_invsync(*args, _connection=None):
    global _running, _thread
    if _running:
        _running = False
        if _thread is not None:
            _thread.join(timeout=2)
            _thread = None
    else:
        _running = True
        _thread = threading.Thread(target=_broadcast_loop, daemon=True)
        _thread.start()
    _out("mp_invsync: 背包同步已{}".format("开启" if _running else "关闭"), _connection)


def start():
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_broadcast_loop, daemon=True)
    _thread.start()


def stop():
    global _running
    _running = False
