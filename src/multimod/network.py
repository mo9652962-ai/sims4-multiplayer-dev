# -*- coding: utf-8 -*-
"""Sims4Multiplayer M1 - 调试版网络层

修复目标 (2026-08-03 第二轮):
1. 命令参数不带默认值（游戏命令系统解析可能失败）
2. 用 Cheat 类型（Live 在某些版本不可用）
3. 所有命令写文件日志（不依赖控制台输出，方便排障）
"""

import sims4.commands
import threading
import socket
import json
import queue
import os
import time
import binascii  # v9.15: CRC32 帧校验（Ethernet FCS 标准）
import hmac as _hmac_mod  # v9.16: HMAC 消息签名（RFC 2104——跨网防伪造/篡改）
import hashlib

# ============ 配置 ============
DEFAULT_PORT = 7655
BUF_SIZE = 4096
LOG_PATH = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts",
                        "The Sims 4", "Mods", "mp_debug.log")


def _log(msg):
    """写调试日志到 Mods 目录（游戏内 print 不可见，文件最可靠）"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass
    try:
        print("[MP]", msg)
    except Exception:
        pass


_log("=== mod loaded ===")

# ============ 全局状态 ============
_network_thread = None
_is_host = False
_peer_address = None
_client_socket = None        # 客户端模式：本机到主机的连接
_server_socket = None
_my_player_id = 0            # 本机 player_id（主机=0，客机=主机分配）
_clients = {}                # 主机模式：player_id -> (sock, addr) 多客户端管理
_clients_lock = threading.Lock()  # v9.3: 多客户端共享状态锁（研究: 并发连接需 Lock）
_client_connecting = False   # v9.20.4: 客机防双连接——mp_join/auto-apply 双触发竞态
_next_player_id = 1          # 主机分配 player_id（0 = 房主自己）
_released_pids = []          # v9.12: 释放的 player_id 复用池（防重连 ID 递增→KeyError）

# v9.12: 协议版本协商（研究: MCP handshake / QUIC RFC 9368 兼容协商）
# 握手时客户端发 PROTO_VERSION，host 校验：不兼容 → version_mismatch 拒绝（防旧 mod 连新 host 错乱）
PROTO_VERSION = 2            # 协议版本：1=JSON行协议时代, 2=pickle帧协议(当前)

_incoming_queue = queue.Queue()
# v9.3: 帧大小上限（研究: websockets max_payload——防恶意超大帧内存 DoS）
# pickle 帧长度前缀是 8 字节，恶意对端可声明 2^64 长度导致内存耗尽；
# 正常消息 < 4MB（存档块 64KB×分块），8MB 上限足够且安全
MAX_FRAME_SIZE = 8 * 1024 * 1024
LOBBY_STATE_PATH = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts",
                                "The Sims 4", "Mods", "mp_lobby_state.json")


class _AlarmOwner(object):
    """alarm owner 需要可弱引用的对象（str 不可弱引用）"""
    pass


_ALARM_OWNER = _AlarmOwner()


def _notify(text):
    """游戏内弹通知（S4MP 同款: sim_info + distributor 直发）"""
    try:
        import services
        client = services.client_manager().get_first_client()
        if client is None:
            _log("notify skipped: no client")
            return
        sim_info = client.active_sim_info
        if sim_info is None:
            _log("notify skipped: no active sim_info")
            return
        from sims4.localization import LocalizationHelperTuning
        import ui.ui_dialog_notification as notif
        dialog = notif.UiDialogNotification.TunableFactory().default(
            sim_info,
            title=lambda *args, **kwargs: LocalizationHelperTuning.get_raw_text(text),
        )
        import omega
        from distributor.ops import GenericProtocolBufferOp
        from protocolbuffers import Distributor_pb2, DistributorOps_pb2, Consts_pb2
        from protocolbuffers.DistributorOps_pb2 import Operation
        msg = dialog.build_msg(icon_override=None)
        op = GenericProtocolBufferOp(Operation.UI_NOTIFICATION_SHOW, msg)
        view = Distributor_pb2.ViewUpdate()
        entry = view.entries.add()
        entry.primary_channel.id.manager_id = 0
        entry.primary_channel.id.object_id = 0
        op_msg = DistributorOps_pb2.Operation()
        op.write(op_msg)
        entry.operation_list.operations.append(op_msg)
        omega.send(services.get_first_client().id, Consts_pb2.MSG_OBJECTS_VIEW_UPDATE, view.SerializeToString())
        _log("notify sent: {}".format(text[:30]))
    except Exception as e:
        _log("notify failed: {}".format(e))


def _process_incoming():
    """处理收到的消息（游戏主线程调用）"""
    while True:
        try:
            item = _incoming_queue.get_nowait()
        except queue.Empty:
            break
        try:
            if isinstance(item, tuple):
                msg, sender_pid = item
            else:
                msg, sender_pid = item, None
            # v7.2: pickle 协议直接给 dict（不再是 JSON 字符串）
            data = msg if isinstance(msg, dict) else json.loads(msg)
            mtype = data.get("type")
            # v9.20: 消息对账日志（借鉴 S4MP RECV/DONE 成对模式——调试时能精确对账
            # 每条消息的到达与处理完成；默认关闭防刷屏）
            if TRACE_MESSAGES:
                _log("RECV - {} from Player#{}".format(mtype, sender_pid))
            # v9.13: batch 帧拆开逐个处理（研究: 消息合并——TCP 小消息批处理）
            if mtype == "batch":
                for sub in data.get("msgs", []):
                    if isinstance(sub, dict):
                        _incoming_queue.put((sub, sender_pid))
                continue
            if mtype == "chat":
                _notify("[{}] {}".format(data.get("from", "peer"), data.get("text", "")))
                # v9.0: 写入 lobby state → 启动器聊天页显示
                try:
                    from multimod import lobby
                    lobby.add_chat(data.get("from", "?"), data.get("text", ""))
                except Exception:
                    pass
                _log("chat from {}: {}".format(data.get("from"), data.get("text")))
            elif mtype == "ping":
                # v9.14: 应用层 ping（研究: 游戏 RTT 测量——ping 回 pong 带时间戳）
                try:
                    ts = data.get("ts")
                    if ts:
                        # 回 pong：host 回给对应 client / client 回给 host
                        if _is_host and sender_pid is not None and sender_pid in _clients:
                            _send_json(_clients[sender_pid][0], {"type": "pong", "ts": ts})
                        elif not _is_host and _client_socket is not None:
                            _send_json(_client_socket, {"type": "pong", "ts": ts})
                    _log("PING received")
                except Exception:
                    pass
            elif mtype == "pong":
                # v9.14: 收到 pong → 计算 RTT（滚动平均）
                try:
                    ts = data.get("ts")
                    if ts:
                        rtt = (time.time() - ts) * 1000.0  # ms
                        if 0 < rtt < 5000:
                            _record_rtt(rtt)
                except Exception:
                    pass
            elif mtype == "sim_pos":
                # 位置同步消息：交给 sync 模块处理（延迟导入避免循环依赖）
                try:
                    from multimod import sync
                    sync.process_message(data)
                except Exception as e:
                    _log("sync process error: {}".format(e))
            elif mtype == "money_sync":
                # v8.2: 金钱同步（研究: household.funds API）
                try:
                    from multimod import money_sync
                    money_sync.process_message(data)
                except Exception as e:
                    _log("money sync err: {}".format(e))
            elif mtype == "stats_sync":
                # v8.4: 需求/技能同步（研究: commodity_tracker API）
                try:
                    from multimod import stats_sync
                    stats_sync.process_message(data)
                except Exception as e:
                    _log("stats sync err: {}".format(e))
            elif mtype in ("travel_req", "travel_ack", "travel_go",
                           "travel_arrived", "travel_all_arrived", "travel_missing"):
                # v8.3 + v9.11: 旅行双端确认 + 场景切换抵达报告
                # (研究: S4MP 0.5.2 无限加载修复 / SimSync travel together)
                try:
                    from multimod import lobby
                    if mtype == "travel_req":
                        lobby.on_travel_req(data)
                    elif mtype == "travel_ack":
                        lobby.on_travel_ack(sender_pid)
                    elif mtype == "travel_go":
                        lobby.on_travel_go(data)
                    elif mtype == "travel_arrived":
                        lobby.on_travel_arrived(sender_pid, data.get("zone_id"))
                    elif mtype == "travel_all_arrived":
                        lobby.on_travel_all_arrived(data)
                    else:
                        lobby.on_travel_missing(data)
                except Exception as e:
                    _log("travel err: {}".format(e))
            elif mtype == "clock":
                # 时间同步消息（M3c）：主机广播速度 → 客机应用
                try:
                    from multimod import clock_sync
                    clock_sync.process_message(data)
                except Exception as e:
                    _log("clock process error: {}".format(e))
            elif mtype == "mood":
                # 心情同步消息（v7.1）：广播 mood → 接收端应用
                try:
                    from multimod import mood_sync
                    mood_sync.process_message(data)
                except Exception as e:
                    _log("mood process error: {}".format(e))
            elif mtype == "interaction":
                # v9.17: 交互队列事件同步（🥇 一起生活的核心体验）
                # (研究: si_state.pyc 反编译确认 push_super_affordance)
                try:
                    from multimod import interaction_sync
                    interaction_sync.process_message(data)
                except Exception as e:
                    _log("interaction sync err: {}".format(e))
            elif mtype == "inventory":
                # v9.17: 背包同步（🥈 摸对方背包/共享物品）
                try:
                    from multimod import inventory_sync
                    inventory_sync.process_message(data)
                except Exception as e:
                    _log("inventory sync err: {}".format(e))
            elif mtype == "relationship":
                # v9.17: 关系同步（🥉 双方看到的关系一致）
                # (研究: relationship_tracker.pyc 反编译确认 API)
                try:
                    from multimod import relationship_sync
                    relationship_sync.process_message(data)
                except Exception as e:
                    _log("relationship sync err: {}".format(e))
            elif mtype == "object_pos":
                # v9.17: Buy 家具同步（阶段 5 后半——家具放置/旋转/移动）
                # (竞品 S4MP/SimSync 均确认 buy mode 可同步、build mode 不做)
                try:
                    from multimod import buy_sync
                    buy_sync.process_message(data)
                except Exception as e:
                    _log("buy_sync err: {}".format(e))
            elif mtype == "world_snapshot":
                # v9.17: 登录全量快照——加入时立即对齐世界状态
                try:
                    _log("world snapshot received ({} fields)".format(len(data)))
                    if "clock_speed" in data:
                        from multimod import clock_sync
                        clock_sync.apply_remote_clock(int(data["clock_speed"]))
                    if "positions" in data:
                        from multimod import sync
                        sync.process_snapshot(data["positions"])
                    if "funds" in data:
                        from multimod import money_sync
                        money_sync.process_message({"type": "money_sync", "funds": data["funds"]})
                except Exception as e:
                    _log("world_snapshot err: {}".format(e))
            elif mtype in ("hello", "welcome", "lobby", "ready", "in_lot",
                           "save_sync_req", "save_sync_ack", "save_sync_done",
                           "game_start", "members", "members_ack", "start_game",
                           "lobby_join", "heartbeat", "leave", "kicked",
                           "join_rejected", "version_mismatch", "save_chunk",
                           "save_chunk_done", "save_resend_req"):
                # 房间系统消息（M3d）：交给 lobby 模块处理
                try:
                    from multimod import lobby
                    lobby.process_message(data, sender_pid)
                except Exception as e:
                    _log("lobby process error: {}".format(e))
            else:
                _log("unknown message: {}".format(data))
            if TRACE_MESSAGES:
                _log("DONE - {} from Player#{}".format(mtype, sender_pid))
        except Exception as e:
            _log("process error: {}".format(e))


# ============ v9.16: HMAC 消息签名（研究: RFC 2104 / HKDF RFC 5869） ============
# 跨网场景（UPnP/STUN）防伪造/篡改：握手 nonce 交换 → 派生会话密钥 → 帧带 HMAC
# 帧格式: [8长度][4CRC32][32 HMAC-SHA256][pickle]（44 字节头）
# 安全: Encrypt-then-MAC 顺序（先算 pickle 数据 → HMAC → 发送；接收端先验 HMAC 再反序列化）
_hmac_keys = {}        # pid → 会话密钥（per-client，多客户端各自独立）

# v9.19.1: 调试开关——允许本机 127.0.0.1 多客户端连接（虚拟测试用）。
# 真实游戏场景保持默认关闭（防 auto-apply 自连接风暴）。
ALLOW_SELF = bool(os.environ.get("SIMSYNC_ALLOW_SELF"))

# v9.20: 消息对账日志开关（借鉴 S4MP RECV/DONE 成对模式）。
# 设为 True 时每条消息打 RECV/DONE 两行（调试对账用，默认关防刷屏）。
TRACE_MESSAGES = os.environ.get("SIMSYNC_TRACE_MESSAGES") == "1"
_my_nonce = b""        # 本端 nonce（握手时生成）
_peer_nonce = b""      # 对端 nonce


def _gen_nonce():
    """生成握手 nonce（随机 16 字节 hex）"""
    return os.urandom(16).hex().encode()


def _derive_hmac_key(room_password, client_nonce, host_nonce):
    """派生会话密钥（研究: HKDF RFC 5869——主密钥 → 会话密钥）

    key = HMAC-SHA256(room_password, client_nonce + host_nonce)
    双方用相同输入 → 相同 key（无需传输密钥，防中间人伪造）
    """
    secret = str(room_password).encode("utf-8") if room_password else b""
    material = client_nonce + host_nonce
    return _hmac_mod.new(secret, material, hashlib.sha256).digest()


def _sock_to_pid(sock):
    """反查 socket → player_id（多客户端时按目标选 key）"""
    with _clients_lock:
        for _pid, (_sock, _addr) in list(_clients.items()):
            if _sock is sock:
                return _pid
    return None


def _sign_frame(payload_data, key=None):
    """对帧数据签名（无 key 返回 32 字节零签名占位——帧头恒 44 字节）

    v9.16 修正: 无 key 也填满 32 字节（hello/welcome 握手阶段），
    否则帧头长度不固定 → 收端固定偏移解析错位
    """
    if not key:
        return b"\x00" * 32
    return _hmac_mod.new(key, payload_data, hashlib.sha256).digest()


def _verify_frame(payload_data, signature, key=None):
    """验签（无 key 或签名空 → 通过；有 key 必须匹配）"""
    if not key:
        return True  # 未握手（hello/welcome 阶段）
    if not signature:
        return False  # 有 key 但消息无签名 → 拒绝
    expected = _sign_frame(payload_data, key)
    return _hmac_mod.compare_digest(signature, expected)


# ============ v9.14: 连接健康监控（研究: RTT 测量 / Cloudflare 质量评分） ============
# 应用层 ping/pong 测 RTT → 滚动平均 → 健康评分（0-100）
_rtt_samples = []            # 最近 RTT 样本（ms）
_rtt_lock = threading.Lock()
_last_ping_ts = 0            # 上次主动 ping 时间
PING_INTERVAL = 3.0          # 主动 ping 间隔（秒）


def _record_rtt(rtt_ms):
    """记录 RTT 样本（滚动窗口 20 个）"""
    with _rtt_lock:
        _rtt_samples.append(rtt_ms)
        if len(_rtt_samples) > 20:
            _rtt_samples.pop(0)


def get_rtt_ms():
    """当前 RTT（最近样本平均，无样本返回 None）"""
    with _rtt_lock:
        if not _rtt_samples:
            return None
        return sum(_rtt_samples) / len(_rtt_samples)


def _send_ping():
    """主动发 ping（带时间戳，对端回 pong 测 RTT）"""
    global _last_ping_ts
    now = time.time()
    if now - _last_ping_ts < PING_INTERVAL:
        return
    _last_ping_ts = now
    try:
        if _is_host:
            _broadcast({"type": "ping", "ts": now})
        elif _client_socket is not None:
            _send_json(_client_socket, {"type": "ping", "ts": now})
    except Exception:
        pass


def get_health_score():
    """连接健康评分 0-100（研究: Cloudflare/PingPlotter 质量模型）

    评分 = 100 - RTT 扣分 - 丢失扣分
    - RTT < 50ms: 优秀；50-150: 良好；>300: 差
    """
    rtt = get_rtt_ms()
    if rtt is None:
        return None
    # RTT 扣分（0-60 分）：50ms 内不扣，每超 5ms 扣 1
    rtt_penalty = max(0, int((rtt - 50) / 5)) if rtt > 50 else 0
    rtt_penalty = min(rtt_penalty, 60)
    # 样本太少视为不稳定（小扣分）
    with _rtt_lock:
        samples = len(_rtt_samples)
    if samples < 3:
        rtt_penalty = min(rtt_penalty + 10, 70)
    score = max(0, 100 - rtt_penalty)
    return score


def get_health_label():
    """健康等级（启动器/诊断显示）"""
    score = get_health_score()
    if score is None:
        return "未测量"
    if score >= 85:
        return "🟢 优秀"
    if score >= 70:
        return "🟡 良好"
    if score >= 50:
        return "🟠 一般"
    return "🔴 差"


# ============ 自动消息处理 (alarm 循环, 游戏主线程) ============
_ALARM_INTERVAL_MS = 500
_process_alarm_handle = None


def _process_alarm_callback(_alarm_handle):
    """周期检查消息队列 + 进图状态上报 + 启动器命令桥 + 心跳检查（必须主线程）"""
    _process_incoming()
    _send_ping()  # v9.14: 主动 ping 测 RTT（连接健康监控）
    _check_lot_status()  # M3d: 进图门槛检测
    _check_launcher_cmd()  # M3d: 启动器按钮 → 游戏内命令桥
    _check_heartbeats()  # M3d 增强: 主机心跳超时检测
    return True  # 持续循环


def _on_tick_callback():
    """v9.19: core_services.on_tick 回调（替代 alarm——S4MP CoreServicesHooks 风格）
    
    由 core_hooks._on_game_tick() 每 500ms 调用一次。
    与 _process_alarm_callback 等效，但不依赖 TimeService。
    """
    _process_incoming()
    _send_ping()
    _check_lot_status()
    _check_launcher_cmd()
    _check_heartbeats()


# ============ M3d 增强: 心跳检查（主机侧，每 500ms 由 alarm 调用） ============
_last_heartbeat_check = 0


def _check_heartbeats():
    """每 5s 调用一次 lobby.check_heartbeats（避免每 500ms 全扫）"""
    global _last_heartbeat_check
    now = time.time()
    if now - _last_heartbeat_check < 5.0:
        return
    _last_heartbeat_check = now
    try:
        if _is_host:
            from multimod import lobby
            lobby.check_heartbeats()
    except Exception as e:
        _log("heartbeat check error: {}".format(e))


# ============ M3d: 启动器命令桥（mp_cmd.json 轮询） ============
# 启动器 GUI 按钮（准备/同步存档/开始）写入 mp_cmd.json，mod 轮询执行
CMD_PATH = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts",
                        "The Sims 4", "Mods", "mp_cmd.json")
_last_cmd_ts = 0


def _check_launcher_cmd():
    """每 500ms 检查启动器命令文件（防重复执行：记录 ts）"""
    global _last_cmd_ts
    try:
        if not os.path.exists(CMD_PATH):
            return
        with open(CMD_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = data.get("ts", 0)
        cmd = data.get("cmd", "")
        if not cmd or ts == _last_cmd_ts:
            return
        _last_cmd_ts = ts
        _log("launcher cmd: {}".format(cmd))
        if cmd == "mp_ready":
            from multimod import lobby
            lobby.report_my_ready(True)
            _notify("已准备!")
        elif cmd == "mp_unready":
            from multimod import lobby
            lobby.report_my_ready(False)
            _notify("已取消准备")
        elif cmd == "mp_syncsave":
            from multimod import lobby
            lobby.host_start_save_sync()
        elif cmd == "mp_start":
            from multimod import lobby
            lobby.host_start_game()
        elif cmd == "mp_leave":
            from multimod import lobby
            lobby.leave_room()
            _notify("已离开房间")
        # 执行后删除，避免残留
        try:
            os.remove(CMD_PATH)
        except Exception:
            pass
    except Exception as e:
        _log("launcher cmd error: {}".format(e))


# ============ M3d: 进图状态检测（alarm 轮询，不用 zone callback——全局单例坑） ============
_last_lot_status = None


def _check_lot_status():
    """每 500ms 检查本机是否在家庭地段（有 active sim = 已进图）"""
    global _last_lot_status
    try:
        in_lot = _has_active_sim()
        if in_lot != _last_lot_status:
            _last_lot_status = in_lot
            _log("lot status changed: in_lot={}".format(in_lot))
            try:
                from multimod import lobby
                lobby.report_my_in_lot(in_lot)
            except Exception as e:
                _log("lobby in_lot error: {}".format(e))
    except Exception as e:
        _log("check lot error: {}".format(e))


def _has_active_sim():
    """是否有 active sim（= 进入家庭地段）"""
    try:
        import services
        cm = services.client_manager()
        if cm is None:
            return False
        client = cm.get_first_client()
        if client is None:
            return False
        return client.active_sim is not None
    except Exception:
        return False


def _ensure_alarm():
    """确保处理 alarm 已启动"""
    global _process_alarm_handle
    try:
        if _process_alarm_handle is not None:
            return
        import alarms
        from date_and_time import TimeSpan
        # owner 不能是 str（AlarmHandle 内部 weakref.ref(owner)，str 不可弱引用）
        # 必须 repeating=True！默认一次性 alarm 触发一次就注销，消息队列没人处理
        # 必须 add_alarm_REAL_TIME！sim_timeline 在游戏暂停时不走，wall_clock 才不受暂停影响
        _process_alarm_handle = alarms.add_alarm_real_time(
            _ALARM_OWNER,
            TimeSpan(_ALARM_INTERVAL_MS),
            _process_alarm_callback,
            repeating=True,
            cross_zone=True,
        )
        _log("alarm started")
    except Exception as e:
        _log("alarm error: {}".format(e))


# alarm 延迟到命令执行时注册（游戏加载早期 TimeService 未初始化，会报 NoneType 错误）
# 由 _ensure_alarm() 在 mp_host / mp_join 中调用
# _ensure_alarm()  # ← 移除启动时注册


def _send_json(sock, payload, prio=1):
    """发送消息（v7.2: pickle 二进制 + 8 字节长度前缀，ts4mp 同款）

    协议: struct.pack('>Q', len(data)) + pickle.dumps(dict)
    相比 JSON 行协议: ~3-5× 快，体积更小，自动处理 Python 类型
    风险: pickle 不可信数据有反序列化风险——本 mod 仅局域网信任对端（同 ts4mp）
    v9.15: 帧加 CRC32 校验（研究: Ethernet FCS——CRC-32 0x04C11DB7 标准）
    v9.16: 帧加 HMAC-SHA256 签名（研究: RFC 2104——跨网防伪造/篡改）
    帧格式: [8长度][4CRC32][32 HMAC][pickle]（44 字节头）
    v9.15: prio 消息优先级（研究: pvigier 多流分离——QoS 分级）
    0=紧急(状态关键,直发) 1=普通(默认) 2=低优(位置等,可批处理)
    """
    try:
        import pickle
        from struct import pack
        data = pickle.dumps(payload)
        crc = binascii.crc32(data) & 0xffffffff
        # v9.16: 按目标选会话密钥签名（多客户端各自独立）
        # host 端: 按目标 pid 反查；client 端: 用自己的 key（socket 不在 host _clients）
        if _is_host:
            _tgt_pid = _sock_to_pid(sock)
            key = _hmac_keys.get(_tgt_pid) if _tgt_pid is not None else None
        else:
            key = _hmac_keys.get(_my_player_id)
        sig = _sign_frame(data, key)
        sock.sendall(pack('>QI', len(data), crc) + sig)
        sock.sendall(data)
        return True
    except Exception as e:
        _log("send error: {}".format(e))
        return False


def _send_batch(sock, payloads):
    """v9.13: 批量发送多条消息（研究: Vanilla Java batching——TCP 小消息合并）

    把多条消息合并成一个 {"type": "batch", "msgs": [...]} 帧发送，
    减少帧头开销和 TCP 包数量（同 tick 多条小消息时吞吐更高）。
    接收端 _process_incoming 拆开逐个处理。
    """
    if not payloads:
        return True
    if len(payloads) == 1:
        return _send_json(sock, payloads[0])
    return _send_json(sock, {"type": "batch", "msgs": payloads})


def _broadcast(payload, exclude=None, tags=None):
    """广播消息给所有客户端（主机模式）或对端（客户端模式）
    
    v9.19: 支持 tags 过滤——配合 local_ops_filter 跳过纯本地事件
    """
    try:
        # v9.19: LOCAL_ONLY_OPS 过滤（参考 S4MP client_config.py）
        if tags:
            module = tags.get("module", "")
            event_type = tags.get("event", "")
            try:
                from multimod import local_ops_filter
                if local_ops_filter.is_local_only(module, event_type):
                    return  # 跳过纯本地事件，不广播
            except Exception:
                pass
        if _is_host:
            for pid, (sock, _addr) in list(_clients.items()):
                if pid == exclude:
                    continue
                try:
                    _send_json(sock, payload)
                except Exception:
                    pass
        else:
            if _client_socket is not None:
                _send_json(_client_socket, payload)
    except Exception as e:
        _log("broadcast error: {}".format(e))


def _send_clock_broadcast(speed):
    """主机广播速度变化给客机（由 clock_sync hook 调用）"""
    try:
        if _is_host:
            _broadcast({"type": "clock", "speed": int(speed)})
        else:
            if _client_socket is not None:
                _send_json(_client_socket, {"type": "clock", "speed": int(speed)})
    except Exception as e:
        _log("clock broadcast error: {}".format(e))


# ============ v7.0: 局域网 UDP 广播发现（研究: UDP broadcast NAT） ============
# 房主每 5s 广播房间存在（含房间码/人数）；加入者监听 → 自动发现房间并填充 IP
DISCOVERY_PORT = 7656           # UDP 发现端口（独立于 TCP 7655）
DISCOVERY_MAGIC = "S4S_DISC"    # 消息魔数防串扰
_discovery_socket = None
_discovery_running = False
_discovered_rooms = {}          # host_ip -> {room_code, players, ts}


def _start_host_discovery():
    """房主开始广播房间存在（每 5s 一次）"""
    global _discovery_socket, _discovery_running
    if _discovery_running:
        return
    _discovery_running = True
    import threading

    def _loop():
        global _discovery_socket
        try:
            _discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            while _discovery_running:
                try:
                    from multimod import lobby
                    payload = json.dumps({
                        "magic": DISCOVERY_MAGIC,
                        "room_code": lobby.ROOM_CODE,
                        "players": len(lobby._members),
                        "name": lobby._get_player_name(),
                        "ts": time.time(),
                    }).encode("utf-8")
                    _discovery_socket.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
                except Exception as e:
                    _log("discovery broadcast error: {}".format(e))
                time.sleep(5)
        except Exception as e:
            _log("discovery loop error: {}".format(e))

    threading.Thread(target=_loop, daemon=True).start()
    _log("host discovery started on UDP :{}".format(DISCOVERY_PORT))


def start_client_discovery():
    """加入者开始监听局域网房间广播"""
    global _discovery_socket, _discovery_running
    if _discovery_running:
        return
    _discovery_running = True
    import threading

    def _listen():
        global _discovery_socket
        try:
            _discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            _discovery_socket.bind(("", DISCOVERY_PORT))
            _discovery_socket.settimeout(1.0)
            while _discovery_running:
                try:
                    data, addr = _discovery_socket.recvfrom(1024)
                    try:
                        msg = json.loads(data.decode("utf-8"))
                        if msg.get("magic") == DISCOVERY_MAGIC:
                            host_ip = addr[0]
                            _discovered_rooms[host_ip] = {
                                "room_code": msg.get("room_code", ""),
                                "players": msg.get("players", 0),
                                "name": msg.get("name", "?"),
                                "ts": time.time(),
                            }
                            _log("discovered room at {}: {} ({}人) code={}".format(
                                host_ip, msg.get("name", "?"), msg.get("players", 0),
                                msg.get("room_code", "")))
                    except Exception:
                        pass
                except socket.timeout:
                    pass
                except Exception as e:
                    _log("discovery listen error: {}".format(e))
        except Exception as e:
            _log("discovery listen fatal: {}".format(e))

    threading.Thread(target=_listen, daemon=True).start()
    _log("client discovery listening on UDP :{}".format(DISCOVERY_PORT))


def get_discovered_rooms():
    """返回发现的房间列表（供启动器显示）"""
    return {ip: v for ip, v in _discovered_rooms.items() if time.time() - v.get("ts", 0) < 20}


def _recv_loop(sock, is_client=False, player_id=None):
    """接收循环（v7.2: 长度前缀帧协议，pickle 反序列化）

    帧格式: 8字节大端长度 + pickle 数据
    """
    global _client_socket  # v9.19.1: 函数内赋值 _client_socket=None 使其成为局部变量，需显式 global
    import pickle
    from struct import unpack
    buf = b""
    while True:
        try:
            chunk = sock.recv(BUF_SIZE)
            if not chunk:
                _log("connection closed{}".format("" if player_id is None else " pid={}".format(player_id)))
                break
            buf += chunk
            # 处理完整帧（可能一包多帧 / 一帧多包）
            while len(buf) >= 44:
                (frame_len, frame_crc) = unpack('>QI', buf[:12])
                frame_sig = buf[12:44]
                # v9.3: 帧大小上限（研究: 防恶意超大帧 DoS——websockets max_payload 模式）
                if frame_len > MAX_FRAME_SIZE:
                    _log("frame too large ({}B), rejecting connection".format(frame_len))
                    try:
                        sock.close()
                    except Exception:
                        pass
                    return
                if len(buf) < 44 + frame_len:
                    break  # 帧未收完
                frame_data = buf[44:44 + frame_len]
                buf = buf[44 + frame_len:]
                # v9.16: HMAC 验签（研究: RFC 2104）——先验签再反序列化（防伪造/篡改）
                if not _verify_frame(frame_data, frame_sig, _hmac_keys.get(player_id)):
                    _log("frame HMAC mismatch ({}B), dropped".format(frame_len))
                    continue
                # v9.15: CRC32 校验（研究: Ethernet FCS）——坏帧丢弃不崩
                if frame_crc != (binascii.crc32(frame_data) & 0xffffffff):
                    _log("frame CRC mismatch ({}B), dropped".format(frame_len))
                    continue
                try:
                    # 只反序列化信任的联机对端数据（局域网/房主-客机场景，
                    # 双方都运行同一个 mod；pickle 用于性能，JSON 已实测慢 2.8x）
                    msg = pickle.loads(frame_data)
                    _incoming_queue.put((msg, player_id))
                except Exception as e:
                    _log("unpickle error: {}".format(e))
        except Exception as e:
            _log("recv error: {}".format(e))
            break
    if is_client:
        # v9.19.1: 仅当断开的就是当前连接才清空/调度重连。
        # 旧 _recv_loop 线程收尾时若已有新连接（手动重连/外部替换），
        # 不能把新 _client_socket 误清，否则新连接会被旧线程"杀死"。
        if _client_socket is sock:
            _client_socket = None
            # v7.0: 断线自动重连（指数退避 1.5s→60s，研究: WebSocket reconnect pattern）
            _log("client disconnected, scheduling reconnect")
            try:
                from multimod import lobby
                lobby.schedule_reconnect_with_backoff(broken_sock=sock)
            except Exception as e:
                _log("reconnect schedule error: {}".format(e))
    elif player_id is not None:
        # 客户端断开 → 从成员列表移除 + 广播房间状态
        # v9.20.4: 必须验证 _clients[player_id] 的 socket 就是当前 sock——
        # 踢旧接新后旧连接线程醒来时 pid 已被新连接复用, 若只按 pid 清理
        # 会把新连接误删 → 客机重连风暴(连接建立后即被旧线程杀死)。
        _log("client disconnected: pid={}".format(player_id))
        disconnected = False
        with _clients_lock:  # v9.3: 并发安全（研究: 多客户端共享状态需 Lock）
            cur = _clients.get(player_id)
            if cur is not None and cur[0] is sock:
                _clients.pop(player_id, None)
                # v9.12: player_id 复用池（重连复用原 ID，防递增→KeyError）
                if player_id not in _released_pids:
                    _released_pids.append(player_id)
                disconnected = True
        if disconnected:
            try:
                from multimod import lobby
                lobby.on_client_disconnect(player_id)
            except Exception as e:
                _log("lobby disconnect error: {}".format(e))


def _handle_client(conn, addr):
    """处理单个客户端连接：握手分配 player_id + 接收消息"""
    global _next_player_id
    try:
        with _clients_lock:  # v9.3: 并发安全（研究: 多客户端共享状态需 Lock）
            # v9.12: player_id 复用（研究: 知识库 S4MP KeyError 教训——重连 ID 递增会崩）
            if _released_pids:
                player_id = _released_pids.pop(0)
            else:
                player_id = _next_player_id
                _next_player_id += 1
            _clients[player_id] = (conn, addr)
        _log("client connected: {} pid={}".format(addr, player_id))
        # 欢迎消息（含 player_id + 协议版本）
        # v9.16: host 生成 host_nonce（握手密钥交换——密钥在收到 hello 后派生）
        global _my_nonce, _peer_nonce
        _my_nonce = _gen_nonce()
        _peer_nonce = b""
        _send_json(conn, {"type": "welcome", "player_id": player_id,
                          "proto_version": PROTO_VERSION,
                          "host_nonce": _my_nonce.decode("utf-8")}, prio=0)
        # 通知 lobby 有新成员
        try:
            from multimod import lobby
            lobby.on_client_connected(player_id, addr)
        except Exception as e:
            _log("lobby connect error: {}".format(e))
        # v9.19: world_snapshot 推迟到 HMAC key 派生后发送（on_hello 中）
        # 提前发送会导致 key 不一致 → HMAC mismatch → 客机收不到初始状态
        _ensure_alarm()
        _recv_loop(conn, is_client=False, player_id=player_id)
    except Exception as e:
        _log("handle client error: {}".format(e))


def _send_world_snapshot(sock):
    """v9.17: 登录全量快照——新客户端加入时立即对齐当前世界状态

    研究（2026-08-05）: Minecraft 登录发 chunk / Unreal late joiner replication /
    Rune stateSync——late join 需要初始全量状态，否则新成员要等各同步模块
    逐个广播（10-30s）才能看到完整世界。
    """
    try:
        snap = {"type": "world_snapshot", "ts": time.time()}
        # 时间速度
        try:
            from multimod import clock_sync
            speed = clock_sync.get_current_speed()
            if speed is not None:
                snap["clock_speed"] = int(speed)
        except Exception:
            pass
        # 各 sim 位置
        try:
            from multimod import sync
            pos = sync.collect_snapshot()
            if pos:
                snap["positions"] = pos
        except Exception:
            pass
        # 家庭资金
        try:
            from multimod import money_sync
            funds = money_sync.collect_snapshot()
            if funds is not None:
                snap["funds"] = int(funds)
        except Exception:
            pass
        _send_json(sock, snap, prio=0)
        _log("world snapshot sent ({}B, {} positions)".format(
            len(snap), len(snap.get("positions", {}))))
    except Exception as e:
        _log("world snapshot error: {}".format(e))


def _server_thread(port=DEFAULT_PORT):
    global _server_socket
    try:
        _server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _server_socket.bind(("0.0.0.0", port))
        _server_socket.listen(5)  # 支持多客户端
        _log("server listening on :{}".format(port))
        _notify("MP 主机已开启 :{} 等待连接...".format(port))
        while True:
            try:
                conn, addr = _server_socket.accept()
                # v9.20.4: 同 IP 去重——客机重连时旧连接可能已死(半开)却占用 _clients,
                # 若拒绝新连接 → 客机永远连不上(重连风暴全部被拒)。
                # 策略: 踢旧接新——关闭旧连接, 释放旧 pid, 接受新连接。
                try:
                    with _clients_lock:
                        dup = [pid for pid, (s, a) in _clients.items() if a[0] == addr[0]]
                    if dup:
                        old_pid = dup[0]
                        _log("duplicate connection from {} (existing pid={}), replacing old".format(addr[0], old_pid))
                        try:
                            old_sock = _clients.get(old_pid, (None,))[0]
                            if old_sock is not None:
                                old_sock.close()
                        except Exception:
                            pass
                        with _clients_lock:
                            _clients.pop(old_pid, None)
                            if old_pid not in _released_pids:
                                _released_pids.append(old_pid)
                        # 继续接受新连接（不 continue）
                except Exception as e:
                    _log("dup check error: {}".format(e))
                # v9.19: 禁止本机自连接（防 auto-apply 连接风暴掩盖真实客机）
                # 仅允许 --allow-self 调试模式时通过
                if addr[0] == "127.0.0.1" and not ALLOW_SELF:
                    if len(_clients) > 0:
                        # 已有客机在连 → 拒绝自连接，避免状态混淆
                        try:
                            conn.close()
                        except Exception:
                            pass
                        continue
                # v8.0: TCP_NODELAY 禁用 Nagle（研究: 低延迟优先于带宽）
                try:
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except Exception:
                    pass
                # v8.9: TCP keepalive（研究: 死连接检测——缩短半开连接回收）
                try:
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    # Windows: SIO_KEEPALIVE_VALS (on, keepalive_time_ms, keepalive_interval_ms)
                    conn.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 3000))
                except Exception:
                    pass
                threading.Thread(target=_handle_client, args=(conn, addr), daemon=True).start()
            except Exception as e:
                _log("accept error: {}".format(e))
                break
    except Exception as e:
        _log("server error: {}".format(e))


def _client_thread(host, port=DEFAULT_PORT):
    global _client_socket, _my_player_id, _client_connecting
    # v9.20.4: 防双连接——mp_join 与 auto-apply 可能同时触发两个连接线程,
    # 后启动的覆盖 _client_socket → 主机端同 IP 双连接 → 旧连接 10053 断开
    # → 客机指向已断连接 → 误判断线。已有连接线程在跑则跳过。
    if _client_connecting:
        _log("client thread: already connecting, skip duplicate")
        return
    _client_connecting = True
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # v8.0: TCP_NODELAY 禁用 Nagle（研究: 低延迟优先于带宽）
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        # v8.9: TCP keepalive（研究: 死连接检测——缩短半开连接回收）
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 3000))
        except Exception:
            pass
        sock.connect((host, port))
        _log("connected to {}:{}".format(host, port))
        # 记录主机 IP（主机迁移重连用）——connect 成功后记录
        try:
            from multimod import lobby
            lobby.set_host_ip(host)
        except Exception:
            pass
        _notify("已连接主机 {}:{}".format(host, port))
        _client_socket = sock
        # hello 消息（带玩家名 + 房间密码，主机验证后加入）
        try:
            import socket as _s
            player_name = _s.gethostname()
        except Exception:
            player_name = "玩家"
        # 从启动器配置读密码 + 玩家名（v9.16: 昵称优先）
        room_password = ""
        try:
            cfg = _load_launcher_config()
            if cfg:
                room_password = cfg.get("password", "")
                if cfg.get("name"):
                    player_name = str(cfg["name"])[:16]
        except Exception:
            pass
        # v9.16: 生成 client nonce（握手密钥交换——研究: HKDF RFC 5869）
        global _my_nonce, _peer_nonce
        _my_nonce = _gen_nonce()
        _peer_nonce = b""
        _send_json(sock, {"type": "hello", "name": player_name, "password": room_password,
                          "proto_version": PROTO_VERSION,
                          "client_nonce": _my_nonce.decode("utf-8")}, prio=0)
        # 同主机侧：连接后确保 alarm（防止 auto-apply 主菜单注册失败后没人处理消息）
        _ensure_alarm()
        _recv_loop(sock, is_client=True)
    except Exception as e:
        _log("connect error: {}".format(e))
        _notify("连接失败: {}".format(e))
        # 记录已知主机 IP（重连调度需要）
        try:
            from multimod import lobby
            lobby.set_host_ip(host)
        except Exception:
            pass
        # v9.11: connect 失败也会调度重连（之前线程直接死亡——
        # 偶发 ECONNREFUSED 会让 client 永久离线，直到手动重连）
        try:
            from multimod import lobby
            lobby.schedule_reconnect_with_backoff()
        except Exception as re_err:
            _log("connect reconnect schedule error: {}".format(re_err))
    finally:
        # v9.20.4: 无论连接成功/失败/断线, 重置防双连接标志 (允许后续重连)
        _client_connecting = False


@sims4.commands.Command('mp_host', command_type=sims4.commands.CommandType.Live)
def mp_host(port=None, visibility="public", _connection=None):
    global _network_thread, _is_host
    _log("mp_host called: port={} visibility={}".format(port, visibility))
    output = sims4.commands.CheatOutput(_connection)
    try:
        port = int(port) if port else DEFAULT_PORT
    except Exception:
        port = DEFAULT_PORT
    # ⚠️ alarm 必须在检查"已在运行"之前注册——auto-apply 可能已启动 host 但
    # 主菜单阶段 alarm 失败；此时游戏已进地段，重试必然成功
    _ensure_alarm()
    if _network_thread and _network_thread.is_alive():
        output("MP 已经在运行")
        return
    _is_host = True
    # 房主把自己加入房间列表（M3d）——从启动器配置读可见性/密码
    try:
        from multimod import lobby
        _my_player_id = 0
        vis = "public"
        pwd = ""
        try:
            cfg = _load_launcher_config()
            if cfg:
                vis = cfg.get("visibility", "public")
                pwd = cfg.get("password", "")
        except Exception:
            pass
        lobby.reset_room_state()  # v9.19: 清理上次残留状态
        lobby.add_host_self(visibility=vis, password=pwd)
    except Exception as e:
        _log("lobby setup error: {}".format(e))
    # M3c: 设置时间同步角色 + 安装 GameClock hook（主机掌控时间）
    try:
        from multimod import clock_sync
        clock_sync.set_host_flag(True)
        clock_sync._install_clock_hook()
    except Exception as e:
        _log("clock_sync setup error: {}".format(e))
    _network_thread = threading.Thread(target=_server_thread, args=(port,), daemon=True)
    _network_thread.start()
    # v9.19: 安装 core_services.on_tick hook（替代 alarm，S4MP 风格）

    try:
        from multimod import core_hooks; core_hooks.install_on_tick_hook()
    except Exception as e:
        _log("on_tick hook install error: {}".format(e))
    output("MP 主机模式启动 (端口 {})".format(port))
    # v7.0: 房主广播房间（局域网自动发现）
    try:
        _start_host_discovery()
    except Exception as e:
        _log("discovery start error: {}".format(e))


@sims4.commands.Command('mp_join', command_type=sims4.commands.CommandType.Live)
def mp_join(host=None, port=None, _connection=None):
    global _network_thread, _is_host
    _log("mp_join called: {}:{}".format(host, port))
    output = sims4.commands.CheatOutput(_connection)
    if not host:
        output("用法: mp_join <主机IP> [端口]")
        return
    try:
        port = int(port) if port else DEFAULT_PORT
    except Exception:
        port = DEFAULT_PORT
    _ensure_alarm()  # 提前注册（理由同上）
    _is_host = False
    # M3c: 设置时间同步角色 + 安装 GameClock hook（客机不能自己改时间）
    try:
        from multimod import clock_sync
        clock_sync.set_host_flag(False)
        clock_sync._install_clock_hook()
    except Exception as e:
        _log("clock_sync setup error: {}".format(e))
    _network_thread = threading.Thread(target=_client_thread, args=(host, port), daemon=True)
    _network_thread.start()
    # v9.19: 安装 core_services.on_tick hook

    try:
        from multimod import core_hooks; core_hooks.install_on_tick_hook()
    except Exception as e:
        _log("on_tick hook install error: {}".format(e))
    output("正在连接 {}:{}".format(host, port))
    # v7.0: 加入者监听局域网房间（自动发现）
    try:
        start_client_discovery()
    except Exception as e:
        _log("discovery start error: {}".format(e))


@sims4.commands.Command('mp_say', command_type=sims4.commands.CommandType.Live)
def mp_say(text=None, _connection=None):
    _log("mp_say called: {}".format(text))
    output = sims4.commands.CheatOutput(_connection)
    if not text:
        output("用法: mp_say <消息>")
        return
    # v9.1: 修复房主发不了消息的 bug——host 用 _broadcast，client 用 _send_json
    if _is_host:
        _broadcast({"type": "chat", "from": "me", "text": text})
    elif _client_socket is None:
        output("未连接! 先 mp_host 或 mp_join")
        return
    else:
        _send_json(_client_socket, {"type": "chat", "from": "me", "text": text})
    # v9.0: 本机消息也写入 lobby state（启动器聊天页显示）
    try:
        from multimod import lobby
        lobby.add_chat("我", text)
    except Exception:
        pass
    output("已发送: {}".format(text))


@sims4.commands.Command('mp_emoji', command_type=sims4.commands.CommandType.Live)
def mp_emoji(emoji=None, _connection=None):
    """快捷表情（v8.4，研究: Signal 常用 emoji 固定条）
    用法: mp_emoji 1~8 或 mp_emoji 😊
    1=😊 2=😂 3=❤️ 4=👍 5=🎉 6=😭 7=🤔 8=👏
    """
    output = sims4.commands.CheatOutput(_connection)
    EMOJIS = {"1": "😊", "2": "😂", "3": "❤️", "4": "👍",
              "5": "🎉", "6": "😭", "7": "🤔", "8": "👏"}
    if not emoji:
        output("快捷表情: " + " ".join("{}={}".format(k, v) for k, v in EMOJIS.items()))
        return
    text = EMOJIS.get(str(emoji).strip(), str(emoji).strip())
    # v9.1: 修复房主发不了表情的 bug（同 mp_say）
    if _is_host:
        _broadcast({"type": "chat", "from": "me", "text": text})
    elif _client_socket is None:
        output("未连接! 先 mp_host 或 mp_join")
        return
    else:
        _send_json(_client_socket, {"type": "chat", "from": "me", "text": text})
    output("已发送: {}".format(text))


@sims4.commands.Command('mp_poll', command_type=sims4.commands.CommandType.Live)
def mp_poll(_connection=None):
    _log("mp_poll called")
    _process_incoming()
    # v9.0: 读取启动器指令文件（聊天发送）——双向通信（研究: 状态文件模式）
    try:
        import os as _os
        cmd_path = _os.path.join(_os.path.expanduser("~"), "Documents", "Electronic Arts",
                                 "The Sims 4", "Mods", "Sims4Multiplayer", "chat_cmd.txt")
        if _os.path.exists(cmd_path):
            with open(cmd_path, "r", encoding="utf-8") as _f:
                lines = [_l.strip() for _l in _f.readlines() if _l.strip()]
            if lines:
                for _line in lines[-5:]:  # 最多发 5 条
                    if _client_socket is not None:
                        _send_json(_client_socket, {"type": "chat", "from": "me", "text": _line})
                        _log("launcher chat: {}".format(_line))
                # 清空（已消费）
                open(cmd_path, "w", encoding="utf-8").close()
    except Exception as e:
        _log("chat cmd error: {}".format(e))
    output = sims4.commands.CheatOutput(_connection)
    output("poll done")


@sims4.commands.Command('mp_status', command_type=sims4.commands.CommandType.Live)
def mp_status(_connection=None):
    _log("mp_status called")
    output = sims4.commands.CheatOutput(_connection)
    output("is_host={} connected={}".format(_is_host, _client_socket is not None))
    output("peer={} thread_alive={}".format(_peer_address,
                                            _network_thread.is_alive() if _network_thread else False))


# ============ 启动器配置自动连接 (M3a, 2026-08-04) ============
LAUNCHER_CONFIG_PATH = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts",
                                    "The Sims 4", "Mods", "mp_launcher_config.json")


def _load_launcher_config():
    """读取启动器写入的连接配置（无则返回 None）"""
    try:
        if not os.path.exists(LAUNCHER_CONFIG_PATH):
            return None
        with open(LAUNCHER_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg
    except Exception as e:
        _log("load launcher config error: {}".format(e))
        return None


def _ensure_alarm_retry():
    """后台线程：持续重试注册 alarm，直到成功（解决主菜单 auto-apply 失败）

    用户进地段后 TimeService 就绪 → 重试成功 → 消息处理循环自动启动
    """
    import threading

    def _retry():
        for _ in range(120):  # 最多 10 分钟
            try:
                _ensure_alarm()
                if _process_alarm_handle is not None:
                    _log("alarm retry: registered successfully")
                    return
            except Exception as e:
                _log("alarm retry error: {}".format(e))
            time.sleep(5)

    threading.Thread(target=_retry, daemon=True).start()


def _apply_launcher_config():
    """执行启动器配置：自动连接 + 自动位置同步

    返回: (bool ok, str desc)
    """
    global _network_thread  # ⚠️ 必须声明！否则赋值被当局部变量 → referenced before assignment
    cfg = _load_launcher_config()
    if not cfg:
        return False, "无启动器配置 (mp_launcher_config.json)"
    mode = cfg.get("mode")
    port = int(cfg.get("port", DEFAULT_PORT))
    if mode == "host":
        if _network_thread and _network_thread.is_alive():
            return False, "MP 已经在运行"
        _is_host = True
        _my_player_id = 0
        # 房主建房间（v6.1 修复: auto-apply 也要建房间，否则房间列表为空）
        try:
            from multimod import lobby
            lobby.reset_room_state()
            lobby.add_host_self(visibility=cfg.get("visibility", "public"),
                                password=cfg.get("password", ""))
        except Exception as e:
            _log("lobby setup error: {}".format(e))
        # M3c: 时间同步角色 + hook（host 掌控时间）
        try:
            from multimod import clock_sync
            clock_sync.set_host_flag(True)
            clock_sync._install_clock_hook()
        except Exception as e:
            _log("clock_sync setup error: {}".format(e))
        _ensure_alarm()
        _network_thread = threading.Thread(target=_server_thread, args=(port,), daemon=True)
        _network_thread.start()
        # v9.19: 安装 core_services.on_tick hook（替代 alarm，S4MP 风格）

        try:
            from multimod import core_hooks; core_hooks.install_on_tick_hook()
        except Exception as e:
            _log("on_tick hook install error: {}".format(e))
        _log("auto host started on :{}".format(port))
        # ⚠️ v5.5 修复：主菜单阶段 alarm 可能注册失败 → 后台重试，
        # 用户进地段后自动补注册（无需手动 mp_host）
        _ensure_alarm_retry()
    elif mode == "join":
        host = cfg.get("host", "")
        if not host:
            return False, "加入模式缺少 host IP"
        if _network_thread and _network_thread.is_alive():
            return False, "MP 已经在运行"
        _is_host = False
        # M3c: 时间同步角色 + hook（client 不能改时间）
        try:
            from multimod import clock_sync
            clock_sync.set_host_flag(False)
            clock_sync._install_clock_hook()
        except Exception as e:
            _log("clock_sync setup error: {}".format(e))
        _ensure_alarm()
        _network_thread = threading.Thread(target=_client_thread, args=(host, port), daemon=True)
        _network_thread.start()
        # v9.19: 安装 core_services.on_tick hook

        try:
            from multimod import core_hooks; core_hooks.install_on_tick_hook()
        except Exception as e:
            _log("on_tick hook install error: {}".format(e))
        _log("auto join {}:{}".format(host, port))
        _ensure_alarm_retry()
    else:
        return False, "未知模式: {}".format(mode)

    # 自动开始位置同步（连接建立后再触发，最多等 60s）
    # ⚠️ v5.4.3 修复：不能固定 sleep(3) —— 主菜单 auto-apply 时连接未建立，
    # mp_sync 提前调用会失败（"未连接"），之后连接成功但广播永不启动。
    # 正确做法：循环等 _client_socket 非空 + _network_thread 存活，再调 mp_sync
    if cfg.get("auto_sync", True):
        def _delayed_sync():
            for _ in range(60):
                if _client_socket is not None and _network_thread is not None and _network_thread.is_alive():
                    break
                time.sleep(1.0)
            else:
                _log("auto sync skipped: 60s 内未建立连接")
                return
            try:
                from multimod import sync
                sync.mp_sync()
                _log("auto sync started")
            except Exception as e:
                _log("auto sync error: {}".format(e))
        threading.Thread(target=_delayed_sync, daemon=True).start()

    return True, "已按配置自动连接 ({})".format(mode)


@sims4.commands.Command('mp_apply', command_type=sims4.commands.CommandType.Live)
def mp_apply(_connection=None):
    """读取启动器配置并连接（启动器一键启动时自动调用；游戏已开时手动输入）"""
    _log("mp_apply called")
    output = sims4.commands.CheatOutput(_connection)
    ok, desc = _apply_launcher_config()
    output(desc)
    _log("mp_apply: {}".format(desc))
