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
        # window
        "name": "Your name", "server": "Server", "host": "Host a game", "join": "Join", "copy": "Copy code",
        "code_hint": "room code", "alone": "Play alone (online off)", "log": "What happened",
        "need_server": "Type the server's address first (ask whoever runs your relay), e.g. play.example.org",
        "need_code": "Type the room code your host gave you (letters and digits).",
        "copied": "Room code {} copied.",
        "off": "Online is off - the game starts as usual.",
        # step names
        "row_mod": "Mod", "row_options": "Game settings", "row_server": "Server", "row_game": "Game",
        "row_link": "Connection", "row_world": "Host's game",
        # mod
        "mod_checking": "Checking the mod...", "mod_installing": "Installing the mod...",
        "mod_replacing": "Replacing the mod with the new version...",
        "mod_installed": "Mod installed", "mod_updated": "Mod replaced with the new version", "mod_current": "Mod is up to date",
        "mod_restart": "Mod installed - close The Sims 4 and start it again so it loads",
        "mod_close": "A newer mod is waiting - close The Sims 4 so it can be replaced",
        "mod_none": "This launcher has no mod inside - put S4OM.ts4script into Mods yourself",
        "mod_moved": " (wrong copies moved aside: {})",
        # game settings
        "opt_checking": "Checking the game's settings...", "opt_ok": "Script mods are on",
        "opt_fixed": "Script mods were off - turned them on",
        "opt_close": "Script mods are off - close The Sims 4, this window will turn them on",
        "opt_missing": "The game hasn't been started on this PC yet",
        # server
        "srv_checking": "Checking the server...", "srv_ok": "Server is reachable",
        "srv_down": "Can't reach the server {} - check your internet connection",
        "srv_none": "No server address set",
        # game
        "game_off": "Not running", "game_starting": "Starting...", "game_ok": "Running, mod loaded",
        "game_no_mod": "Running, but the S4OM mod didn't load - close the game and start it again",
        # connection
        "link_none": "Click Host a game, or type a friend's code and click Join",
        "link_host_idle": "Room {} opens when you start The Sims 4 and load your household",
        "link_join_idle": "You'll join room {} when you start The Sims 4",
        "link_soon": "Connecting in a moment...", "link_off": "Online is off",
        "link_hosting": "Hosting room {} - in the room: {}",
        "link_hosting_alone": "Hosting room {} - nobody has joined yet, give them the code",
        "link_connected": "Connected to room {} - in the room: {}",
        "link_waiting": "Waiting for the host to open room {}...",
        "link_joining": "Connecting to the host (room {})...",
        "link_reconnecting": "Connection lost - reconnecting to the host...",
        "link_other_room": "The game is in room {}. To switch to room {} close The Sims 4 and start it again",
        "err_ERR NO_ROOM": "Room {} isn't open - check the code, the host has to be in the game",
        "err_ERR ROOM_FULL": "Room {} is full (12 players)",
        "err_ERR ROOM_TAKEN": "Room code {} is already used by someone else - click Play alone, then Host a game for a new code",
        "err_ERR HOST_TIMEOUT": "The host of room {} isn't answering - they may be loading; trying again",
        "err_ERR SLOW_DOWN": "Too many tries in a short time - wait a minute",
        "err_ERR SERVER_FULL": "The server has no room for another game right now",
        "err_other": "The server refused: {}",
        # host's game
        "world_none": "-",
        "world_host_idle": "Your game is sent by itself when someone joins",
        "world_need_household": "Load your household - then your game is sent to: {}",
        "world_need_save": "Save your game once (Esc > Save As) so it can be sent to: {}",
        "world_saving": "Saving your game for: {}...", "world_sending": "Sending your game...",
        "world_sent": "Your game was sent to the players",
        "world_join_idle": "The host's game arrives by itself once you're connected",
        "world_receiving_wait": "The host is saving their game...",
        "world_receiving": "Receiving the host's game ({}%)...",
        "world_arrived": "The host's game arrived - click \"Load host's game\" in The Sims 4",
        "world_opening": "Opening the host's game...",
        "world_in": "You're in the host's game",
        "world_manual": "The host's game arrived - open it yourself: Load Game, the save with the host's name",
        # headline
        "hl_host_idle": "Room {} is yours. Start The Sims 4 and load your household - the room opens by itself. Give this code to your friends.",
        "hl_join_idle": "You'll join room {}. Start The Sims 4 - you're connected as soon as the host is in the game, and their game opens by itself.",
    },
    "pl": {
        "name": "Twoja nazwa", "server": "Serwer", "host": "Hostuj grę", "join": "Dołącz", "copy": "Kopiuj kod",
        "code_hint": "kod pokoju", "alone": "Graj sam (online wyłączone)", "log": "Co się działo",
        "need_server": "Najpierw wpisz adres serwera (zapytaj osobę, która go prowadzi), np. play.example.org",
        "need_code": "Wpisz kod pokoju od hosta (litery i cyfry).",
        "copied": "Skopiowano kod pokoju {}.",
        "off": "Online wyłączone - gra uruchomi się normalnie.",
        "row_mod": "Mod", "row_options": "Ustawienia gry", "row_server": "Serwer", "row_game": "Gra",
        "row_link": "Połączenie", "row_world": "Gra hosta",
        "mod_checking": "Sprawdzam mod...", "mod_installing": "Trwa instalacja moda...",
        "mod_replacing": "Trwa podmiana moda na nową wersję...",
        "mod_installed": "Mod zainstalowany", "mod_updated": "Podmieniono mod na nową wersję", "mod_current": "Mod jest aktualny",
        "mod_restart": "Mod zainstalowany - zamknij The Sims 4 i włącz ponownie, żeby się załadował",
        "mod_close": "Czeka nowsza wersja moda - zamknij The Sims 4, żeby można ją było podmienić",
        "mod_none": "Ten launcher nie ma moda w środku - wrzuć S4OM.ts4script do folderu Mods ręcznie",
        "mod_moved": " (odłożono błędne kopie: {})",
        "opt_checking": "Sprawdzam ustawienia gry...", "opt_ok": "Mody skryptowe są włączone",
        "opt_fixed": "Mody skryptowe były wyłączone - włączyłem je",
        "opt_close": "Mody skryptowe są wyłączone - zamknij The Sims 4, to okno je włączy",
        "opt_missing": "Gra nie była jeszcze uruchamiana na tym komputerze",
        "srv_checking": "Sprawdzam serwer...", "srv_ok": "Serwer jest dostępny",
        "srv_down": "Nie mogę połączyć się z serwerem {} - sprawdź internet",
        "srv_none": "Nie ustawiono adresu serwera",
        "game_off": "Nie jest uruchomiona", "game_starting": "Uruchamia się...", "game_ok": "Działa, mod załadowany",
        "game_no_mod": "Działa, ale mod S4OM się nie załadował - zamknij grę i włącz ją ponownie",
        "link_none": "Kliknij Hostuj grę albo wpisz kod od znajomego i kliknij Dołącz",
        "link_host_idle": "Pokój {} otworzy się, gdy włączysz The Sims 4 i wczytasz rodzinę",
        "link_join_idle": "Dołączysz do pokoju {} po włączeniu The Sims 4",
        "link_soon": "Za chwilę nastąpi połączenie...", "link_off": "Online jest wyłączone",
        "link_hosting": "Hostujesz pokój {} - w pokoju: {}",
        "link_hosting_alone": "Hostujesz pokój {} - nikt jeszcze nie dołączył, podaj znajomym kod",
        "link_connected": "Połączono z pokojem {} - w pokoju: {}",
        "link_waiting": "Czekam, aż host otworzy pokój {}...",
        "link_joining": "Trwa łączenie z hostem (pokój {})...",
        "link_reconnecting": "Utracono połączenie - trwa ponowne łączenie z hostem...",
        "link_other_room": "Gra jest w pokoju {}. Żeby przejść do pokoju {}, zamknij The Sims 4 i włącz ponownie",
        "err_ERR NO_ROOM": "Pokój {} nie jest otwarty - sprawdź kod, host musi być w grze",
        "err_ERR ROOM_FULL": "Pokój {} jest pełny (12 graczy)",
        "err_ERR ROOM_TAKEN": "Kod pokoju {} jest już zajęty przez kogoś innego - kliknij Graj sam, potem Hostuj grę po nowy kod",
        "err_ERR HOST_TIMEOUT": "Host pokoju {} nie odpowiada - może się wczytuje; próbuję ponownie",
        "err_ERR SLOW_DOWN": "Za dużo prób w krótkim czasie - odczekaj minutę",
        "err_ERR SERVER_FULL": "Serwer nie ma teraz miejsca na kolejną grę",
        "err_other": "Serwer odmówił: {}",
        "world_none": "-",
        "world_host_idle": "Twoja gra zostanie wysłana sama, gdy ktoś dołączy",
        "world_need_household": "Wczytaj rodzinę - wtedy gra zostanie wysłana do: {}",
        "world_need_save": "Zapisz grę raz (Esc > Zapisz jako), żeby wysłać ją do: {}",
        "world_saving": "Zapisuję grę dla: {}...", "world_sending": "Wysyłam grę...",
        "world_sent": "Gra została wysłana graczom",
        "world_join_idle": "Gra hosta dotrze sama, gdy tylko się połączysz",
        "world_receiving_wait": "Host zapisuje swoją grę...",
        "world_receiving": "Odbieram grę hosta ({}%)...",
        "world_arrived": "Gra hosta dotarła - kliknij w The Sims 4 „Load host's game”",
        "world_opening": "Otwieram grę hosta...",
        "world_in": "Jesteś w grze hosta",
        "world_manual": "Gra hosta dotarła - otwórz ją ręcznie: Wczytaj grę, zapis z nazwą hosta",
        "hl_host_idle": "Pokój {} jest Twój. Włącz The Sims 4 i wczytaj rodzinę - pokój otworzy się sam. Podaj ten kod znajomym.",
        "hl_join_idle": "Dołączysz do pokoju {}. Włącz The Sims 4 - połączysz się, gdy tylko host będzie w grze, a jego gra otworzy się sama.",
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


def install_mod(src=None, mods=None, running=None, on_step=None):
    """Put the bundled mod into Mods. -> (state, [moved stray files])
    state: 'none' no bundled mod | 'current' already there | 'installed' | 'updated' |
           'restart' put in while the game runs (loads next start) | 'close' can't replace while the game runs
    on_step('installing' | 'replacing') is called just before the file is written, so a window
    can say "replacing the mod..." while it happens."""
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
    if on_step is not None:
        on_step("replacing" if had else "installing")
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


ROWS = ("mod", "options", "server", "game", "link", "world")
ICON = {"idle": "–", "busy": "…", "ok": "✔", "problem": "✖"}
COLOR = {"idle": "#6e7781", "busy": "#9a6700", "ok": "#1a7f37", "problem": "#cf222e"}

MOD_STEP = {None: ("idle", None), "checking": ("busy", "mod_checking"), "installing": ("busy", "mod_installing"),
            "replacing": ("busy", "mod_replacing"), "installed": ("ok", "mod_installed"), "updated": ("ok", "mod_updated"),
            "current": ("ok", "mod_current"), "restart": ("problem", "mod_restart"), "close": ("problem", "mod_close"),
            "none": ("problem", "mod_none")}
OPT_STEP = {None: ("idle", None), "checking": ("busy", "opt_checking"), "ok": ("ok", "opt_ok"), "fixed": ("ok", "opt_fixed"),
            "close": ("problem", "opt_close"), "missing": ("idle", "opt_missing")}


def steps(cfg, status, running, setup=None, relay=None, starting=False, lang=None):
    """Every stage between "window opened" and "playing together", each with where it stands.

    -> [(row, level, text)] for ROWS, level one of idle / busy / ok / problem.
    cfg: the launcher config.  status: read_status().  running: is the game's process there.
    setup: {"mod": state, "moved": [...], "options": state} from the install step, states as
    install_mod / fix_options return them plus the in-progress 'checking', 'installing', 'replacing'.
    relay: None (not checked) | 'checking' | True | False | 'none' (no address).
    starting: the game was only just seen running - no status yet still means "loading"."""
    t = lambda key, *args: tr(key, *args, lang=lang)
    setup = setup or {}
    mode, code = cfg.get("mode"), clean_code(cfg.get("room_code")) or "?"
    wants = mode in ("host", "join")
    out = []

    level, key = MOD_STEP.get(setup.get("mod"), ("idle", None))
    text = t(key) if key else ICON["idle"]
    if setup.get("moved"):
        text += t("mod_moved", ", ".join(setup["moved"]))
    out.append(("mod", level, text))

    level, key = OPT_STEP.get(setup.get("options"), ("idle", None))
    out.append(("options", level, t(key) if key else ICON["idle"]))

    if relay == "checking":
        out.append(("server", "busy", t("srv_checking")))
    elif relay is True:
        out.append(("server", "ok", t("srv_ok")))
    elif relay is False:
        out.append(("server", "problem", t("srv_down", str(cfg.get("relay") or DEFAULT_RELAY))))
    elif relay == "none":
        out.append(("server", "problem", t("srv_none")))
    else:
        out.append(("server", "idle", ICON["idle"]))

    if status is not None:
        out.append(("game", "ok", t("game_ok")))
    elif not running:
        out.append(("game", "idle", t("game_off")))
    elif starting:
        out.append(("game", "busy", t("game_starting")))
    else:
        out.append(("game", "problem", t("game_no_mod")))

    if status is None:
        key = {"host": "link_host_idle", "join": "link_join_idle"}.get(mode)
        out.append(("link", "idle", t(key, code) if key else t("link_none")))
        out.append(("world", "idle", t({"host": "world_host_idle", "join": "world_join_idle"}.get(mode, "world_none"))))
        return out

    state, room = status.get("state"), str(status.get("room") or "").upper()
    players = [str(x) for x in status.get("players") or []]
    error = status.get("error")
    if state not in ("idle", None) and wants and room and room != code:
        out.append(("link", "problem", t("link_other_room", room, code)))
    elif error and state not in ("hosting", "connected"):
        key = "err_" + str(error)
        out.append(("link", "problem", t(key, room or code) if key in TEXT["en"] else t("err_other", error)))
    elif state == "hosting":
        others = len(players) > 1
        out.append(("link", "ok", t("link_hosting", room, ", ".join(players)) if others else t("link_hosting_alone", room)))
    elif state == "connected":
        out.append(("link", "ok", t("link_connected", room, ", ".join(players) or "?")))
    elif state == "waiting for the host":
        out.append(("link", "busy", t("link_waiting", room)))
    elif state == "joining":
        out.append(("link", "busy", t("link_joining", room or code)))
    elif state == "reconnecting":
        out.append(("link", "busy", t("link_reconnecting")))
    elif wants:
        out.append(("link", "busy", t("link_soon")))
    else:
        out.append(("link", "idle", t("link_off")))

    hand = status.get("handoff") or {}
    phase, names = hand.get("phase") or "", ", ".join(str(n) for n in hand.get("names") or []) or "?"
    progress = hand.get("progress")
    if status.get("host"):
        world = {"need_household": ("busy", t("world_need_household", names)),
                 "need_save": ("problem", t("world_need_save", names)),
                 "saving": ("busy", t("world_saving", names)),
                 "sending": ("busy", t("world_sending")),
                 "sent": ("ok", t("world_sent"))}.get(phase, ("idle", t("world_host_idle") if state == "hosting" else t("world_none")))
    else:
        world = {"receiving": ("busy", t("world_receiving", progress) if progress is not None else t("world_receiving_wait")),
                 "arrived": ("problem", t("world_arrived")),
                 "opening": ("busy", t("world_opening")),
                 "in_host_game": ("ok", t("world_in")),
                 "open_manually": ("problem", t("world_manual"))}.get(
                     phase, ("idle", t("world_join_idle") if state not in ("idle", None) else t("world_none")))
    out.append(("world",) + world)
    return out


def headline(cfg, rows, lang=None):
    """One sentence: what the player should do, or what is happening, right now."""
    by = {row: (level, text) for row, level, text in rows}
    for row in ROWS:                               # something needs the player: say that first
        if by[row][0] == "problem":
            return by[row][1]
    if by["game"][0] == "idle":                    # game not running: what to do next
        code = clean_code(cfg.get("room_code")) or "?"
        if cfg.get("mode") == "host":
            return tr("hl_host_idle", code, lang=lang)
        if cfg.get("mode") == "join":
            return tr("hl_join_idle", code, lang=lang)
        return tr("link_none", lang=lang)
    for row in ("world", "link", "game", "mod", "options", "server"):   # the furthest stage that is moving
        if by[row][0] == "busy":
            return by[row][1]
    for row in ("world", "link", "game"):
        if by[row][0] == "ok":
            return by[row][1]
    return by["link"][1]


def journal(entries, before, rows, now=None, keep=8):
    """Add a line to the event log for every stage whose state changed. Percentages don't count
    as a change ("receiving 12%" -> "receiving 40%" is one event). -> the new `before`."""
    seen = dict(before or {})
    for row, level, text in rows:
        mark = (level, re.sub(r"\d+%", "%", text))
        if seen.get(row) == mark:
            continue
        first = row not in seen
        seen[row] = mark
        if level == "idle" and first:
            continue                               # nothing has happened in this stage yet
        entries.append("{}  {}".format(time.strftime("%H:%M:%S", time.localtime(now or time.time())), text))
    del entries[:-keep]
    return seen


# ---------------------------------------------------------------- window
BUSY_SHOWN = 0.7        # seconds an in-progress state stays up, so a quick step is still seen


def main():
    import tkinter as tk
    from tkinter import ttk

    cfg = load_config()
    root = tk.Tk()
    root.title("S4OM Online")
    root.resizable(False, False)
    pad = {"padx": 12, "pady": 5}

    name_var = tk.StringVar(value=clean_name(cfg.get("name")))
    relay_var = tk.StringVar(value=str(cfg.get("relay") or DEFAULT_RELAY))
    code_var = tk.StringVar()
    shown_code = tk.StringVar(value=hosting_code(cfg) or "")
    headline_var = tk.StringVar()
    state = {"busy": False, "message_until": 0.0, "running_since": None, "seen": {}, "log": [],
             "setup": {"mod": None, "moved": [], "options": None}, "relay": None}
    widgets = {}

    def flash(text, seconds=8):
        """A message about the last click; the live headline takes over again after a moment."""
        state["message_until"] = time.time() + seconds
        headline_var.set(text)

    def relay_or_complain():
        relay = clean_relay(relay_var.get())
        if relay is None:
            state["relay"] = "none"
            flash(tr("need_server"))
        return relay

    def from_thread(fn):
        root.after(0, fn)

    def do_setup():
        """Mod and game settings, each reported while it happens."""
        def show(key, value):
            from_thread(lambda: (state["setup"].__setitem__(key, value), refresh()))

        def work():
            running = game_running()
            show("mod", "checking")
            time.sleep(BUSY_SHOWN)

            def step(what):
                show("mod", what)
                time.sleep(BUSY_SHOWN)
            mod_state, moved = install_mod(running=running, on_step=step)
            show("moved", moved)
            show("mod", mod_state)
            show("options", "checking")
            time.sleep(BUSY_SHOWN)
            show("options", fix_options(running=running))
        threading.Thread(target=work, daemon=True).start()

    def check_server(then=None):
        relay = clean_relay(relay_var.get())
        if relay is None:
            state["relay"] = "none"
            refresh()
            return
        if state["busy"]:
            return
        state["busy"] = True
        state["relay"] = "checking"
        refresh()

        def work():
            started = time.time()
            ok = relay_reachable(relay)
            time.sleep(max(0.0, BUSY_SHOWN - (time.time() - started)))

            def done():
                state["busy"] = False
                state["relay"] = bool(ok)
                refresh()
                if then is not None and ok:
                    then()
            from_thread(done)
        threading.Thread(target=work, daemon=True).start()

    def start_session(make):
        if relay_or_complain() is None:
            return

        def go():
            new = make(load_config())
            save_config(new)
            shown_code.set(hosting_code(new) or "")
            state["message_until"] = 0.0
            refresh()
            do_setup()
        check_server(go)

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
        refresh(keep_message=True)

    def on_copy():
        if shown_code.get():
            root.clipboard_clear()
            root.clipboard_append(shown_code.get())
            flash(tr("copied", shown_code.get()), 4)

    def refresh(keep_message=False):
        running, status = game_running(), read_status()
        if not running or status is not None:
            state["running_since"] = None
        elif state["running_since"] is None:
            state["running_since"] = time.time()
        # the mod loads some way into the game's start-up: only call it missing after a while
        starting = state["running_since"] is not None and time.time() - state["running_since"] < MOD_LOAD_GRACE
        current = load_config()
        rows = steps(current, status, running, state["setup"], state["relay"], starting)
        for row, level, text in rows:
            icon, label = widgets[row]
            icon.configure(text=ICON[level], foreground=COLOR[level])
            label.configure(text=text, foreground="#1f2328" if level != "idle" else COLOR["idle"])
        state["seen"] = journal(state["log"], state["seen"], rows)
        log_box.configure(state="normal")
        log_box.delete("1.0", "end")
        log_box.insert("end", "\n".join(state["log"]))
        log_box.configure(state="disabled")
        log_box.see("end")
        if keep_message or time.time() < state["message_until"]:
            return
        headline_var.set(headline(current, rows))

    def tick():
        try:
            refresh()
        finally:
            root.after(2000, tick)

    frm = ttk.Frame(root, padding=10)
    frm.grid()
    ttk.Label(frm, text=tr("name")).grid(row=0, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=name_var, width=24).grid(row=0, column=1, columnspan=2, sticky="w", **pad)
    if not DEFAULT_RELAY or cfg.get("relay"):       # a build with its server baked in doesn't need this row
        ttk.Label(frm, text=tr("server")).grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=relay_var, width=34).grid(row=1, column=1, columnspan=2, sticky="w", **pad)

    ttk.Separator(frm).grid(row=2, column=0, columnspan=3, sticky="ew", pady=6)
    ttk.Button(frm, text=tr("host"), command=on_host).grid(row=3, column=0, sticky="ew", **pad)
    tk.Label(frm, textvariable=shown_code, font=("Consolas", 22, "bold"), width=9).grid(row=3, column=1, **pad)
    ttk.Button(frm, text=tr("copy"), command=on_copy).grid(row=3, column=2, sticky="ew", **pad)

    ttk.Button(frm, text=tr("join"), command=on_join).grid(row=4, column=0, sticky="ew", **pad)
    code_entry = ttk.Entry(frm, textvariable=code_var, width=12, font=("Consolas", 14))
    code_entry.grid(row=4, column=1, **pad)
    code_entry.bind("<Return>", lambda e: on_join())
    ttk.Label(frm, text=tr("code_hint")).grid(row=4, column=2, sticky="w", **pad)

    ttk.Separator(frm).grid(row=5, column=0, columnspan=3, sticky="ew", pady=6)
    ttk.Label(frm, textvariable=headline_var, wraplength=500, justify="left", font=("Segoe UI", 10, "bold")).grid(
        row=6, column=0, columnspan=3, sticky="w", **pad)

    box = ttk.Frame(frm)
    box.grid(row=7, column=0, columnspan=3, sticky="w", padx=12, pady=4)
    for n, row in enumerate(ROWS):
        icon = ttk.Label(box, text=ICON["idle"], width=2, font=("Segoe UI Symbol", 11))
        icon.grid(row=n, column=0, sticky="nw", pady=2)
        ttk.Label(box, text=tr("row_" + row), width=15).grid(row=n, column=1, sticky="nw", pady=2)
        label = ttk.Label(box, text="", wraplength=360, justify="left")
        label.grid(row=n, column=2, sticky="nw", pady=2)
        widgets[row] = (icon, label)

    ttk.Label(frm, text=tr("log"), foreground=COLOR["idle"]).grid(row=8, column=0, columnspan=3, sticky="w", padx=12, pady=(8, 0))
    log_box = tk.Text(frm, height=8, width=70, state="disabled", relief="flat", background="#f6f8fa",
                      font=("Segoe UI", 9), wrap="word")
    log_box.grid(row=9, column=0, columnspan=3, sticky="w", padx=12, pady=(2, 6))
    ttk.Button(frm, text=tr("alone"), command=on_offline).grid(row=10, column=0, columnspan=2, sticky="w", **pad)

    refresh()
    do_setup()
    root.after(900, check_server)
    root.after(1500, tick)
    root.mainloop()


if __name__ == "__main__":
    main()
