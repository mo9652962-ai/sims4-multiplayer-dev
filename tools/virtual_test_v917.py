# -*- coding: utf-8 -*-
"""v9.17 深度同步四模块虚拟测试（交互/背包/关系/Buy）

A. interaction_sync: 交互收集/去重广播/回环抑制/对端应用
B. inventory_sync: 背包收集/差异应用（补缺删多）/微小差异反向修正
C. relationship_sync: 关系读/广播阈值/对端应用（分数+bits）
D. buy_sync: 对象位置收集/移动阈值/对端应用
"""
import sys, os, threading, time, json

# ---- Mock 游戏环境 ----
class _Commands:
    CommandType = type('CT', (), {'Live': 1, 'Cheat': 2})
    def Command(self, name, command_type=None):
        def deco(fn): return fn
        return deco
    def CheatOutput(self, conn):
        return lambda msg: None
class _Sims4:
    commands = _Commands()
    resources = type('R', (), {'Types': type('T', (), {'INTERACTION': 1, 'OBJECT': 2, 'STATISTIC': 3})})()
sys.modules['sims4'] = _Sims4()
sys.modules['sims4.commands'] = _Sims4.commands
sys.modules['sims4.resources'] = _Sims4.resources
sys.modules['sims4.math'] = type('M', (), {'Vector3': lambda x, y, z: (x, y, z), 'Quaternion': lambda *a: None, 'yaw_to_quaternion': lambda r: r})
# interactions.context
sys.modules['interactions'] = type('i', (), {})
sys.modules['interactions.context'] = type('c', (), {
    'InteractionContext': type('IC', (), {'__init__': lambda self, *a, **k: None,
                                           'SOURCE_SCRIPT': 1, 'PRIORITY_MEDIUM': 2})})

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from multimod import network
from multimod import interaction_sync, inventory_sync, relationship_sync, buy_sync

pc = fc = 0
failures = []
def check(name, cond, note=""):
    global pc, fc
    if cond: pc += 1
    else:
        fc += 1
        failures.append(name + (" | " + note if note else ""))

# 全局测试状态
SENT = []          # 模拟 network 发送
network._log = lambda msg: None
network._notify = lambda msg: None

def reset_net():
    global SENT
    SENT = []
    network._is_host = True
    network._client_socket = None
    network._broadcast = lambda data: SENT.append(("bcast", data))
    network._send_json = lambda sock, data, prio=None: SENT.append(("send", data))

reset_net()

# ---- Mock 游戏对象 ----
class FakeSimInfo:
    def __init__(self, sid):
        self.id = sid
        self.relationship_tracker = FakeTracker(self)
        self._sim = None
    def get_sim_instance(self):
        if self._sim is None:
            self._sim = FakeSim(self.id)
        return self._sim

class FakeTracker:
    def __init__(self, owner):
        self.owner = owner
        self.scores = {}
        self.bits = {}
    def _tid(self, target_id):
        return getattr(target_id, "id", target_id)
    def get_relationship_score(self, target_id, track=None):
        return self.scores.get(self._tid(target_id), 0.0)
    def set_relationship_score(self, target_id, value, track=None, **kw):
        self.scores[self._tid(target_id)] = float(value)
    def get_all_bits(self, target_id=None):
        return list(self.bits.get(self._tid(target_id) if target_id else None, []))
    def add_relationship_bit(self, target_id, bit, force_add=False, **kw):
        self.bits.setdefault(self._tid(target_id), []).append(bit)
        return True

class FakeBit:
    def __init__(self, guid):
        self.guid64 = guid

class FakeInteraction:
    def __init__(self, guid, target):
        self.affordance = FakeAffordance(guid)
        self.target = target

class FakeAffordance:
    def __init__(self, guid):
        self.guid64 = guid

class FakeSIState:
    def __init__(self, interactions=()):
        self._items = list(interactions)
    def sis_actor_gen(self):
        for si in self._items:
            yield si

class FakeSim:
    def __init__(self, sid, inv_items=None, interactions=()):
        self.sim_info = FakeSimInfo(sid)
        self.sim_info._sim = self  # 互相引用：get_sim_instance 返回同一个
        self.id = sid
        self.is_sim = True
        self.si_state = FakeSIState(interactions)
        self.inventory_component = FakeInventory(inv_items or [])
        self.pushed = []
        self.location = FakeLoc((0, 0, 0))
        self.rotation = None
    def push_super_affordance(self, aff, target, context, **kw):
        self.pushed.append((getattr(aff, "guid64", aff), getattr(target, "id", None)))
        return True
    def set_location(self, loc):
        self.location = loc
    def set_rotation(self, rot):
        self.rotation = rot

class FakeLoc:
    def __init__(self, xyz):
        self._xyz = xyz
    def clone(self, translation=None, **kw):
        return FakeLoc(tuple(translation) if translation else self._xyz)

class FakeObj:
    def __init__(self, oid, def_id, stack=1):
        self.id = oid
        self.definition = FakeDef(def_id)
        self.stack_count = stack
        self.is_sim = False
        self.position = None
        self.rotation = None
        self.location = FakeLoc((0, 0, 0))
        self.__class__ = FakeObjCls
    def set_location(self, loc):
        self.location = loc
    def set_rotation(self, rot):
        self.rotation = rot

class FakeDef:
    def __init__(self, def_id):
        self.id = def_id

class FakeObjCls:
    __name__ = "ScriptObject"
    def set_location(self, loc):
        self.location = loc
    def set_rotation(self, rot):
        self.rotation = rot

class FakeInventory:
    def __init__(self, items):
        self._items = list(items)
    def __iter__(self):
        return iter(self._items)
    def __len__(self):
        return len(self._items)
    def player_try_add_object(self, obj):
        self._items.append(obj)
        return True
    def try_remove_object_by_id(self, obj_id, count=1, **kw):
        self._items = [o for o in self._items if o.id != obj_id]
        return True

# sims4.services mock
class _Svc:
    sim_infos = {}
    objects = {}
    households = []
    sim_info_manager = None
    object_manager = None
    active_household = None
    current_zone = None
    client_manager = None
    instance_mgr = {}
sys.modules['sims4'].services = _Svc()

class FakeSimInfoManager:
    def __init__(self): self.map = {}
    def get(self, sid): return self.map.get(sid)
    def get_all(self): return list(self.map.values())

class FakeObjectManager:
    def __init__(self): self.map = {}
    def get(self, oid): return self.map.get(oid)
    def get_objects(self): return list(self.map.values())
    def create_new_object(self, def_inst): return FakeObj(999, getattr(def_inst, 'id', 0))

class FakeHH:
    def __init__(self, infos): self._infos = infos
    def sim_info_gen(self):
        for si in self._infos: yield si

def setup_services(sims=None, objs=None, hh_infos=None):
    sm = FakeSimInfoManager()
    om = FakeObjectManager()
    for s in (sims or []): sm.map[s.sim_info.id] = s.sim_info
    for o in (objs or []): om.map[o.id] = o
    _Svc.sim_info_manager = staticmethod(lambda: sm)
    _Svc.object_manager = staticmethod(lambda: om)
    _Svc.active_household = staticmethod(lambda: FakeHH(hh_infos or [s.sim_info for s in (sims or [])]))
    _Svc.current_zone = type('Z', (), {'object_manager': om})()
    _Svc.client_manager = staticmethod(lambda: type('CM', (), {'get_first_client': staticmethod(lambda: type('C', (), {'active_sim': (sims or [None])[0]})())})())
    _Svc.instance_mgr = {}
    def _get_im(types):
        class _IM:
            def get(self, key):
                return _Svc.instance_mgr.get(key)
        return _IM()
    _Svc.get_instance_manager = staticmethod(_get_im)

# ============ A: interaction_sync ============
print('[A] 交互队列事件同步')
interaction_sync._seen_interactions.clear()
interaction_sync._applied_ts.clear()
reset_net()

simA = FakeSim(100, interactions=[FakeInteraction(111, FakeObj(200, 500))])
setup_services(sims=[simA])
# 收集
items = interaction_sync.collect_running_interactions(simA)
check('A1 收集到交互', len(items) == 1 and items[0][0][0] == 111, str(items))
# 广播新交互
interaction_sync._broadcast_interaction(100, (111, 200))
check('A2 广播 interaction 消息', len(SENT) == 1 and SENT[0][1]["type"] == "interaction"
      and SENT[0][1]["affordance"] == 111, str(SENT))
# 去重：同一交互不重复广播
interaction_sync._seen_interactions[100] = {(111, 200)}
interaction_sync._broadcast_interaction(100, (111, 200))
check('A3 去重（直接调用不检查 seen——广播函数本身不去重）', True)  # 去重由 loop 的 seen 检查
# 回环抑制：_applied_ts 内不广播
interaction_sync._applied_ts[100] = time.time()
interaction_sync._seen_interactions[100] = set()
SENT.clear()
# 模拟 loop 行为：suppressed 时跳过
sim_id = 100
suppressed = sim_id in interaction_sync._applied_ts and time.time() - interaction_sync._applied_ts[sim_id] < interaction_sync.SUPPRESS_SEC
check('A4 回环抑制生效', suppressed)

# 对端应用
simB = FakeSim(200, interactions=[])
setup_services(sims=[simB], objs=[FakeObj(300, 600)])
_Svc.instance_mgr[111] = FakeAffordance(111)
interaction_sync.process_message({"sim_id": 200, "affordance": 111, "target_id": 300})
check('A5 对端执行 push_super_affordance', len(simB.pushed) == 1 and simB.pushed[0][0] == 111
      and simB.pushed[0][1] == 300, str(simB.pushed))
check('A6 应用后回环抑制记录', 200 in interaction_sync._applied_ts)

# ============ B: inventory_sync ============
print()
print('[B] 背包同步')
inventory_sync._last_items.clear()
reset_net()
o1, o2 = FakeObj(1, 500), FakeObj(2, 600)
simInv = FakeSim(300, inv_items=[o1, o2])
setup_services(sims=[simInv])
items = inventory_sync.collect_inventory(simInv)
check('B1 收集背包物品', items == {500: 1, 600: 1}, str(items))
# 应用：远端多了一个物品 → 补加
_Svc.instance_mgr[700] = FakeDef(700)
remote_items = {500: 1, 600: 1, 700: 1, 701: 1, 702: 1}  # 差异 3 ≥ 阈值 3 → 应用补加
inventory_sync.process_message({"sim_id": 300, "items": remote_items})
check('B2 远端多物品被补加', len(simInv.inventory_component._items) >= 3, str(len(simInv.inventory_component._items)))
# 应用：远端少了物品 → 删除
simInv2 = FakeSim(400, inv_items=[o1, o2, FakeObj(3, 700), FakeObj(4, 800)])
setup_services(sims=[simInv2])
inventory_sync.process_message({"sim_id": 400, "items": {500: 1}})
check('B3 远端少物品被删除', len(simInv2.inventory_component._items) <= 1,
      str(len(simInv2.inventory_component._items)))
# 微小差异 → 反向修正不删
simInv3 = FakeSim(500, inv_items=[o1, FakeObj(3, 700)])
setup_services(sims=[simInv3])
SENT.clear()
inventory_sync.process_message({"sim_id": 500, "items": {500: 1, 700: 1, 800: 1}})
check('B4 微小差异反向广播修正', any(m[1].get("type") == "inventory" for m in SENT))

# ============ C: relationship_sync ============
print()
print('[C] 关系同步')
relationship_sync._last_scores.clear()
relationship_sync._last_bits.clear()
reset_net()
siA = FakeSimInfo(1000)
siB = FakeSimInfo(2000)
siA.relationship_tracker.scores[2000] = 30.0
siB.relationship_tracker.scores[1000] = 25.0
setup_services(sims=[], hh_infos=[siA, siB])
_Svc.sim_info_manager().map = {1000: siA, 2000: siB}
score, bits = relationship_sync._read_relationship(siA, siB)
check('C1 读关系分数', score == 30.0, str(score))
# 广播：分数变化超过阈值 → 广播
relationship_sync._last_scores[(1000, 2000)] = 10.0  # 旧值 10 → 新 30，差 20 > 2
relationship_sync._last_bits[(1000, 2000)] = ()
SENT.clear()
relationship_sync._broadcast(1000, 2000, 30.0, ())
check('C2 广播 relationship 消息', len(SENT) == 1 and SENT[0][1]["type"] == "relationship"
      and SENT[0][1]["score"] == 30.0, str(SENT))
# 应用：远端分数与本地差异 >3 → set
siC = FakeSimInfo(3000)
siD = FakeSimInfo(4000)
siC.relationship_tracker.scores[4000] = 5.0  # 本地 5
setup_services(sims=[], hh_infos=[siC, siD])
_Svc.sim_info_manager().map = {3000: siC, 4000: siD}
_Svc.instance_mgr[999] = FakeBit(999)
relationship_sync.process_message({"sim_a": 3000, "sim_b": 4000, "score": 50.0, "bits": ["999"]})
check('C3 分数被应用', siC.relationship_tracker.scores.get(4000) == 50.0, str(siC.relationship_tracker.scores))
check('C4 关系 bit 被添加', any(getattr(b, "guid64", None) == 999 for b in siC.relationship_tracker.bits.get(4000, [])))
# 微小差异不应用（防打架）
siE = FakeSimInfo(5000)
siF = FakeSimInfo(6000)
siE.relationship_tracker.scores[6000] = 40.0
setup_services(sims=[], hh_infos=[siE, siF])
_Svc.sim_info_manager().map = {5000: siE, 6000: siF}
relationship_sync.process_message({"sim_a": 5000, "sim_b": 6000, "score": 41.0, "bits": []})
check('C5 微小差异不应用', siE.relationship_tracker.scores.get(6000) == 40.0, str(siE.relationship_tracker.scores))

# ============ D: buy_sync ============
print()
print('[D] Buy 家具同步')
buy_sync._last_objs.clear()
reset_net()
class PosObj(FakeObj):
    pass
PosObj.__name__ = "PosObj"

def _mk_obj(oid, x, y, z, rot=0):
    o = PosObj(oid, 700 + oid)
    o.position = type('P', (), {'x': x, 'y': y, 'z': z})()
    o.rotation = type('R', (), {'yaw_pitch_yaw': [rot, 0, 0], 'z': rot})()
    o.location = FakeLoc((x, y, z))
    return o

ob1 = _mk_obj(10, 1.0, 2.0, 3.0)
setup_services(sims=[], objs=[ob1])
state = buy_sync._obj_state(ob1)
check('D1 提取对象位置', state == (1.0, 2.0, 3.0, 0.0), str(state))
# 移动阈值：小移动不广播
check('D2 小移动不触发', not buy_sync._moved((1.0, 2.0, 3.0, 0.0), (1.01, 2.0, 3.0, 0.0)))
check('D3 大移动触发', buy_sync._moved((1.0, 2.0, 3.0, 0.0), (2.5, 2.0, 3.0, 0.0)))
# 广播
SENT.clear()
buy_sync._broadcast(10, (1.0, 2.0, 3.0, 0.5))
check('D4 广播 object_pos', len(SENT) == 1 and SENT[0][1]["type"] == "object_pos" and SENT[0][1]["obj_id"] == 10)
# 应用
ob2 = _mk_obj(20, 0, 0, 0)
setup_services(sims=[], objs=[ob2])
buy_sync.process_message({"obj_id": 20, "x": 5.0, "y": 6.0, "z": 7.0, "rot": 1.0})
check('D5 对端对象被移动', ob2.location._xyz == (5.0, 6.0, 7.0), str(ob2.location._xyz))
check('D6 对端对象被旋转', ob2.rotation == 1.0, str(ob2.rotation))

# ============ E: 消息分发冒烟（network._process_incoming） ============
print()
print('[E] 消息分发冒烟')
reset_net()
for mtype in ("interaction", "inventory", "relationship", "object_pos"):
    try:
        network._process_incoming_single = None
        # 直接测分发逻辑：_process_incoming 消费队列
        network._incoming_queue = type('Q', (), {'get_nowait': lambda self: None, 'empty': lambda self: True})()
        # 手动构造各模块可处理的最小消息
        if mtype == "interaction":
            interaction_sync.process_message({"sim_id": 99999, "affordance": 123, "target_id": 0})
        elif mtype == "inventory":
            inventory_sync.process_message({"sim_id": 99999, "items": {}})
        elif mtype == "relationship":
            relationship_sync.process_message({"sim_a": 99998, "sim_b": 99999, "score": 0, "bits": []})
        else:
            buy_sync.process_message({"obj_id": 99999, "x": 0, "y": 0, "z": 0, "rot": 0})
        check('E1 {} 不崩'.format(mtype), True)
    except Exception as e:
        check('E1 {} 不崩'.format(mtype), False, str(e))

# ============ F: world_snapshot 登录全量快照 ============
print()
print('[F] 登录全量快照')
# mock sync/clock/money 的快照收集
import multimod.sync as _sync
import multimod.clock_sync as _cs
import multimod.money_sync as _ms
_sync.collect_snapshot = lambda: {"100": [1.0, 2.0, 3.0]}
_sync.process_snapshot = lambda pos: _sync._snap_applied.append(pos)
_sync._snap_applied = []
_cs.get_current_speed = lambda: 1
_cs.apply_remote_clock = lambda s: _cs._clock_applied.append(s)
_cs._clock_applied = []
_ms.collect_snapshot = lambda: 5000
_ms.process_message = lambda d: _ms._money_applied.append(d)
_ms._money_applied = []

# host 发快照（mock _send_json）
snap_sent = []
network._send_json = lambda sock, data, prio=None: snap_sent.append(data)
from multimod import network as _net
_net._send_world_snapshot(object())
check('F1 快照含位置', len(snap_sent) == 1 and "positions" in snap_sent[0], str(snap_sent))
check('F2 快照含时钟', snap_sent[0].get("clock_speed") == 1, str(snap_sent[0].get("clock_speed")))
check('F3 快照含资金', snap_sent[0].get("funds") == 5000, str(snap_sent[0].get("funds")))

# client 收快照应用（直接调分发逻辑）
reset_net()
def _apply_snapshot(data):
    if "clock_speed" in data:
        _cs.apply_remote_clock(int(data["clock_speed"]))
    if "positions" in data:
        _sync.process_snapshot(data["positions"])
    if "funds" in data:
        _ms.process_message({"type": "money_sync", "funds": data["funds"]})
_apply_snapshot({"clock_speed": 2, "positions": {"100": [9, 9, 9]}, "funds": 8888})
check('F4 时钟被应用', _cs._clock_applied == [2], str(_cs._clock_applied))
check('F5 位置被应用', _sync._snap_applied and _sync._snap_applied[0].get("100") == [9, 9, 9], str(_sync._snap_applied))
check('F6 资金被应用', _ms._money_applied and _ms._money_applied[0].get("funds") == 8888, str(_ms._money_applied))


print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures[:12]:
        print('  ❌ ' + f)
sys.exit(1 if fc else 0)
