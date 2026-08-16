# -*- coding: utf-8 -*-
"""Sims4Multiplayer v9.17 - 交互队列事件同步（🥇 一起生活的核心体验）

研究（2026-08-05）:
- interactions/si_state.pyc 反编译确认: sim.si_state + sim.queue 读交互,
  sim.push_super_affordance(super_affordance, target, context) 执行交互
  （支持传字符串交互名或 tuning 实例，内部构造 AOP + test_and_execute）
- 竞品 SimSync/S4MP 都是"同步操作"模型: 检测交互启动 → 广播 → 对端执行同款交互

功能:
  检测本机 sim 的交互启动（做饭/聊天/打扫/阅读...）
  → 广播 {type:"interaction", sim_id, affordance_guid, target_id}
  → 对端 push_super_affordance 执行同款交互
  → 双方屏幕上看到同一个交互动作（对方小人在做饭/聊天）

回环抑制（关键）:
  对端 push 的交互会在本端触发同样的检测 → 又广播回去
  → _applied_ts 记录本端 push 的交互（抑制窗口 3s），轮询时跳过

命令:
  mp_intsync  开始/停止交互同步（默认随 mp_sync 自动带起）
"""
import threading
import time

import sims4.commands

try:
    from multimod import network
except Exception:
    network = None

SYNC_INTERVAL = 0.5       # 交互变化快，0.5s 轮询
SUPPRESS_SEC = 3.0        # 回环抑制窗口（本端 push 的交互 3s 内不广播）

_running = False
_thread = None
# sim_id -> set((guid64, target_id))  已广播的交互（去重）
_seen_interactions = {}
# sim_id -> ts  本端 push 的交互时间戳（回环抑制）
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


# ---------------- 收集本机交互 ----------------
def _get_my_sim():
    """获取本机控制的 active sim 实例（无则 None）"""
    try:
        import services  # v9.21.1: 真实游戏无 sims4.services，顶层 services 才是游戏模块
        client = services.client_manager().get_first_client()
        if client is None:
            return None
        return client.active_sim
    except Exception:
        return None


def _iter_sims():
    """遍历家庭内所有有 si_state 的 sim 实例"""
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
            if sim is not None and getattr(sim, "si_state", None) is not None:
                sims.append(sim)
        return sims
    except Exception:
        return []


def _interaction_key(si):
    """提取交互的 (affordance_guid, target_id) —— guid 唯一标识交互类型"""
    try:
        aff = getattr(si, "affordance", None)
        if aff is None:
            return None
        guid = getattr(aff, "guid64", None)
        if guid is None:
            # 兜底: tuning 实例字符串名（push_super_affordance 也接受字符串）
            guid = str(aff)
        target = getattr(si, "target", None)
        target_id = getattr(target, "id", 0)
        return (guid, target_id)
    except Exception:
        return None


def collect_running_interactions(sim):
    """收集 sim 当前运行中的交互 → [(key, sim_id)]"""
    result = []
    try:
        si_state = getattr(sim, "si_state", None)
        if si_state is None:
            return result
        sim_id = getattr(getattr(sim, "sim_info", None), "id", 0)
        for si in si_state.sis_actor_gen():
            try:
                key = _interaction_key(si)
                if key is None:
                    continue
                # 只同步非临时/非打断类交互（防止把"被推"的交互回环）
                result.append((key, sim_id))
            except Exception:
                continue
    except Exception:
        pass
    return result


# ---------------- 广播 ----------------
def _broadcast_loop():
    global _running
    while _running:
        try:
            sims = _iter_sims()
            for sim in sims:
                sim_id = getattr(getattr(sim, "sim_info", None), "id", 0)
                if sim_id in _applied_ts and time.time() - _applied_ts[sim_id] < SUPPRESS_SEC:
                    continue  # 回环抑制：本端刚 push 的交互不广播
                seen = _seen_interactions.setdefault(sim_id, set())
                for key, sid in collect_running_interactions(sim):
                    if key not in seen:
                        seen.add(key)
                        # 清理：seen 集超 100 个时清空（防无限增长）
                        if len(seen) > 100:
                            seen.clear()
                        _broadcast_interaction(sid, key)
        except Exception:
            pass
        # 等一个短周期（线程可中断）
        for _ in range(int(SYNC_INTERVAL / 0.05)):
            if not _running:
                return
            time.sleep(0.05)


def _broadcast_interaction(sim_id, key):
    if network is None:
        return
    guid, target_id = key
    try:
        if network._is_host:
            network._broadcast({"type": "interaction", "sim_id": sim_id,
                                "affordance": guid, "target_id": target_id,
                                "ts": time.time()})
        elif network._client_socket is not None:
            network._send_json(network._client_socket,
                               {"type": "interaction", "sim_id": sim_id,
                                "affordance": guid, "target_id": target_id,
                                "ts": time.time()})
    except Exception:
        pass


# ---------------- 接收应用 ----------------
def process_message(data):
    """收到对端交互 → 在本地执行同款交互"""
    try:
        sim_id = int(data.get("sim_id", 0))
        guid = data.get("affordance")
        target_id = int(data.get("target_id", 0))
        if not guid:
            return
        # 找到目标 sim
        import services  # v9.21.1: 真实游戏无 sims4.services，顶层 services 才是游戏模块
        sim_info = services.sim_info_manager().get(sim_id)
        if sim_info is None:
            return
        sim = sim_info.get_sim_instance()
        if sim is None or getattr(sim, "si_state", None) is None:
            return
        # 目标对象（可能是对象/Sim）
        target = None
        if target_id:
            target = services.object_manager().get(target_id)
            if target is None:
                target = services.sim_info_manager().get(target_id)
                if target is not None:
                    target = target.get_sim_instance()
        # 执行交互
        if isinstance(guid, int):
            aff = services.get_instance_manager(sims4.resources.Types.INTERACTION).get(guid)
        else:
            aff = guid
        if aff is None:
            return
        from interactions.context import InteractionContext
        ctx = InteractionContext(sim, InteractionContext.SOURCE_SCRIPT,
                                 InteractionContext.PRIORITY_MEDIUM)
        sim.push_super_affordance(aff, target, ctx)
        _applied_ts[sim_id] = time.time()  # 回环抑制
        network._log("interaction_sync: applied {} on sim {}".format(aff, sim_id))
    except Exception as e:
        if network is not None:
            try:
                network._log("interaction_sync apply error: {}".format(e))
            except Exception:
                pass


# ---------------- 命令 ----------------
@sims4.commands.Command('mp_intsync', command_type=sims4.commands.CommandType.Live)
def mp_intsync(*args, _connection=None):
    global _running, _thread
    _out("MP 交互同步: {}".format("开" if not _running else "关"))
    if _running:
        _running = False
        if _thread is not None:
            _thread.join(timeout=2)
            _thread = None
    else:
        _running = True
        _thread = threading.Thread(target=_broadcast_loop, daemon=True)
        _thread.start()
    _out("mp_intsync: 交互同步已{}".format("开启" if _running else "关闭"), _connection)


def start():
    """随 mp_sync 自动带起（幂等）"""
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_broadcast_loop, daemon=True)
    _thread.start()


def stop():
    global _running
    _running = False
