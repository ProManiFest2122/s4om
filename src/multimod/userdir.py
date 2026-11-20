# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\userdir.py
# Compiled at: 2026-11-15 21:31:35
# Size of source mod 2**32: 2402 bytes

"""Where this player's The Sims 4 folder really is (Mods, Saves, Tray).

fork 2026-11-15 (ticket #4): every path used to be ~/Documents/Electronic Arts/The Sims 4. When
Windows' Documents folder is moved (another drive, OneDrive), the launcher writes the session
config into the REAL Documents, but the mod read a stale config (and wrote its log) under
~/Documents - so a host's game joined an old room ("ERR NO_ROOM") and "Send logs" sent an empty
log. Order: where the game loaded us from > Windows' Documents setting > the old default.
No game imports here - used before anything else loads.
"""

import os

_DEFAULT = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts", "The Sims 4")


def from_module_path(path, isdir=os.path.isdir):
    r"""Pure (tested): '...\The Sims 4\Mods\[sub\]S4OM.ts4script\multimod\x.pyc' -> '...\The Sims 4'."""
    if not path:
        return
    parts = path.replace("\\", "/").split("/")
    for i in range(len(parts) - 1, 0, -1):
        if parts[i].lower() == "mods":
            root = "/".join(parts[:i])
            if root.endswith(":"):
                root += "/"
            root = os.path.normpath(root)
            if isdir(os.path.join(root, "Mods")):
                return root


def _from_registry():
    try:
        import winreg
    except Exception:
        return
    else:
        for key, expand in (
            ("Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\User Shell Folders", True),
            ("Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Shell Folders", False),
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                    (val, _) = winreg.QueryValueEx(k, "Personal")
                docs = os.path.expandvars(val) if expand else val
                root = os.path.join(docs, "Electronic Arts", "The Sims 4")
                if os.path.isdir(root):
                    return root
            except Exception:
                continue


def _find():
    # Tests (tools/) set this so they never read or write a real player's Mods folder.
    override = os.environ.get("S4OM_USERDIR")
    if override:
        return override
    try:
        found = from_module_path(os.path.abspath(__file__))
        if found:
            return found
    except Exception:
        pass

    return _from_registry() or _DEFAULT


SIMS_DIR = _find()

MODS_DIR = os.path.join(SIMS_DIR, "Mods")


def sims_path(*parts):
    return (os.path.join)(SIMS_DIR, *parts)


def mods_path(*parts):
    return (os.path.join)(MODS_DIR, *parts)
