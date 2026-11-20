# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\native.py
# Compiled at: 2026-10-12 00:59:27
# Size of source mod 2**32: 4559 bytes

"""Native bridge probe - step 1 toward live build mode and auto-loading saves.

Both need native code inside the game (walls/floors and loading a save live in the C++
side). Script mods can load native extension modules (.pyd) from a loose folder with
importlib's ExtensionFileLoader - s4-online does this - so the plan is our own .pyd.
The game ships its own ctypes binary (Game/Bin/Python/DLLs/_ctypes_x64.pyd, linked
to the game's python37_x64.dll) but not the pure-Python ctypes package - S4OM carries that
package (CPython 3.7.9, PSF license) inside S4OM.ts4script. So:

  stage 1: logs sys.version / sys.dllhandle / sys.winver / extension suffixes
  stage 2: imports the game's _ctypes, then ctypes, and reads the game exe path, base
           address and whether python37_x64.dll is loaded - proof native calls work.
           On by default; launcher config "native_probe": false turns it off.

Results -> mp_debug.log and mp_selfcheck.json ("native"). mp_native shows them in-game.
"""

import json, os, sys

from multimod import network

result = {}


def stage1():
    info = {
        "version": sys.version.split()[0],
        "winver": getattr(sys, "winver", None),
        "dll": hasattr(sys, "dllhandle"),
        "executable": (os.path.basename)(sys.executable or ""),
    }
    try:
        import importlib.machinery

        info["ext_suffixes"] = list(importlib.machinery.EXTENSION_SUFFIXES)
    except Exception as e:
        info["ext_suffixes"] = "error: {}".format(e)

    return info


def _game_ctypes_paths():
    seen = []
    for base in [os.path.dirname(sys.executable or "")] + list(sys.path):
        if not isinstance(base, str):
            continue
        for p in (
            os.path.join(base, "Python", "DLLs", "_ctypes_x64.pyd"),
            os.path.join(base, "_ctypes_x64.pyd"),
            os.path.join(base, "DLLs", "_ctypes_x64.pyd"),
        ):
            if p not in seen and os.path.exists(p):
                seen.append(p)

    return seen


def load_ctypes():
    """Import the game's _ctypes (normal import first, then the DLLs folder), then ctypes."""
    how = "import"
    try:
        import _ctypes
    except ImportError:
        import importlib.machinery, importlib.util

        paths = _game_ctypes_paths()
        if not paths:
            raise ImportError("the game's _ctypes_x64.pyd wasn't found")
        loader = importlib.machinery.ExtensionFileLoader("_ctypes", paths[0])
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("_ctypes", loader))
        loader.exec_module(mod)
        sys.modules["_ctypes"] = mod
        how = paths[0]

    import ctypes

    return (ctypes, how)


def stage2():
    ctypes, how = load_ctypes()
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    k32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    k32.GetModuleHandleW.restype = wintypes.HMODULE
    buf = ctypes.create_unicode_buffer(520)
    k32.GetModuleFileNameW(None, buf, 520)
    return {
        "ok": True,
        "ctypes_from": how,
        "exe": buf.value,
        "exe_base": hex(k32.GetModuleHandleW(None) or 0),
        "python_dll": [n for n in ("python37_x64.dll", "python37.dll") if k32.GetModuleHandleW(n)],
    }


def run():
    global result
    result = {"stage1": (stage1())}
    cfg = network._load_launcher_config() or {}
    if cfg.get("native_probe", True) is not False:
        try:
            result["stage2"] = stage2()
        except Exception as e:
            result["stage2"] = {"ok": False, "error": ("{}: {}".format)(type(e).__name__, e)}

    network._log("native: {}".format(json.dumps(result)))
    try:
        path = os.path.join(os.path.dirname(network.CMD_PATH), "mp_selfcheck.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}

        data["native"] = result
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except Exception:
        pass

    return result


try:
    run()
except Exception as _e:
    network._log("native probe failed: {}".format(_e))

try:
    import sims4.commands

    @sims4.commands.Command("mp_native", command_type=(sims4.commands.CommandType.Live))
    def mp_native(_connection=None):
        out = sims4.commands.CheatOutput(_connection)
        out(json.dumps(result))

except Exception:
    pass
