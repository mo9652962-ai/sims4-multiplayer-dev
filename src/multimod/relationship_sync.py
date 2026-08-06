# -*- coding: utf-8 -*-
"""Sims4Multiplayer v9.17 - 关系同步（🥉 依赖交互同步先做，否则关系变化少）

研究（2026-08-05）:
- relationships/relationship_tracker.pyc 反编译成功确认:
  get_relationship_score / set_relationship_score / add_relationship_score
  get_all_bits / has_bit / add_relationship_bit(force_add) / remove_relationship_bit
- 关系是双向的（A→B 和 B→A）→ 两边都同步
- 关系 bits（朋友/恋人/家人等）用 tuning 资源 ID 标识

功能:
  轮询家庭内 sim 两两之间的关系（分数 + bits）
  → 变化广播 {type:"relationship", sim_a, sim_b, score, track, bits:[...]}
  → 接收端 set_relationship_score + add_relationship_bit
  → 双方看到的关系一致（A 和 B 成为朋友，两边都显示）

命令:
  mp_relsync  开始/停止关系同步
"""
import threading
import time

import sims4.commands

try:
    from multimod import network
except Exception:
    network = None

SYNC_INTERVAL = 5.0       # 关系变化慢，5s 轮询
SCORE_THRESHOLD = 2.0     # 分数变化 >2 才广播
_APPLY_THRESHOLD = 3.0    # 接收端分数差异 >3 才应用（防互相打架）

_running = False
_thread = None
_last_scores = {}         # (a,b) -> score 上次广播的分数
_last_bits = {}           # (a,b) -> frozenset(bits)


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


def _family_sim_ids():
    """家庭内所有 sim_info id 列表"""
    try:
        from sims4 import services
        hh = services.active_household()
        if hh is None:
            return []
        return [si.id for si in hh.sim_info_gen() if si is not None]
    except Exception:
        return []


def _read_relationship(sim_info_a, sim_info_b):
    """读 A→B 关系 → (score, bits_tuple)"""
    try:
        tracker = getattr(sim_info_a, "relationship_tracker", None)
        if tracker is None:
            return (0.0, ())
        score = tracker.get_relationship_score(sim_info_b.id) or 0.0
        bits = []
        for bit in tracker.get_all_bits(sim_info_b.id):
            try:
                bits.append(getattr(bit, "guid64", str(bit)))
            except Exception:
                continue
        return (float(score), tuple(sorted(str(b) for b in bits)))
    except Exception:
        return (0.0, ())


def _broadcast_loop():
    global _running
    while _running:
        try:
            ids = _family_sim_ids()
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    a, b = ids[i], ids[j]
                    from sims4 import services
                    sia = services.sim_info_manager().get(a)
                    sib = services.sim_info_manager().get(b)
                    if sia is None or sib is None:
                        continue
                    # A→B
                    score, bits = _read_relationship(sia, sib)
                    key = (a, b)
                    prev_s = _last_scores.get(key)
                    prev_b = _last_bits.get(key)
                    if prev_s is None or abs(score - prev_s) >= SCORE_THRESHOLD or prev_b != bits:
                        _last_scores[key] = score
                        _last_bits[key] = bits
                        _broadcast(a, b, score, bits)
                    # B→A（关系双向）
                    score2, bits2 = _read_relationship(sib, sia)
                    key2 = (b, a)
                    prev_s2 = _last_scores.get(key2)
                    prev_b2 = _last_bits.get(key2)
                    if prev_s2 is None or abs(score2 - prev_s2) >= SCORE_THRESHOLD or prev_b2 != bits2:
                        _last_scores[key2] = score2
                        _last_bits[key2] = bits2
                        _broadcast(b, a, score2, bits2)
        except Exception:
            pass
        for _ in range(int(SYNC_INTERVAL / 0.05)):
            if not _running:
                return
            time.sleep(0.05)


def _broadcast(sim_a, sim_b, score, bits):
    if network is None:
        return
    try:
        msg = {"type": "relationship", "sim_a": sim_a, "sim_b": sim_b,
               "score": score, "bits": list(bits), "ts": time.time()}
        if network._is_host:
            network._broadcast(msg)
        elif network._client_socket is not None:
            network._send_json(network._client_socket, msg)
    except Exception:
        pass


def process_message(data):
    """收到远端关系 → 应用到本地 sim_a 的 tracker"""
    try:
        sim_a = int(data.get("sim_a", 0))
        sim_b = int(data.get("sim_b", 0))
        score = float(data.get("score", 0.0))
        bits = data.get("bits", [])
        if not sim_a or not sim_b:
            return
        from sims4 import services
        sia = services.sim_info_manager().get(sim_a)
        sib = services.sim_info_manager().get(sim_b)
        if sia is None or sib is None:
            return
        tracker = getattr(sia, "relationship_tracker", None)
        if tracker is None:
            return
        # 分数（差异 > 阈值才应用，防双向打架）
        cur = tracker.get_relationship_score(sim_b) or 0.0
        if abs(float(cur) - score) >= _APPLY_THRESHOLD:
            tracker.set_relationship_score(sim_b, score)
        # 关系 bits（缺失的补上）
        cur_bits = set()
        for bit in tracker.get_all_bits(sim_b):
            try:
                cur_bits.add(str(getattr(bit, "guid64", str(bit))))
            except Exception:
                continue
        for b in bits:
            if str(b) not in cur_bits:
                try:
                    bit_inst = services.get_instance_manager(
                        sims4.resources.Types.STATISTIC).get(int(b) if str(b).isdigit() else b)
                    if bit_inst is not None:
                        tracker.add_relationship_bit(sim_b, bit_inst, force_add=True)
                except Exception:
                    continue
        network._log("relationship_sync: applied {}<->{} score={}".format(sim_a, sim_b, score))
    except Exception as e:
        if network is not None:
            try:
                network._log("relationship_sync apply error: {}".format(e))
            except Exception:
                pass


@sims4.commands.Command('mp_relsync', command_type=sims4.commands.CommandType.Live)
def mp_relsync(*args, _connection=None):
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
    _out("mp_relsync: 关系同步已{}".format("开启" if _running else "关闭"), _connection)


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
