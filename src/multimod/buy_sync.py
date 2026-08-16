# -*- coding: utf-8 -*-
"""Sims4Multiplayer v9.17 - Buy 家具同步（阶段 5 后半，竞品实验性功能）

研究（2026-08-05）:
- 竞品行为：S4MP "buy mode fully works for everyone; furnishing, rotating"
  + SimSync FAQ "Buy mode works but experimental; host controls items"
- 对象位置 API：object.location / object.set_location(location)（S4MP live_drag 同款）
- 只同步 Buy（家具放置/旋转/移动），**不做 Build（墙/地板/房间）**——两家竞品都不做

功能:
  轮询地段内对象的位置/旋转变化（排除 sim 和地板/墙体类）
  → 变化广播 {type:"object_pos", obj_id, x, y, z, rot}
  → 接收端 object.set_location + rotation
  → 双方看到家具被移动/旋转/摆放一致

命令:
  mp_buysync  开始/停止 Buy 同步
"""
import threading
import time

import sims4.commands

try:
    from multimod import network
except Exception:
    network = None

SYNC_INTERVAL = 1.0       # 家具变化中等，1s 轮询
MOVE_THRESHOLD = 0.05     # 位置变化 >5cm 或旋转变化 >2° 才广播

_running = False
_thread = None
_last_objs = {}           # obj_id -> (x, y, z, rot)
_IGNORED_TYPES = ("Floor", "Wall", "Stairs", "Roof", "Fence", "Column")  # 建筑类不同步


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


def _iter_lot_objects():
    """遍历地段内对象（排除 sim）"""
    try:
        import services  # v9.21.1: 真实游戏无 sims4.services，顶层 services 才是游戏模块
        zone = services.current_zone()
        if zone is None:
            return []
        objs = []
        for obj in zone.object_manager.get_objects():
            if obj is None:
                continue
            # 排除 sim（sim 由位置同步管）
            if getattr(obj, "is_sim", False):
                continue
            # 排除建筑类
            cls_name = obj.__class__.__name__
            if any(t in cls_name for t in _IGNORED_TYPES):
                continue
            objs.append(obj)
        return objs
    except Exception:
        return []


def _obj_state(obj):
    """提取对象位置/旋转 → (x, y, z, rot) 或 None"""
    try:
        pos = getattr(obj, "position", None)
        if pos is None:
            return None
        rot = getattr(obj, "rotation", None)
        rot_z = 0.0
        if rot is not None:
            try:
                rot_z = float(rot.yaw_pitch_yaw[0]) if hasattr(rot, "yaw_pitch_yaw") else float(rot.z)
            except Exception:
                rot_z = 0.0
        return (float(pos.x), float(pos.y), float(pos.z), rot_z)
    except Exception:
        return None


def _broadcast_loop():
    global _running
    while _running:
        try:
            for obj in _iter_lot_objects():
                obj_id = getattr(obj, "id", 0)
                state = _obj_state(obj)
                if state is None:
                    continue
                prev = _last_objs.get(obj_id)
                if prev is None or _moved(prev, state):
                    _last_objs[obj_id] = state
                    _broadcast(obj_id, state)
            # 清理不存在的对象
            if len(_last_objs) > 500:
                _last_objs.clear()
        except Exception:
            pass
        for _ in range(int(SYNC_INTERVAL / 0.05)):
            if not _running:
                return
            time.sleep(0.05)


def _moved(prev, cur):
    dx = abs(prev[0] - cur[0]) + abs(prev[1] - cur[1]) + abs(prev[2] - cur[2])
    drot = abs(prev[3] - cur[3])
    return dx >= MOVE_THRESHOLD or drot >= 0.035  # ~2°


def _broadcast(obj_id, state):
    if network is None:
        return
    try:
        msg = {"type": "object_pos", "obj_id": obj_id,
               "x": state[0], "y": state[1], "z": state[2], "rot": state[3],
               "ts": time.time()}
        if network._is_host:
            network._broadcast(msg)
        elif network._client_socket is not None:
            network._send_json(network._client_socket, msg)
    except Exception:
        pass


def process_message(data):
    """收到远端对象位置 → 移动本地对象"""
    try:
        obj_id = int(data.get("obj_id", 0))
        if not obj_id:
            return
        import services  # v9.21.1: 真实游戏无 sims4.services，顶层 services 才是游戏模块
        obj = services.object_manager().get(obj_id)
        if obj is None:
            return
        x, y, z = float(data.get("x", 0)), float(data.get("y", 0)), float(data.get("z", 0))
        rot = float(data.get("rot", 0))
        # 移动
        from sims4.math import Vector3
        loc = obj.location.clone(translation=Vector3(x, y, z))
        obj.set_location(loc)
        # 旋转
        if abs(rot) > 0.001:
            try:
                from sims4.math import Quaternion, yaw_to_quaternion
                obj.set_rotation(yaw_to_quaternion(rot))
            except Exception:
                pass
    except Exception as e:
        if network is not None:
            try:
                network._log("buy_sync apply error: {}".format(e))
            except Exception:
                pass


@sims4.commands.Command('mp_buysync', command_type=sims4.commands.CommandType.Live)
def mp_buysync(*args, _connection=None):
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
    _out("mp_buysync: Buy 家具同步已{}".format("开启" if _running else "关闭"), _connection)


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
