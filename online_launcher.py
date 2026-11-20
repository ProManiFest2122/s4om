# -*- coding: utf-8 -*-
"""S4OM Online - a small window for playing online by room code.

Host: click "Host a game", give the code to your friends, start The Sims 4.
Join: type the code, click "Join", start The Sims 4.

No account, no token, no port forwarding: everyone connects out to a relay (relay/relay.py) and
meets by room code. This window only writes Mods/mp_launcher_config.json, the file the mod reads
when the game starts (already in the game? type mp_apply in the cheat console). The same can be
done without it from the cheat console: mp_host_online / mp_join_online <CODE>.

Standard library only (tkinter).  Run: python online_launcher.py
Build an .exe:  pyinstaller --onefile --windowed --name S4OM-Online --paths src/multimod online_launcher.py
"""
import json
import os
import random
import socket
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src", "multimod"))
try:
    from relay_default import DEFAULT_RELAY     # the same default the mod is built with
except Exception:
    DEFAULT_RELAY = ""

RELAY_PORT = 7656
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # same as the mod: no 0/O, 1/I
MOD_FILE = "S4OM.ts4script"


# ---------------------------------------------------------------- logic (no window needed)
def sims_dir():
    """This player's 'The Sims 4' folder - same order as the mod's userdir.py."""
    override = os.environ.get("S4OM_USERDIR")
    if override:
        return override
    try:
        import winreg
        for key, expand in (("Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\User Shell Folders", True),
                            ("Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Shell Folders", False)):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                    val, _ = winreg.QueryValueEx(k, "Personal")
                root = os.path.join(os.path.expandvars(val) if expand else val, "Electronic Arts", "The Sims 4")
                if os.path.isdir(root):
                    return root
            except Exception:
                continue
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts", "The Sims 4")


def config_path():
    return os.path.join(sims_dir(), "Mods", "mp_launcher_config.json")


def load_config(path=None):
    try:
        with open(path or config_path(), encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg, path=None):
    path = path or config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def mod_installed():
    mods = os.path.join(sims_dir(), "Mods")
    for dirpath, dirnames, filenames in os.walk(mods):
        if MOD_FILE in filenames:
            return True
        if dirpath.count(os.sep) - mods.count(os.sep) >= 1:    # the game loads scripts one folder deep at most
            dirnames[:] = []
    return False


def clean_relay(text):
    """'host' or 'host:port' -> (host, port), or None when it isn't an address."""
    text = str(text or "").strip()
    host, _, port = text.partition(":")
    if not host or " " in text or (port and not port.isdigit()):
        return None
    return host, int(port) if port else RELAY_PORT


def clean_code(text):
    code = "".join(str(text or "").split()).upper()
    return code if 4 <= len(code) <= 12 and code.isalnum() else None


def clean_name(text):
    return (str(text or "").strip() or os.environ.get("USERNAME") or os.environ.get("USER") or "Player")[:16]


def new_code():
    return "".join(random.choice(CODE_ALPHABET) for _ in range(6))


def find_room(relay, code, timeout=6):
    """True / False: is that room open on the relay?  None: the relay can't be reached."""
    try:
        s = socket.create_connection(relay, timeout=timeout)
        try:
            s.sendall("S4OM FIND {}\n".format(code).encode("ascii"))
            reply = s.recv(64).decode("ascii", "replace").strip()
        finally:
            s.close()
    except OSError:
        return None
    return reply.startswith("OK")


def relay_reachable(relay, timeout=6):
    return find_room(relay, "PING00", timeout) is not None


def _session(cfg, mode, code, name, relay_text):
    cfg = dict(cfg)
    cfg.update(mode=mode, room_code=code, name=clean_name(name), auto_sync=True)
    cfg.pop("host", None)           # "host" is the LAN host's IP; online rooms don't use it
    cfg.pop("relay_token", None)    # left over from the paid service; never needed here
    if relay_text and relay_text != DEFAULT_RELAY:
        cfg["relay"] = relay_text
    else:
        cfg.pop("relay", None)      # use the one built into the mod
    return cfg


def host_config(cfg, name, relay_text, code=None):
    return _session(cfg, "host", code or new_code(), name, relay_text)


def join_config(cfg, name, relay_text, code):
    return _session(cfg, "join", code, name, relay_text)


def offline_config(cfg):
    cfg = dict(cfg)
    for key in ("mode", "room_code", "host"):
        cfg.pop(key, None)
    return cfg


def describe(cfg):
    mode, code = cfg.get("mode"), cfg.get("room_code")
    if mode == "host" and code:
        return "Set to HOST room {}".format(code)
    if mode == "join" and code:
        return "Set to JOIN room {}".format(code)
    return "Online is off - the game starts as usual"


# ---------------------------------------------------------------- window
NEXT_STEP = ("Now start The Sims 4 and load your household - S4OM connects by itself.\n"
             "Already in the game? Press Ctrl+Shift+C and type:  mp_apply")


def main():
    import tkinter as tk
    from tkinter import ttk

    cfg = load_config()
    root = tk.Tk()
    root.title("S4OM Online")
    root.resizable(False, False)
    pad = {"padx": 12, "pady": 6}

    name_var = tk.StringVar(value=clean_name(cfg.get("name")))
    relay_var = tk.StringVar(value=str(cfg.get("relay") or DEFAULT_RELAY))
    code_var = tk.StringVar()
    shown_code = tk.StringVar(value=cfg.get("room_code", "") if cfg.get("mode") == "host" else "")
    status = tk.StringVar(value=describe(cfg))
    busy = {"on": False}

    def say(text):
        status.set(text)

    def relay_or_complain():
        relay = clean_relay(relay_var.get())
        if relay is None:
            say("Type the server's address first (ask whoever runs your relay), e.g. play.example.org")
        return relay

    def in_background(work, done):
        if busy["on"]:
            return
        busy["on"] = True
        say("Checking the server...")

        def run():
            try:
                result = work()
            except Exception as e:
                result = e
            root.after(0, lambda: (busy.update(on=False), done(result)))
        threading.Thread(target=run, daemon=True).start()

    def on_host():
        relay = relay_or_complain()
        if relay is None:
            return

        def done(ok):
            if ok is not True:
                say("Can't reach the server {} - check your internet and the address.".format(relay_var.get().strip()))
                return
            new = host_config(load_config(), name_var.get(), relay_var.get().strip())
            save_config(new)
            shown_code.set(new["room_code"])
            say("Hosting room {}. Give this code to your friends.\n{}".format(new["room_code"], NEXT_STEP))
        in_background(lambda: relay_reachable(relay), done)

    def on_join():
        relay = relay_or_complain()
        if relay is None:
            return
        code = clean_code(code_var.get())
        if code is None:
            say("Type the room code your host gave you (letters and digits).")
            return

        def done(found):
            if found is None or isinstance(found, Exception):
                say("Can't reach the server {} - check your internet and the address.".format(relay_var.get().strip()))
            elif not found:
                say("Room {} isn't open. Check the code - and the host has to be in the game first.".format(code))
            else:
                save_config(join_config(load_config(), name_var.get(), relay_var.get().strip(), code))
                shown_code.set("")
                say("Joining room {}.\n{}".format(code, NEXT_STEP))
        in_background(lambda: find_room(relay, code), done)

    def on_offline():
        save_config(offline_config(load_config()))
        shown_code.set("")
        say("Online is off - the game starts as usual.")

    def on_copy():
        if shown_code.get():
            root.clipboard_clear()
            root.clipboard_append(shown_code.get())
            say("Room code {} copied.".format(shown_code.get()))

    frm = ttk.Frame(root, padding=10)
    frm.grid()
    ttk.Label(frm, text="Your name").grid(row=0, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=name_var, width=24).grid(row=0, column=1, columnspan=2, sticky="w", **pad)
    ttk.Label(frm, text="Server").grid(row=1, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=relay_var, width=34).grid(row=1, column=1, columnspan=2, sticky="w", **pad)

    ttk.Separator(frm).grid(row=2, column=0, columnspan=3, sticky="ew", pady=8)
    ttk.Button(frm, text="Host a game", command=on_host).grid(row=3, column=0, sticky="ew", **pad)
    tk.Label(frm, textvariable=shown_code, font=("Consolas", 22, "bold"), width=9).grid(row=3, column=1, **pad)
    ttk.Button(frm, text="Copy code", command=on_copy).grid(row=3, column=2, sticky="ew", **pad)

    ttk.Button(frm, text="Join a game", command=on_join).grid(row=4, column=0, sticky="ew", **pad)
    code_entry = ttk.Entry(frm, textvariable=code_var, width=12, font=("Consolas", 14))
    code_entry.grid(row=4, column=1, **pad)
    code_entry.bind("<Return>", lambda e: on_join())
    ttk.Label(frm, text="room code").grid(row=4, column=2, sticky="w", **pad)

    ttk.Separator(frm).grid(row=5, column=0, columnspan=3, sticky="ew", pady=8)
    ttk.Label(frm, textvariable=status, wraplength=430, justify="left").grid(row=6, column=0, columnspan=3, sticky="w", **pad)
    ttk.Button(frm, text="Play alone (online off)", command=on_offline).grid(row=7, column=0, columnspan=2, sticky="w", **pad)

    if not mod_installed():
        say("{} isn't in your Mods folder yet:\n{}\nPut it there (and turn on script mods in the game's options).".format(
            MOD_FILE, os.path.join(sims_dir(), "Mods")))
    root.mainloop()


if __name__ == "__main__":
    main()
