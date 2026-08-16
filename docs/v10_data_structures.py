# -*- coding: utf-8 -*-
"""SimSync v10 核心数据结构定义（路线图配套权威定义）

对应文档: docs/ROADMAP_V10.md（v9.40 房间系统 / v9.50 mod同步 / v9.60 语音提示 /
v9.70 反作弊），以及协议 v3 的消息信封（v9.90）。

兼容性: Python 3.7+（游戏内 mod 与启动器共用）。
  - 仅使用 stdlib: dataclasses / typing / json / time / hashlib
  - 不使用 3.8+ 语法（walrus、positional-only、typing.Literal/Protocol 等）
  - dataclasses 于 3.7 引入，可用

序列化约定:
  - to_dict()  → 网络传输 / JSON 落盘格式（全部为 JSON 原生类型）
  - from_dict() → 容错反序列化：未知字段忽略、缺失字段取默认值（版本兼容）
  - 网络传输走既有 pickle 帧通道时直接 to_dict() 后作为 payload；跨版本场景
    （协议 v3 认证前）转 JSON 字符串。

在启动器/工具中直接运行本文件可执行自检:
  python v10_data_structures.py
"""

import dataclasses
import hashlib
import json
import time
from typing import Any, Dict, List, Optional, Tuple

# ============================================================================
# 0. 通用: 协议 v3 消息信封（v9.90）
# ============================================================================

#: 协议版本常量（与 src/multimod/network.py 的 PROTO_VERSION 对齐演进）
PROTO_VERSION_V3 = 3

#: 帧标志位（v3 帧头 flags 字节）
FLAG_COMPRESSED = 0x01   # payload 经 zlib
FLAG_ENCRYPTED = 0x02    # payload 经 ChaCha20（v3 可选）
FLAG_BATCHED = 0x04      # payload 是多条 MessageEnvelope 的数组


@dataclasses.dataclass
class MessageEnvelope:
    """协议 v3 消息信封——payload 之外的所有路由/校验元数据。

    v2 的问题: 消息类型藏在 pickle payload 里，认证前必须反序列化才能知道
    "这是什么消息"（RCE 暴露面）。v3 把 type/seq/pid 提到明文区:
      [ver:1B][type:2B][flags:1B][len:4B][crc:4B][hmac:32B][payload]
    本 dataclass 即帧头明文区的结构化表示。
    """
    type_id: int = 0            # 消息类型（明文枚举，见 TYPE_ID_TABLE）
    seq: int = 0                # 发送端单调递增序号（重放检测）
    ts: float = 0.0             # 发送端时间戳（time.time()）
    sender_pid: int = -1        # 发送者 player_id（host=0）
    flags: int = 0              # FLAG_* 位或
    proto_version: int = PROTO_VERSION_V3
    payload: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "MessageEnvelope":
        if not d:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_json(self) -> str:
        """认证前阶段的安全编码（v3 规定未握手 payload 只允许 JSON）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, s: str) -> "MessageEnvelope":
        return cls.from_dict(json.loads(s))


#: v3 消息类型 ID 分配表（帧头明文 2B；0-63 系统保留，64+ 动态扩展）
TYPE_ID_TABLE: Dict[int, str] = {
    0: "heartbeat", 1: "hello", 2: "welcome", 3: "join_rejected",
    4: "version_mismatch", 5: "lobby", 6: "ready", 7: "leave", 8: "kicked",
    16: "chat", 17: "clock", 18: "sim_pos", 19: "money_sync", 20: "stats_sync",
    21: "interaction", 22: "inventory", 23: "relationship", 24: "object_pos",
    25: "world_snapshot", 26: "batch",
    32: "ping", 33: "pong",
    48: "travel_req", 49: "travel_ack", 50: "travel_go", 51: "travel_arrived",
    52: "travel_all_arrived", 53: "travel_missing",
    56: "save_sync_req", 57: "save_sync_ack", 58: "save_chunk",
    59: "save_chunk_done", 60: "save_resend_req", 61: "save_sync_done",
    64: "room_list_req", 65: "room_list", 66: "spectator_join",
    67: "perm_grant", 68: "seat_reserved",
    72: "mods_manifest_req", 73: "mods_manifest", 74: "mods_mismatch",
    75: "mods_chunk_req", 76: "mods_chunk", 77: "mods_chunk_done",
    78: "mods_sync_done",
    80: "voice_ctrl", 81: "voice_state",
    88: "correction_req", 89: "trust_update", 90: "rate_exceeded",
    91: "cheat_alert",
}
TYPE_NAME_TO_ID: Dict[str, int] = {v: k for k, v in TYPE_ID_TABLE.items()}


# ============================================================================
# 1. 房间系统 2.0（v9.40）
# ============================================================================

#: 成员角色（权限分级）
ROLE_HOST = 0        # 房主: 全部权限
ROLE_SUBHOST = 1     # 副房主: 踢人/发起旅行/同步存档
ROLE_MEMBER = 2      # 普通成员
ROLE_SPECTATOR = 3   # 观战: 只收状态，不参与门槛，不可发操作

#: 房间状态（与 room_protocol.py 状态机对齐并扩展席位保留态）
ROOM_STATE_WAITING = "waiting"
ROOM_STATE_READY = "ready"
ROOM_STATE_SYNCING = "syncing"
ROOM_STATE_SYNCED = "synced"
ROOM_STATE_LAUNCHING = "launching"


@dataclasses.dataclass
class RoomMember:
    """房间成员（lobby 广播 members[] 的规范结构，替代现在的裸 dict）。

    对比 v2 的改进: 角色分级（role）、席位保留（seat_expire_ts）、
    mod 一致性标记（mods_ok）进入成员模型。
    """
    player_id: int = 0
    name: str = ""
    role: int = ROLE_MEMBER                    # ROLE_*
    ready: bool = False
    in_lot: bool = False
    online: bool = True
    last_seen: float = 0.0                     # time.time()，心跳刷新
    ip: str = ""                               # 房主侧取 socket 对端地址（不自报）
    seat_expire_ts: float = 0.0                # >0 表示席位保留中（断线重连窗口）
    mods_ok: Optional[bool] = None             # None=未上报 True=一致 False=有差异

    def is_reserved(self, now: Optional[float] = None) -> bool:
        n = time.time() if now is None else now
        return self.seat_expire_ts > n

    def can_kick(self) -> bool:
        return self.role in (ROLE_HOST, ROLE_SUBHOST)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "RoomMember":
        if not d:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclasses.dataclass
class RoomSettings:
    """房间设置（持久化到 mp_room_persist.json，房主崩溃恢复用）。"""
    room_code: str = ""                        # 6 位，ROOM_CODE_ALPHABET
    visibility: str = "public"                 # public | private
    password: str = ""                         # 仅内存/建房时使用，不落盘
    max_players: int = 8
    allow_spectators: bool = True
    require_mods_match: bool = True            # mod 不一致拦截开始（房主可强制）
    seat_reserve_seconds: int = 120            # 席位保留时长
    game_port: int = 7655
    room_port: int = 7660
    created_ts: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d.pop("password", None)                # 密码不进持久化/广播
        return d

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "RoomSettings":
        if not d:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclasses.dataclass
class RoomListEntry:
    """大厅房间列表条目（UDP 发现聚合 / 集中式目录服务返回项）。"""
    room_code: str = ""
    name: str = ""
    host_ip: str = ""
    room_port: int = 7660
    players: int = 0
    max_players: int = 8
    has_password: bool = False
    mods_hash: str = ""                        # 房主 manifest_hash 前 8 位
    state: str = ROOM_STATE_WAITING
    ts: float = 0.0                            # 发现/注册时间（TTL 过滤用）

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "RoomListEntry":
        if not d:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclasses.dataclass
class RoomSnapshot:
    """lobby 消息的规范载荷（v2 里散在 dict 顶层的字段收拢 + 加世代号）。

    generation 用于主机迁移防脑裂（M-2）: 每次成员表变更 +1，竞选时校验
    本地 generation 一致才结算，不一致则延长 settle 等待。
    """
    members: List[RoomMember] = dataclasses.field(default_factory=list)
    state: str = ROOM_STATE_WAITING
    save_sync_phase: str = "idle"              # idle | waiting_ack | done
    start_granted: bool = False
    generation: int = 0
    room_code: str = ""
    visibility: str = "public"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "members": [m.to_dict() for m in self.members],
            "state": self.state,
            "save_sync_phase": self.save_sync_phase,
            "start_granted": self.start_granted,
            "generation": self.generation,
            "room_code": self.room_code,
            "visibility": self.visibility,
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "RoomSnapshot":
        if not d:
            return cls()
        members = [RoomMember.from_dict(m) for m in d.get("members", []) or []]
        return cls(
            members=members,
            state=str(d.get("state", ROOM_STATE_WAITING)),
            save_sync_phase=str(d.get("save_sync_phase", "idle")),
            start_granted=bool(d.get("start_granted", False)),
            generation=int(d.get("generation", 0)),
            room_code=str(d.get("room_code", "")),
            visibility=str(d.get("visibility", "public")),
        )


# ============================================================================
# 2. Mod 同步（v9.50）
# ============================================================================

#: mod 文件后缀白名单（S-6/S-7 路径穿越修复 + mod 分发共用的校验集）
MOD_ALLOWED_SUFFIXES = (".package", ".ts4script", ".py")

#: 全量哈希阈值（小于该值算全量，大于则采样哈希）
MOD_FULL_HASH_LIMIT = 5 * 1024 * 1024


def hash_mod_file(path: str, sample_limit: int = MOD_FULL_HASH_LIMIT) -> str:
    """计算 mod 文件指纹（大文件用 首1MB+尾1MB+大小 采样）。

    纯 stdlib；10GB 级 Mods 目录控制在 30s 内的依据: 采样哈希只读 2MB/文件。
    """
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        if size <= sample_limit:
            f.seek(0)
            h.update(f.read())
        else:
            f.seek(0)
            h.update(f.read(1024 * 1024))
            f.seek(-1024 * 1024, 2)
            h.update(f.read(1024 * 1024))
    h.update(str(size).encode("ascii"))
    return h.hexdigest()


@dataclasses.dataclass
class ModEntry:
    """manifest 中的单个 mod 条目。"""
    rel_path: str = ""                         # 相对 Mods/ 的正斜杠路径（白名单校验后）
    size: int = 0
    sha256: str = ""                           # 全量或采样哈希
    load_order: int = 0                        # 资源加载序提示

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "ModEntry":
        if not d:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclasses.dataclass
class ModManifest:
    """房主侧 Mods 目录指纹（对比 = 双方各自计算 manifest_hash）。"""
    room_code: str = ""
    game_version: str = ""
    entries: List[ModEntry] = dataclasses.field(default_factory=list)
    created_ts: float = 0.0

    def manifest_hash(self) -> str:
        """集合指纹 = sha256(排序后的 'path:sha' 行)。"""
        lines = sorted("{}:{}".format(e.rel_path, e.sha256) for e in self.entries)
        return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "room_code": self.room_code,
            "game_version": self.game_version,
            "entries": [e.to_dict() for e in self.entries],
            "created_ts": self.created_ts,
            "manifest_hash": self.manifest_hash(),
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "ModManifest":
        if not d:
            return cls()
        entries = [ModEntry.from_dict(e) for e in d.get("entries", []) or []]
        return cls(
            room_code=str(d.get("room_code", "")),
            game_version=str(d.get("game_version", "")),
            entries=entries,
            created_ts=float(d.get("created_ts", 0.0)),
        )


@dataclasses.dataclass
class ModDiff:
    """客机对比结果（mods_mismatch 消息载荷）。"""
    missing: List[str] = dataclasses.field(default_factory=list)      # 客机缺
    extra: List[str] = dataclasses.field(default_factory=list)        # 客机多
    different: List[str] = dataclasses.field(default_factory=list)    # 哈希不同

    def is_empty(self) -> bool:
        return not (self.missing or self.extra or self.different)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "ModDiff":
        if not d:
            return cls()
        return cls(
            missing=[str(x) for x in d.get("missing", []) or []],
            extra=[str(x) for x in d.get("extra", []) or []],
            different=[str(x) for x in d.get("different", []) or []],
        )

    @classmethod
    def compute(cls, host: ModManifest, mine: ModManifest) -> "ModDiff":
        """客户端侧对比: 以房主 manifest 为基准。"""
        host_map = {e.rel_path: e.sha256 for e in host.entries}
        my_map = {e.rel_path: e.sha256 for e in mine.entries}
        missing = sorted(p for p in host_map if p not in my_map)
        different = sorted(p for p in host_map
                           if p in my_map and my_map[p] != host_map[p])
        extra = sorted(p for p in my_map if p not in host_map)
        return cls(missing=missing, extra=extra, different=different)


@dataclasses.dataclass
class ModSyncProgress:
    """mod 分发进度（启动器 UI 显示 / mods_chunk 流的记账）。"""
    rel_path: str = ""
    received_bytes: int = 0
    total_bytes: int = 0
    received_chunks: int = 0
    total_chunks: int = 0
    sha256: str = ""                           # 期望值（done 时校验）
    staged_path: str = ""                      # 暂存区落盘路径（不直接进 Mods 根）

    def percent(self) -> float:
        if self.total_chunks <= 0:
            return 0.0
        return min(100.0, 100.0 * self.received_chunks / self.total_chunks)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "ModSyncProgress":
        if not d:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# ============================================================================
# 3. 语音提示（v9.60）
# ============================================================================

#: 语音事件类型（VoicePromptEvent.event 取值）
VOICE_EVT_MEMBER_JOIN = "member_join"
VOICE_EVT_MEMBER_LEAVE = "member_leave"
VOICE_EVT_KICKED = "kicked"
VOICE_EVT_TRAVEL_START = "travel_start"
VOICE_EVT_TRAVEL_ARRIVED = "travel_arrived"
VOICE_EVT_SAVE_DONE = "save_done"
VOICE_EVT_RECONNECTING = "reconnecting"
VOICE_EVT_QUALITY_BAD = "quality_bad"


@dataclasses.dataclass
class VoicePromptEvent:
    """事件语音播报条目（mod → mp_voice_events.jsonl → 启动器 TTS 消费）。

    设计: 语音完全在游戏进程外（启动器托盘进程）执行，游戏侧只追加写此结构
    的 JSONL 行，规避游戏内音频 API 与线程风险。
    """
    event: str = ""                            # VOICE_EVT_*
    text: str = ""                             # 已渲染文案（含玩家名等）
    priority: int = 1                          # 0=立即播报 1=普通 2=可丢弃
    ts: float = 0.0
    cooldown_key: str = ""                     # 同 key 在冷却时间内不重复播报

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "VoicePromptEvent":
        if not d:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclasses.dataclass
class VoicePromptRule:
    """播报规则（可配置文件 voice_rules.json 的一条）。"""
    event: str = ""
    template: str = ""                         # 如 "{name} 加入了房间"
    priority: int = 1
    cooldown_seconds: float = 10.0             # 同 cooldown_key 最小间隔
    enabled: bool = True

    def render(self, ctx: Optional[Dict[str, Any]] = None) -> str:
        try:
            return self.template.format(**(ctx or {}))
        except (KeyError, IndexError):
            return self.template

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "VoicePromptRule":
        if not d:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclasses.dataclass
class VoiceChatPacket:
    """PTT 语音包（UDP 7662，启动器托盘进程之间传输，不经过游戏）。

    包体 = pack('>HII', seq, ts_ms, sender_pid) + opus_data（≤200B）。
    本 dataclass 是其结构化表示（构造/解析工具）。
    """
    seq: int = 0                               # 单调递增（丢包检测）
    ts_ms: int = 0                             # 发送端毫秒时间戳（jitter buffer 对齐）
    sender_pid: int = -1
    opus_data: bytes = b""                     # 20ms 帧 Opus 24kbps VBR

    def pack(self) -> bytes:
        import struct
        return struct.pack(">HII", self.seq & 0xFFFF,
                           self.ts_ms & 0xFFFFFFFF, self.sender_pid & 0xFFFFFFFF) \
            + self.opus_data

    @classmethod
    def unpack(cls, raw: bytes) -> "VoiceChatPacket":
        import struct
        seq, ts_ms, pid = struct.unpack(">HII", raw[:10])
        return cls(seq=seq, ts_ms=ts_ms, sender_pid=pid, opus_data=raw[10:])

    def to_dict(self) -> Dict[str, Any]:
        # bytes 不可 JSON，语音包不走 JSON 通道；to_dict 仅供调试 UI
        return {"seq": self.seq, "ts_ms": self.ts_ms,
                "sender_pid": self.sender_pid, "len": len(self.opus_data)}


@dataclasses.dataclass
class VoiceSettings:
    """语音设置（启动器设置页持久化）。"""
    enabled: bool = True
    ptt_key: str = "v"                         # 按住说话键
    output_device: str = ""                    # 空=系统默认
    input_volume: float = 0.8
    output_volume: float = 0.9
    jitter_ms: int = 120                       # 接收 jitter buffer
    bitrate_kbps: int = 24
    events_enabled: bool = True                # 事件语音播报总开关

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "VoiceSettings":
        if not d:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# ============================================================================
# 4. 反作弊（v9.70）
# ============================================================================

#: 信任分级动作
TRUST_ACTION_NONE = 0
TRUST_ACTION_MUTE = 1                          # 禁言（chat 令牌桶置零）
TRUST_ACTION_KICK = 2                          # 踢出 + 时限拉黑
TRUST_ACTION_BAN = 3                           # 长期拉黑（房主手动解除）


@dataclasses.dataclass
class MovementSample:
    """移动校验样本（房主侧为每个远端 sim 维护的最后状态）。"""
    sim_id: int = 0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    ts: float = 0.0
    last_seq: int = 0                          # 单调校验（重放检测）
    accumulated_error: float = 0.0             # delta_q 量化误差累计估计

    def distance_to(self, x: float, y: float, z: float) -> float:
        return ((self.x - x) ** 2 + (self.y - y) ** 2 + (self.z - z) ** 2) ** 0.5

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "MovementSample":
        if not d:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclasses.dataclass
class MovementCheckResult:
    """单帧移动校验结果。"""
    ok: bool = True
    speed: float = 0.0                         # 本帧推算速度 m/s
    reason: str = ""                           # 拒绝原因（审计日志用）
    need_correction: bool = False              # True → 发 correction_req 要绝对坐标

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def check_movement(sample: MovementSample, x: float, y: float, z: float,
                   ts: float, seq: int,
                   max_speed: float = 12.0) -> MovementCheckResult:
    """房主侧校验一帧位置（纯函数，便于单测——不修改 sample，调用方在
    接受后自行更新 sample 的 x/y/z/ts/last_seq；拒绝帧也应推进 last_seq
    以阻断对同一帧的重放）。

    规则:
      1. seq 必须 > last_seq（重放/乱序丢弃）
      2. speed = dist/dt，超过 max_speed 拒绝；连续超速由调用方累计触发纠正
    """
    dt = ts - sample.ts
    if seq <= sample.last_seq:
        return MovementCheckResult(ok=False, reason="seq_not_increasing")
    if dt <= 0:
        # 同刻多帧: 只有序号合法性，速度无法判定则放行（防除零）
        return MovementCheckResult(ok=True)
    dist = sample.distance_to(x, y, z)
    speed = dist / dt
    if speed > max_speed:
        return MovementCheckResult(ok=False, speed=speed,
                                   reason="speed_over_limit",
                                   need_correction=True)
    return MovementCheckResult(ok=True, speed=speed)


@dataclasses.dataclass
class RateLimitPolicy:
    """每 (pid, 消息类型) 令牌桶策略。"""
    message_type: str = ""
    capacity: int = 10                          # 突发桶容量
    refill_per_second: float = 1.0
    penalty: int = 1                            # 溢出一次扣的信任分

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "RateLimitPolicy":
        if not d:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


#: 默认速率策略表（v9.70 内置，可被 mp_ratelimit.json 覆盖）
DEFAULT_RATE_POLICIES: Tuple[RateLimitPolicy, ...] = (
    RateLimitPolicy("chat", capacity=10, refill_per_second=10.0 / 60.0),
    RateLimitPolicy("sim_pos", capacity=20, refill_per_second=10.0),
    RateLimitPolicy("interaction", capacity=10, refill_per_second=5.0),
    RateLimitPolicy("money_sync", capacity=3, refill_per_second=1.0),
    RateLimitPolicy("save_resend_req", capacity=3, refill_per_second=0.2, penalty=2),
)


@dataclasses.dataclass
class TokenBucket:
    """令牌桶（运行态；单线程访问即可——挂在房主主线程消费路径上）。"""
    capacity: int = 10
    tokens: float = 10.0
    refill_per_second: float = 1.0
    last_refill_ts: float = 0.0

    def refill(self, now: Optional[float] = None) -> None:
        n = time.time() if now is None else now
        if self.last_refill_ts <= 0:
            self.last_refill_ts = n
            return
        elapsed = max(0.0, n - self.last_refill_ts)
        if elapsed > 0:
            self.tokens = min(float(self.capacity),
                              self.tokens + elapsed * self.refill_per_second)
            self.last_refill_ts = n

    def try_acquire(self, now: Optional[float] = None) -> bool:
        self.refill(now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


@dataclasses.dataclass
class PlayerTrustRecord:
    """每客机信任分账本（房主侧，随断开清理，扣分明细写 mp_audit.log）。"""
    player_id: int = 0
    score: int = 100
    money_divergence_count: int = 0            # 金钱不可收敛次数
    speed_violation_count: int = 0             # 移动超速次数
    rate_exceeded_count: int = 0               # 令牌桶溢出次数
    money_last_host_value: int = 0             # 房主侧资金基准（收敛校验用）
    updated_ts: float = 0.0

    def action(self) -> int:
        """按当前分数返回应执行的动作（TRUST_ACTION_*）。"""
        if self.score < 30:
            return TRUST_ACTION_KICK
        if self.score < 60:
            return TRUST_ACTION_MUTE
        return TRUST_ACTION_NONE

    def penalize(self, points: int, reason: str,
                 audit: Optional[List[Dict[str, Any]]] = None) -> None:
        self.score = max(0, self.score - points)
        self.updated_ts = time.time()
        if audit is not None:
            audit.append({"player_id": self.player_id, "points": -points,
                          "reason": reason, "score": self.score,
                          "ts": self.updated_ts})

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "PlayerTrustRecord":
        if not d:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclasses.dataclass
class CheatAlert:
    """反作弊告警（房主 UI 通知 + mp_audit.log 结构化条目）。"""
    player_id: int = 0
    kind: str = ""                              # speed/money_divergence/rate/replay
    detail: str = ""
    evidence: Dict[str, Any] = dataclasses.field(default_factory=dict)
    trust_score_after: int = 100
    action: int = TRUST_ACTION_NONE
    ts: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "CheatAlert":
        if not d:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclasses.dataclass
class ValidationPolicy:
    """反作弊总策略（可持久化 mp_validation.json，房主可调）。"""
    max_speed_mps: float = 12.0                # 游戏跑姿上限(~6) ×2 容差
    money_divergence_tolerance: int = 500      # 客机值与房主基准偏差容差
    money_divergence_max_count: int = 5        # 不可收敛次数上限 → 踢出
    correction_interval_s: float = 30.0        # 周期性绝对坐标重同步间隔
    correction_error_threshold: float = 0.5    # 累计漂移 >0.5m 触发纠正
    ts_window_s: float = 60.0                  # 消息时间戳容忍窗（弱校验）
    kick_score: int = 30
    mute_score: int = 60

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "ValidationPolicy":
        if not d:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# ============================================================================
# 自检（python v10_data_structures.py）
# ============================================================================

def _selftest() -> int:
    checks = 0

    def ok(cond, name):
        assert cond, "selftest failed: {}".format(name)

    # 1. 信封 round-trip + 未知字段忽略
    env = MessageEnvelope(type_id=TYPE_NAME_TO_ID["chat"], seq=7, ts=1.5,
                          sender_pid=2, payload={"text": "hi"})
    rt = MessageEnvelope.from_dict(json.loads(env.to_json()))
    ok(rt.type_id == env.type_id and rt.seq == 7 and rt.payload["text"] == "hi",
       "envelope roundtrip")
    d = env.to_dict(); d["future_field"] = 1
    ok(MessageEnvelope.from_dict(d).seq == 7, "unknown field ignored")
    checks += 2

    # 2. 房间成员/快照
    snap = RoomSnapshot(members=[RoomMember(player_id=1, name="A", role=ROLE_SUBHOST),
                                 RoomMember(player_id=2, name="B", role=ROLE_SPECTATOR)],
                        state=ROOM_STATE_SYNCED, generation=5)
    rt = RoomSnapshot.from_dict(snap.to_dict())
    ok(len(rt.members) == 2 and rt.members[0].role == ROLE_SUBHOST
       and rt.members[1].can_kick() is False and rt.generation == 5, "room snapshot")
    m = RoomMember(seat_expire_ts=time.time() + 60)
    ok(m.is_reserved() and not RoomMember().is_reserved(), "seat reserved")
    ok("password" not in RoomSettings(password="x").to_dict(), "password not serialized")
    checks += 3

    # 3. manifest hash 稳定 + diff
    mf_host = ModManifest(entries=[ModEntry("a.package", 1, "aa"), ModEntry("b.package", 2, "bb")])
    mf_cli = ModManifest(entries=[ModEntry("a.package", 1, "aa"), ModEntry("c.py", 3, "cc")])
    ok(mf_host.manifest_hash() == ModManifest.from_dict(mf_host.to_dict()).manifest_hash(),
       "manifest hash stable")
    diff = ModDiff.compute(mf_host, mf_cli)
    ok(diff.missing == ["b.package"] and diff.extra == ["c.py"] and not diff.is_empty(),
       "mod diff")
    checks += 2

    # 4. 语音
    pkt = VoiceChatPacket(seq=9, ts_ms=123456, sender_pid=3, opus_data=b"\x01\x02\x03")
    rt2 = VoiceChatPacket.unpack(pkt.pack())
    ok(rt2.seq == 9 and rt2.sender_pid == 3 and rt2.opus_data == b"\x01\x02\x03",
       "voice packet pack/unpack")
    rule = VoicePromptRule(template="{name} 加入了房间")
    ok(rule.render({"name": "Tom"}) == "Tom 加入了房间", "rule render")
    checks += 2

    # 5. 反作弊
    s = MovementSample(sim_id=1, x=0.0, y=0.0, z=0.0, ts=100.0, last_seq=0)
    r1 = check_movement(s, 1.0, 0.0, 0.0, 101.0, seq=1)          # 1 m/s OK
    ok(r1.ok and abs(r1.speed - 1.0) < 1e-9, "movement ok")
    s.last_seq = 1                                              # 调用方推进序号
    r2 = check_movement(s, 100.0, 0.0, 0.0, 102.0, seq=2)        # 100 m/s 拒绝
    ok(not r2.ok and r2.need_correction, "movement rejected")
    s.last_seq = 2
    r3 = check_movement(s, 1.0, 0.0, 0.0, 103.0, seq=1)          # 旧 seq 重放
    ok(not r3.ok and r3.reason == "seq_not_increasing", "replay rejected")
    bucket = TokenBucket(capacity=2, tokens=2.0, refill_per_second=0.0)
    ok(bucket.try_acquire() and bucket.try_acquire() and not bucket.try_acquire(),
       "token bucket")
    trust = PlayerTrustRecord(player_id=1)
    audit = []
    for _ in range(8):
        trust.penalize(10, "money_divergence", audit)
    ok(trust.score == 20 and trust.action() == TRUST_ACTION_KICK and len(audit) == 8,
       "trust escalate")
    checks += 5

    print("[v10_data_structures] all {} selftest groups passed".format(checks))
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
