# -*- coding: utf-8 -*-
"""Sims4Multiplayer v9.19 — 热重载模块（参考 S4MP reload_service.py）

功能：
  mp_reload <module>  重载单个 .py 文件（从 Mods 目录）
  mp_reload_all       断开网络 + 重载所有模块

来源: S4MP 反编译 reload_service.py（sims4.reload.reload_file API）
用法: 游戏内作弊控制台输入命令，mod 改动后无需重启游戏

v9.19 修复: 顶层 sims4.* 改为 try/except 惰性导入（测试环境无 sims4 可导入不崩）
"""
import os
import time

# 游戏内 API——测试环境可用 mock 或跳过
try:
    import sims4.commands
    import sims4.reload as _r
    _SIMS4_OK = True
except (ImportError, ModuleNotFoundError):
    _SIMS4_OK = False
    sims4 = None  # type: ignore


def _log(msg):
    try:
        from multimod import network
        network._log(msg)
    except Exception:
        log_path = os.path.join(os.path.expanduser("~"), "Documents",
                                "Electronic Arts", "The Sims 4", "Mods", "mp_debug.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(time.strftime("%H:%M:%S"), msg))


def _reload_directory(path, output):
    """递归重载目录下所有 .py 文件（参考 S4MP reload_directory）"""
    for entry in os.listdir(path):
        full = os.path.join(path, entry)
        if os.path.isdir(full):
            _reload_directory(full, output)
        elif entry.endswith(".py"):
            try:
                result = _r.reload_file(full)
                if result is None:
                    output("⚠️ 重载失败: {}".format(entry))
            except Exception as e:
                output("⚠️ 重载错误 {}: {}".format(entry, e))


# ---- 命令注册（仅游戏环境生效） ----

if _SIMS4_OK:

    @sims4.commands.Command("mp_reload", command_type=sims4.commands.CommandType.Live)
    def mp_reload(module=None, _connection=None):
        """热重载指定模块（如 mp_reload clock_sync）"""
        output = sims4.commands.CheatOutput(_connection)
        if not module:
            output("用法: mp_reload <模块名>  (如 mp_reload clock_sync)")
            output("      mp_reload_all  重载全部模块")
            return
        try:
            mod_dir = os.path.join(os.path.expanduser("~"), "Documents",
                                   "Electronic Arts", "The Sims 4", "Mods")
            parts = str(module).replace(".", os.sep).replace("/", os.sep).split(os.sep)
            if parts[0] == "multimod":
                parts = parts[1:]
            fname = parts[-1]
            if not fname.endswith(".py"):
                fname += ".py"
            path = os.path.join(mod_dir, "multimod", *parts[:-1], fname) if len(parts) > 1 else \
                   os.path.join(mod_dir, "multimod", fname)
            if not os.path.exists(path):
                output("文件不存在: {}".format(path))
                return
            output("重载中: {}".format(path))
            result = _r.reload_file(path)
            if result is not None:
                output("✅ 重载成功: {}".format(fname))
                _log("hot reload OK: {}".format(fname))
            else:
                output("❌ 重载失败")
                _log("hot reload FAIL: {}".format(fname))
        except Exception as e:
            output("重载错误: {}".format(e))
            _log("hot reload error: {}".format(e))

    @sims4.commands.Command("mp_reload_all", command_type=sims4.commands.CommandType.Live)
    def mp_reload_all(_connection=None):
        """断开网络 + 重载全部模块（参考 S4MP /mp.reload）"""
        output = sims4.commands.CheatOutput(_connection)
        output("断开连接...")
        try:
            from multimod import network
            network._log("hot reload: disconnecting...")
            if network._is_host:
                network._broadcast({"type": "leave"})
                for pid, (sock, _) in list(network._clients.items()):
                    try:
                        sock.close()
                    except Exception:
                        pass
                network._clients.clear()
                if network._server_socket:
                    try:
                        network._server_socket.close()
                    except Exception:
                        pass
                    network._server_socket = None
            elif network._client_socket:
                try:
                    network._client_socket.close()
                except Exception:
                    pass
                network._client_socket = None
            network._network_thread = None
        except Exception as e:
            output("断开连接失败: {}".format(e))
            _log("hot reload disconnect error: {}".format(e))

        output("重载全部模块...")
        mod_dir = os.path.join(os.path.expanduser("~"), "Documents",
                               "Electronic Arts", "The Sims 4", "Mods", "multimod")
        if os.path.isdir(mod_dir):
            _reload_directory(mod_dir, output)
        output("✅ 重载完成")
        _log("hot reload all: done")
