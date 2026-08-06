# -*- coding: utf-8 -*-
"""Sims4Multiplayer M1 - 入口模块 (v4.3)

v4.3: 通知用 S4MP 同款 (sim_info + distributor 直发) + 自动消息处理 alarm
v4.2 实验: 验证多模块 + Live 类型命令是否都正常
- __init__.py 保留 mp_hello (已验证能触发)
- 引入 network.py (多模块, 所有命令 Live 类型)
- 如果 mp_hello 仍能触发 → 多模块没问题, 之前是 Cheat 类型的问题
"""

import sims4.commands
import os
import time

LOG_PATH = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts",
                        "The Sims 4", "Mods", "mp_debug.log")


def _log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


_log("=== v9.17 module loaded ===")

# 引入多模块
from multimod import network  # noqa: F401
from multimod import reload_service  # noqa: F401  # v9.19: 热重载 (S4MP 借鉴)
from multimod import event_system  # noqa: F401  # v9.19: EventHook + tick (S4MP 借鉴)
from multimod import core_hooks  # noqa: F401  # v9.19: on_tick hook (S4MP 借鉴)


def _auto_apply_config():
    """游戏加载完成后自动读取启动器配置并连接（延迟等待游戏就绪）"""
    try:
        import threading

        def _do():
            time.sleep(30)  # 等游戏主菜单/家庭加载完成
            try:
                # 仅在配置存在且用户尚未手动连接时执行
                from multimod import network as net
                if net._network_thread and net._network_thread.is_alive():
                    _log("auto-apply skipped: already connected")
                    return
                cfg = net._load_launcher_config()
                if cfg is None:
                    _log("auto-apply skipped: no launcher config")
                    return
                _log("auto-apply: found config, connecting...")
                net._apply_launcher_config()
            except Exception as e:
                _log("auto-apply error: {}".format(e))

        threading.Thread(target=_do, daemon=True).start()
    except Exception as e:
        _log("auto-apply setup error: {}".format(e))


_auto_apply_config()  # 启动器一键启动时自动连接


@sims4.commands.Command("mp_hello", command_type=sims4.commands.CommandType.Live)
def mp_hello(*args, _connection=None):
    _log("mp_hello EXECUTED! args={} conn={}".format(args, _connection))
    output = sims4.commands.CheatOutput(_connection)
    output("Sims4Multiplayer v4.2: OK!")
    _notify("mp_hello v4.2 生效!")


def _notify(text):
    try:
        import services
        client = services.client_manager().get_first_client()
        if client is None:
            _log("no client")
            return
        sim_info = client.active_sim_info
        if sim_info is None:
            _log("no active sim_info")
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
        _log("notify sent")
    except Exception as e:
        _log("notify failed: {}".format(e))


_log("=== v9.17 init complete ===")
