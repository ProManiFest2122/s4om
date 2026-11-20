# -*- coding: utf-8 -*-
"""S4OM Online - one small window for playing The Sims 4 together by room code.

Host: click "Host a game", give the code to your friends, play.
Join: type the code, click "Join", start The Sims 4.

That is all a player does. The window takes care of the rest:
  - it carries the mod (S4OM.ts4script) inside and puts it into the Mods folder, replaces an
    older copy, and moves aside misnamed copies ("S4OM (1).ts4script", "S4OM.ts4script.zip")
  - it turns on "Script Mods Allowed" in the game's Options.ini when it is off (with a backup)
  - it writes Mods/mp_launcher_config.json; the mod follows that file while the game runs, so
    it doesn't matter whether the game was started before or after the click
  - it shows what the game is actually doing, read from Mods/mp_status.json which the mod
    writes: hosting / waiting for the host / connected, and who is in the room

No account, no token, no port forwarding: everyone connects out to a relay (relay/relay.py).

Standard library only (tkinter).  Run: python online_launcher.py
Build an .exe (after build.py):
  pyinstaller --onefile --windowed --name S4OM-Online --paths src/multimod \
              --add-data "build/S4OM.ts4script;." online_launcher.py
"""
import hashlib
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src", "multimod"))
try:
    from relay_default import DEFAULT_RELAY     # the same default the mod is built with
except Exception:
    DEFAULT_RELAY = ""

RELAY_PORT = 7656
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # same as the mod: no 0/O, 1/I
MOD_FILE = "S4OM.ts4script"
STATUS_FILE = "mp_status.json"
STATUS_FRESH = 20.0          # the mod rewrites its status every 3 s; older than this = not running
MOD_LOAD_GRACE = 120.0       # seconds a starting game may take before its mod reports in
GAME_EXES = ("ts4_x64.exe", "ts4.exe", "ts4_dx9_x64.exe", "ts4_x64_fpb.exe")


# ---------------------------------------------------------------- texts
TEXT = {
    "en": {
        "name": "Your name", "server": "Server", "host": "Host a game", "join": "Join a game", "copy": "Copy code",
        "code_hint": "room code", "alone": "Play alone (online off)", "checking": "Checking the server...",
        "need_server": "Type the server's address first (ask whoever runs your relay), e.g. play.example.org",
        "need_code": "Type the room code your host gave you (letters and digits).",
        "no_server": "Can't reach the server {} - check your internet connection.",
        "copied": "Room code {} copied.",
        "off": "Online is off - the game starts as usual.",
        "setup_ok": "Mod installed, script mods on.",
        "setup_installed": "Mod installed into your Mods folder.",
        "setup_updated": "Mod updated to this version.",
        "setup_restart": "Mod installed - close The Sims 4 and start it again so it loads.",
        "setup_close": "A newer mod is ready - close The Sims 4 so it can be replaced, then open this window again.",
        "setup_nobundle": "This copy of the launcher has no mod inside - put S4OM.ts4script into your Mods folder yourself.",
        "setup_options_fixed": "Script mods were switched off in the game - turned them on.",
        "setup_options_close": "Script mods are switched off in the game. Close The Sims 4 and open this window again - it will turn them on.",
        "setup_moved": "Moved aside {} wrong copy/copies of the mod.",
        "st_host_idle": "Room {} is yours. Start The Sims 4 and load your household - the room opens by itself. Give this code to your friends.",
        "st_join_idle": "You'll join room {}. Start The Sims 4 - you're connected as soon as the host is in the game, and their game opens by itself.",
        "st_none_idle": "Click Host a game, or type a friend's code and click Join.",
        "st_no_mod": "The Sims 4 is running, but the S4OM mod didn't load. Close the game and start it again.",
        "st_hosting": "Hosting room {} - in the room: {}.",
        "st_hosting_alone": "Hosting room {} - nobody has joined yet. Give this code to your friends.",
        "st_connected": "Connected to room {} - in the room: {}.",
        "st_waiting": "Waiting for the host: room {} isn't open yet. You'll be connected as soon as it is.",
        "st_joining": "Joining room {}...",
        "st_game_idle": "The Sims 4 is running. Connecting in a moment...",
        "st_game_off": "The Sims 4 is running, online is off.",
        "st_other_room": "The game is in room {}. To switch to room {} close The Sims 4 and start it again.",
        "st_starting": "The Sims 4 is starting...",
    },
    "pl": {
        "name": "Twoja nazwa", "server": "Serwer", "host": "Hostuj grę", "join": "Dołącz", "copy": "Kopiuj kod",
        "code_hint": "kod pokoju", "alone": "Graj sam (online wyłączone)", "checking": "Sprawdzam serwer...",
        "need_server": "Najpierw wpisz adres serwera (zapytaj osobę, która go prowadzi), np. play.example.org",
        "need_code": "Wpisz kod pokoju od hosta (litery i cyfry).",
        "no_server": "Nie mogę połączyć się z serwerem {} - sprawdź internet.",
        "copied": "Skopiowano kod pokoju {}.",
        "off": "Online wyłączone - gra uruchomi się normalnie.",
        "setup_ok": "Mod zainstalowany, mody skryptowe włączone.",
        "setup_installed": "Zainstalowano mod w folderze Mods.",
        "setup_updated": "Zaktualizowano mod do tej wersji.",
        "setup_restart": "Zainstalowano mod - zamknij The Sims 4 i włącz ponownie, żeby się załadował.",
        "setup_close": "Jest nowsza wersja moda - zamknij The Sims 4, żeby można ją było podmienić, i otwórz to okno ponownie.",
        "setup_nobundle": "Ta kopia launchera nie ma moda w środku - wrzuć S4OM.ts4script do folderu Mods ręcznie.",
        "setup_options_fixed": "Mody skryptowe były wyłączone w grze - włączyłem je.",
        "setup_options_close": "Mody skryptowe są wyłączone w grze. Zamknij The Sims 4 i otwórz to okno ponownie - włączy je.",
        "setup_moved": "Odłożono na bok błędne kopie moda: {}.",
        "st_host_idle": "Pokój {} jest Twój. Włącz The Sims 4 i wczytaj rodzinę - pokój otworzy się sam. Podaj ten kod znajomym.",
        "st_join_idle": "Dołączysz do pokoju {}. Włącz The Sims 4 - połączysz się, gdy tylko host będzie w grze, a jego gra otworzy się sama.",
        "st_none_idle": "Kliknij Hostuj grę albo wpisz kod od znajomego i kliknij Dołącz.",
        "st_no_mod": "The Sims 4 działa, ale mod S4OM się nie załadował. Zamknij grę i włącz ją ponownie.",
        "st_hosting": "Hostujesz pokój {} - w pokoju: {}.",
        "st_hosting_alone": "Hostujesz pokój {} - nikt jeszcze nie dołączył. Podaj ten kod znajomym.",
        "st_connected": "Połączono z pokojem {} - w pokoju: {}.",
        "st_waiting": "Czekam na hosta: pokój {} nie jest jeszcze otwarty. Połączysz się, gdy tylko się otworzy.",
        "st_joining": "Dołączam do pokoju {}...",
        "st_game_idle": "The Sims 4 działa. Za chwilę nastąpi połączenie...",
        "st_game_off": "The Sims 4 działa, online jest wyłączone.",
        "st_other_room": "Gra jest w pokoju {}. Żeby przejść do pokoju {}, zamknij The Sims 4 i włącz ponownie.",
        "st_starting": "The Sims 4 się uruchamia...",
    },
}


def system_language():
    """'pl' on a Polish Windows, otherwise 'en'."""
    try:
        import ctypes

        if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x15:
            return "pl"
    except Exception:
        pass
    return "pl" if str(os.environ.get("LANG", "")).lower().startswith("pl") else "en"


LANG = system_language()


def tr(key, *args, lang=None):
    return TEXT[lang or LANG][key].format(*args)


# ---------------------------------------------------------------- the player's folders
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


def mods_dir():
    return os.path.join(sims_dir(), "Mods")


def config_path():
    return os.path.join(mods_dir(), "mp_launcher_config.json")


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


def game_running():
    """Is The Sims 4 running on this PC?"""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=10, creationflags=0x08000000).stdout.decode("ascii", "replace").lower()
        return any('"{}"'.format(exe) in out for exe in GAME_EXES)
    except Exception:
        return False


# ---------------------------------------------------------------- the mod and the game's options
def bundled_mod():
    """The mod file this launcher carries: inside the .exe, or build/ when run from source."""
    for folder in (getattr(sys, "_MEIPASS", None), os.path.join(HERE, "build")):
        if folder and os.path.isfile(os.path.join(folder, MOD_FILE)):
            return os.path.join(folder, MOD_FILE)
    return None


def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def stray_copies(mods):
    """Copies of the mod that don't load or load twice: renamed downloads next to the real file
    ("S4OM (1).ts4script", "S4OM.ts4script.zip") and the real name again in a subfolder."""
    found = []
    try:
        entries = sorted(os.listdir(mods))
    except OSError:
        return found
    wrong = re.compile(r"^s4om\b.*\.ts4script", re.I)
    for name in entries:
        full = os.path.join(mods, name)
        if os.path.isfile(full):
            if name != MOD_FILE and wrong.match(name):
                found.append(full)
        elif os.path.isdir(full):
            try:
                found.extend(os.path.join(full, n) for n in sorted(os.listdir(full)) if wrong.match(n))
            except OSError:
                pass
    return found


def install_mod(src=None, mods=None, running=None):
    """Put the bundled mod into Mods. -> (state, [moved stray files])
    state: 'none' no bundled mod | 'current' already there | 'installed' | 'updated' |
           'restart' put in while the game runs (loads next start) | 'close' can't replace while the game runs"""
    src = bundled_mod() if src is None else src
    mods = mods or mods_dir()
    if not src:
        return "none", []
    running = game_running() if running is None else running
    dst = os.path.join(mods, MOD_FILE)
    moved = []
    if not running:
        aside = os.path.join(os.path.dirname(mods), "S4OM-old-files")
        for path in stray_copies(mods):
            try:
                os.makedirs(aside, exist_ok=True)
                shutil.move(path, os.path.join(aside, "{}-{}".format(int(time.time()), os.path.basename(path))))
                moved.append(os.path.basename(path))
            except OSError:
                pass
    had = os.path.isfile(dst)
    if had and file_hash(dst) == file_hash(src):
        return "current", moved
    if had and running:
        return "close", moved          # the game holds the old file; replacing it mid-game helps nobody
    try:
        os.makedirs(mods, exist_ok=True)
        shutil.copyfile(src, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
    except OSError:
        return "close", moved
    return ("restart" if running else ("updated" if had else "installed")), moved


def fix_options(path=None, running=None):
    """Make sure the game allows script mods. -> 'ok' | 'fixed' | 'close' (wrong, but the game is
    running and would overwrite the file) | 'missing' (game never started on this PC)."""
    path = path or os.path.join(sims_dir(), "Options.ini")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return "missing"
    text = raw.decode("latin-1")        # byte-for-byte round trip whatever the encoding is
    new = re.sub(r"(?im)^(\s*scriptmodsenabled\s*=\s*)0\b", r"\g<1>1", text)
    new = re.sub(r"(?im)^(\s*modsdisabled\s*=\s*)1\b", r"\g<1>0", new)
    if not re.search(r"(?im)^\s*scriptmodsenabled\s*=", new):
        eol = "\r\n" if "\r\n" in new else "\n"
        new, n = re.subn(r"(?im)^(\[options\][ \t]*\r?\n)", lambda m: m.group(1) + "scriptmodsenabled = 1" + eol, new, count=1)
        if not n:
            new = "[options]" + eol + "scriptmodsenabled = 1" + eol + new
    if new == text:
        return "ok"
    running = game_running() if running is None else running
    if running:
        return "close"
    backup = path + ".s4om-backup"
    try:
        if not os.path.exists(backup):
            shutil.copyfile(path, backup)
        with open(path + ".tmp", "wb") as f:
            f.write(new.encode("latin-1"))
        os.replace(path + ".tmp", path)
    except OSError:
        return "close"
    return "fixed"


def setup_message(mod_state, moved, options_state, lang=None):
    """One or two lines for the player about what was installed or fixed."""
    lines = []
    key = {"installed": "setup_installed", "updated": "setup_updated", "restart": "setup_restart",
           "close": "setup_close", "none": "setup_nobundle"}.get(mod_state)
    if key:
        lines.append(tr(key, lang=lang))
    if options_state == "fixed":
        lines.append(tr("setup_options_fixed", lang=lang))
    elif options_state == "close":
        lines.append(tr("setup_options_close", lang=lang))
    if moved:
        lines.append(tr("setup_moved", ", ".join(moved), lang=lang))
    return "\n".join(lines) or tr("setup_ok", lang=lang)


# ---------------------------------------------------------------- sessions
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
    # "at" is the click: the running game treats a new one as a new request (session_watch.py)
    cfg.update(mode=mode, room_code=code, name=clean_name(name), auto_sync=True, at=int(time.time()))
    cfg.pop("host", None)           # "host" is the LAN host's IP; online rooms don't use it
    cfg.pop("relay_token", None)    # left over from the paid service; never needed here
    if relay_text and relay_text != DEFAULT_RELAY:
        cfg["relay"] = relay_text
    else:
        cfg.pop("relay", None)      # use the one built into the mod
    return cfg


def hosting_code(cfg):
    """The room code this player is already set to host, or None."""
    return clean_code(cfg.get("room_code")) if cfg.get("mode") == "host" else None


def host_config(cfg, name, relay_text, code=None):
    # Keep the code this player already hosts with. A game that is running keeps its room: a
    # fresh code on every click showed the host a code their game was not using, and friends
    # who typed it were told the room isn't open.
    return _session(cfg, "host", code or hosting_code(cfg) or new_code(), name, relay_text)


def join_config(cfg, name, relay_text, code):
    return _session(cfg, "join", code, name, relay_text)


def offline_config(cfg):
    cfg = dict(cfg)
    for key in ("mode", "room_code", "host", "at"):
        cfg.pop(key, None)
    return cfg


# ---------------------------------------------------------------- what the game is doing
def read_status(path=None, now=None):
    """The mod's Mods/mp_status.json if the game wrote it recently, else None."""
    path = path or os.path.join(mods_dir(), STATUS_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or (now or time.time()) - float(data.get("at") or 0) > STATUS_FRESH:
        return None
    return data


def describe(cfg, status, running, lang=None, starting=False):
    """The one line that tells the player where they stand.
    status: read_status(). running: is the game's process there. starting: the game has only
    just been seen running, so a missing status still means "loading", not "the mod is missing"."""
    mode, code = cfg.get("mode"), clean_code(cfg.get("room_code")) or "?"
    wants = mode in ("host", "join")
    if status is None and not running:
        if mode == "host":
            return tr("st_host_idle", code, lang=lang)
        if mode == "join":
            return tr("st_join_idle", code, lang=lang)
        return tr("st_none_idle", lang=lang)
    if status is None:
        return tr("st_starting" if starting else "st_no_mod", lang=lang)
    state, room = status.get("state"), str(status.get("room") or "").upper()
    players = ", ".join(str(p) for p in status.get("players") or [])
    note = status.get("note") or ""
    if state != "idle" and wants and room and room != code:
        return tr("st_other_room", room, code, lang=lang)
    if state == "hosting":
        others = len(status.get("players") or []) > 1
        line = tr("st_hosting", room, players, lang=lang) if others else tr("st_hosting_alone", room, lang=lang)
    elif state == "connected":
        line = tr("st_connected", room, players or "?", lang=lang)
    elif state == "waiting for the host":
        line = tr("st_waiting", room, lang=lang)
    elif state == "joining":
        line = tr("st_joining", room, lang=lang)
    else:
        line = tr("st_game_idle", lang=lang) if wants else tr("st_game_off", lang=lang)
    if note and state != "idle":
        line += "\n" + note
    return line


# ---------------------------------------------------------------- window
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
    shown_code = tk.StringVar(value=hosting_code(cfg) or "")
    status_var = tk.StringVar(value=describe(cfg, None, False))
    setup_var = tk.StringVar(value="")
    state = {"busy": False, "message": "", "message_until": 0.0, "running_since": None}

    def flash(text, seconds=8):
        """A message about the last click; the live status takes over again after a moment."""
        state["message"], state["message_until"] = text, time.time() + seconds
        status_var.set(text)

    def relay_or_complain():
        relay = clean_relay(relay_var.get())
        if relay is None:
            flash(tr("need_server"))
        return relay

    def in_background(work, done):
        if state["busy"]:
            return
        state["busy"] = True

        def run():
            try:
                result = work()
            except Exception as e:
                result = e
            root.after(0, lambda: (state.update(busy=False), done(result)))
        threading.Thread(target=run, daemon=True).start()

    def do_setup():
        def work():
            running = game_running()
            mod_state, moved = install_mod(running=running)
            return setup_message(mod_state, moved, fix_options(running=running))
        in_background(work, lambda text: setup_var.set(text if isinstance(text, str) else ""))

    def start_session(make):
        relay = relay_or_complain()
        if relay is None:
            return
        flash(tr("checking"), 30)

        def done(reachable):
            if reachable is not True:
                flash(tr("no_server", relay_var.get().strip()), 15)
                return
            new = make(load_config())
            save_config(new)
            shown_code.set(hosting_code(new) or "")
            state["message_until"] = 0.0
            refresh()
            do_setup()
        in_background(lambda: relay_reachable(relay), done)

    def on_host():
        start_session(lambda c: host_config(c, name_var.get(), relay_var.get().strip()))

    def on_join():
        code = clean_code(code_var.get())
        if code is None:
            flash(tr("need_code"))
            return
        start_session(lambda c: join_config(c, name_var.get(), relay_var.get().strip(), code))

    def on_offline():
        save_config(offline_config(load_config()))
        shown_code.set("")
        flash(tr("off"))

    def on_copy():
        if shown_code.get():
            root.clipboard_clear()
            root.clipboard_append(shown_code.get())
            flash(tr("copied", shown_code.get()), 4)

    def refresh():
        running, status = game_running(), read_status()
        if not running or status is not None:
            state["running_since"] = None
        elif state["running_since"] is None:
            state["running_since"] = time.time()
        # the mod loads some way into the game's start-up: only call it missing after a while
        starting = state["running_since"] is not None and time.time() - state["running_since"] < MOD_LOAD_GRACE
        if time.time() >= state["message_until"]:
            status_var.set(describe(load_config(), status, running, starting=starting))

    def tick():
        try:
            refresh()
        finally:
            root.after(2500, tick)

    frm = ttk.Frame(root, padding=10)
    frm.grid()
    ttk.Label(frm, text=tr("name")).grid(row=0, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=name_var, width=24).grid(row=0, column=1, columnspan=2, sticky="w", **pad)
    if not DEFAULT_RELAY or cfg.get("relay"):       # a build with its server baked in doesn't need this row
        ttk.Label(frm, text=tr("server")).grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=relay_var, width=34).grid(row=1, column=1, columnspan=2, sticky="w", **pad)

    ttk.Separator(frm).grid(row=2, column=0, columnspan=3, sticky="ew", pady=8)
    ttk.Button(frm, text=tr("host"), command=on_host).grid(row=3, column=0, sticky="ew", **pad)
    tk.Label(frm, textvariable=shown_code, font=("Consolas", 22, "bold"), width=9).grid(row=3, column=1, **pad)
    ttk.Button(frm, text=tr("copy"), command=on_copy).grid(row=3, column=2, sticky="ew", **pad)

    ttk.Button(frm, text=tr("join"), command=on_join).grid(row=4, column=0, sticky="ew", **pad)
    code_entry = ttk.Entry(frm, textvariable=code_var, width=12, font=("Consolas", 14))
    code_entry.grid(row=4, column=1, **pad)
    code_entry.bind("<Return>", lambda e: on_join())
    ttk.Label(frm, text=tr("code_hint")).grid(row=4, column=2, sticky="w", **pad)

    ttk.Separator(frm).grid(row=5, column=0, columnspan=3, sticky="ew", pady=8)
    ttk.Label(frm, textvariable=status_var, wraplength=460, justify="left").grid(row=6, column=0, columnspan=3, sticky="w", **pad)
    ttk.Label(frm, textvariable=setup_var, wraplength=460, justify="left", foreground="#555").grid(
        row=7, column=0, columnspan=3, sticky="w", **pad)
    ttk.Button(frm, text=tr("alone"), command=on_offline).grid(row=8, column=0, columnspan=2, sticky="w", **pad)

    do_setup()
    root.after(300, tick)
    root.mainloop()


if __name__ == "__main__":
    main()
