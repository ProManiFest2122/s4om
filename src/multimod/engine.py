# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\engine.py
# Compiled at: 2026-11-15 21:32:29
# Size of source mod 2**32: 19553 bytes

"""Native game functions S4OM calls directly (through ctypes, see native.py).

These are the functions the game's own screens call - Load Game, Place Lot from the
Library - so S4OM can do those steps for the player. They live inside TS4_x64.exe, so
each is listed per game version with its first bytes; if a patch moves or changes one,
the bytes won't match and S4OM falls back to asking the player to click instead of
calling something wrong.

To support a new game version: find each function again (same handlers, see the notes)
and add the version below.
"""

import os, re

from multimod import network

KNOWN = {
    "1.128.90.1030": {
        "load_slot": (5370409712, "405355565741564883ec4048c7442430feffffff448bf133ff893da161780240"),
        "apply_blueprint": (5379203552, "4c8bd1488b0dfe4ef5014885c97411488b01440fb6c24533"),
        "bb_ctx": (5379020480, "488b05c916f801c3cccccccccccccccc"),
        "bb_prepare": (5379020544, "4057415641574883ec5048c7442440feffffff48895c2478"),
        "bp_msg_init": (5370814496, "48894c24085356574883ec3048c7442420feffffff488bf9"),
        "msg_service": (5389697504, "488b05595d5801c3"),
        "lot_confirm_ui": (5383549680, "4883ec28e8e7ce5d004885c07415"),
        "bp_save_ui": (5383550350, "e84dcc5d004885c0741b488b084c8b51304533c94c8d4424"),
    },
    "1.129.39.1030": {
        "load_slot": (5370410864, "405355565741564883ec4048c7442430feffffff448bf133ff893d216d780240"),
        "apply_blueprint": (5379204640, "4c8bd1488b0d1e5bf5014885c97411488b01440fb6c24533"),
        "bb_ctx": (5379021568, "488b05e922f801c3cccccccccccccccc"),
        "bb_prepare": (5379021632, "4057415641574883ec5048c7442440feffffff48895c2478"),
        "bp_msg_init": (5370815648, "48894c24085356574883ec3048c7442420feffffff488bf9"),
        "msg_service": (5389701184, "488b05395f5801c3"),
        "lot_confirm_ui": (5383551712, "4883ec28e857d55d004885c07415"),
        "bp_save_ui": (5383552382, "e8bdd25d004885c0741b488b084c8b51304533c94c8d4424"),
    },
}

_cache = {}


def game_version():
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(network.CMD_PATH)), "GameVersion.txt")
        with open(path, "rb") as f:
            m = re.search(b"\\d+\\.\\d+\\.\\d+\\.\\d+", f.read(200))
        if m:
            return m.group(0).decode()
        return ""
    except OSError:
        return ""


def fn(name, restype, *argtypes):
    """ctypes function for `name`, or (None, reason). Checked once, then cached."""
    key = (name, restype, argtypes)
    if key in _cache:
        return _cache[key]
    version = game_version()
    table = KNOWN.get(version)
    if not table or name not in table:
        res = (None, "game version {} not supported yet".format(version or "?"))
    else:
        (addr, sig) = table[name]
        from multimod import native

        (ctypes, _how) = native.load_ctypes()
        got = ctypes.string_at(addr, len(sig) // 2).hex()
        if got != sig:
            res = (None, "{} not where expected (bytes {})".format(name, got[:16]))
        else:
            res = ((ctypes.CFUNCTYPE)(restype, *argtypes)(addr), "ok")
    _cache[key] = res
    return res


def apply_lot(tray_id, furnished=True, keep_venue=False, prepare=True):
    """Place a Library lot (Tray instance id) on the active lot, like Place Lot in build mode.
    Returns (ok, message)."""
    from multimod import native

    ctypes, _how = native.load_ctypes()
    apply_bp, why = fn("apply_blueprint", ctypes.c_bool, ctypes.c_uint64, ctypes.c_uint8)
    ctx_fn, why2 = fn("bb_ctx", ctypes.c_void_p)
    prep, why3 = fn("bb_prepare", ctypes.c_bool, ctypes.c_void_p, ctypes.c_uint8)
    if not (apply_bp and ctx_fn and prep):
        return (False, why if not apply_bp else why2 if not ctx_fn else why3)
    if prepare:
        ctx = ctx_fn()
        network._log("engine: apply_lot ctx {}".format(hex(ctx or 0)))
        if ctx:
            prep(ctx, 1)
            network._log("engine: apply_lot prepared")
    flags = (1 if furnished else 0) | (2 if keep_venue else 0)
    network._log("engine: apply_lot calling apply {:x} flags {}".format(int(tray_id), flags))
    ok = bool(apply_bp(ctypes.c_uint64(int(tray_id)), flags))
    network._log("engine: apply_lot {:x} flags {} -> {}".format(int(tray_id), flags, ok))
    return (ok, "placed" if ok else "the game refused the lot")


SAVE_BLUEPRINT_MSG = 874057848


def _short_str(ctypes, addr, text):
    """The game's 16-byte string, short form: chars inline, last byte = 15 - length."""
    raw = text.encode("utf-8")[:15]
    ctypes.memset(addr, 0, 16)
    ctypes.memmove(addr, raw, len(raw))
    ctypes.c_uint8.from_address(addr + 15).value = 15 - len(raw)


def save_lot(name, bedrooms=0, bathrooms=0):
    """Save the active lot to My Library, like the build-mode "Save to My Library" button.
    Must run on the game's main thread (on_main_thread). Returns (ok, message)."""
    from multimod import native

    (ctypes, _how) = native.load_ctypes()
    (init, why) = fn("bp_msg_init", ctypes.c_void_p, ctypes.c_void_p)
    (service, why2) = fn("msg_service", ctypes.c_void_p)
    (guard, why3) = fn("bp_save_ui", ctypes.c_void_p)
    if not (init and service and guard):
        return (False, why if (not init) else why2 if (not service) else why3)
    buf = ctypes.create_string_buffer(256)
    a = ctypes.addressof(buf)
    init(a)
    ctypes.c_uint8.from_address(a + 128).value = 1
    ctypes.c_uint64.from_address(a + 136).value = 0
    _short_str(ctypes, a + 144, name)
    _short_str(ctypes, a + 160, "")
    _short_str(ctypes, a + 176, "")
    ctypes.c_uint64.from_address(a + 192).value = 0
    ctypes.c_uint16.from_address(a + 192).value = int(bedrooms)
    ctypes.c_uint16.from_address(a + 194).value = int(bathrooms)
    ctypes.c_uint8.from_address(a + 196).value = 0
    ctypes.c_uint8.from_address(a + 197).value = 0
    ctypes.c_uint8.from_address(a + 198).value = 1
    ctypes.c_uint8.from_address(a + 200).value = 0
    svc = service()
    if not svc:
        return (False, "the game's message service isn't up")
    vt = ctypes.c_void_p.from_address(svc).value
    post = ctypes.CFUNCTYPE(
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p
    )(ctypes.c_void_p.from_address(vt + 48).value)
    network._log("engine: save_lot posting {!r}".format(name))
    post(svc, SAVE_BLUEPRINT_MSG, a, None)
    network._log("engine: save_lot posted")
    return (True, "saved")


CONFIRM_LOT_MSG = 3155669766


def confirm_lot():
    """Click the check mark under a lot being placed (apply_lot only opens the placement).
    Main thread only. Returns (ok, message)."""
    from multimod import native

    (ctypes, _how) = native.load_ctypes()
    (service, why) = fn("msg_service", ctypes.c_void_p)
    (guard, why2) = fn("lot_confirm_ui", ctypes.c_void_p)
    if not (service and guard):
        return (False, why if (not service) else why2)
    svc = service()
    if not svc:
        return (False, "the game's message service isn't up")
    vt = ctypes.c_void_p.from_address(svc).value
    post = ctypes.CFUNCTYPE(
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p
    )(ctypes.c_void_p.from_address(vt + 48).value)
    post(svc, CONFIRM_LOT_MSG, None, None)
    return (True, "confirmed")


WM_S4OM = 34100

_main = {"hwnd": None, "old": None, "proc": None, "jobs": [], "on": False}

import threading as _threading

_main_lock = _threading.Lock()


def _game_hwnd(ctypes):
    from ctypes import wintypes

    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    me = k32.GetCurrentProcessId()
    found = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _l):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me:
            if u32.IsWindowVisible(hwnd):
                if u32.GetWindowTextLengthW(hwnd) > 0:
                    found.append(hwnd)
        return True

    u32.EnumWindows(EnumProc(cb), 0)
    if found:
        return found[0]


def _remove_main_hook(u32, ctypes):
    """Put the game's own window handler back. Caller holds _main_lock. If something else
    (an overlay) subclassed the window after us, leave ours chained in rather than cut it off."""
    if not _main["on"]:
        return
    cur = u32.GetWindowLongPtrW(_main["hwnd"], -4)
    mine = ctypes.cast(_main["proc"], ctypes.c_void_p).value
    if cur != mine:
        network._log("engine: window handler changed by something else - leaving the hook in")
        return
    u32.SetWindowLongPtrW(_main["hwnd"], -4, _main["old"])
    _main["on"] = False
    network._log("engine: main-thread hook removed")


def _install_main_hook():
    """Caller holds _main_lock."""
    if _main["on"]:
        return True
    from multimod import native

    (ctypes, _how) = native.load_ctypes()
    from ctypes import wintypes

    u32 = ctypes.windll.user32
    hwnd = _game_hwnd(ctypes)
    if not hwnd:
        return False
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    u32.SetWindowLongPtrW.restype = ctypes.c_void_p
    u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    u32.GetWindowLongPtrW.restype = ctypes.c_void_p
    u32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    u32.CallWindowProcW.restype = LRESULT
    u32.CallWindowProcW.argtypes = [
        ctypes.c_void_p,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    if _main["proc"] is None:

        def proc(h, msg, w, l):
            if msg == WM_S4OM:
                with _main_lock:
                    jobs, _main["jobs"] = _main["jobs"], []
                for job in jobs:
                    try:
                        job()
                    except Exception as e:
                        network._log("engine: main-thread job failed: {}".format(e))

                with _main_lock:
                    if not _main["jobs"]:
                        _remove_main_hook(u32, ctypes)
                return 0
            return u32.CallWindowProcW(_main["old"], h, msg, w, l)

        _main["proc"] = WNDPROC(proc)
    _main["hwnd"] = hwnd
    _main["old"] = u32.SetWindowLongPtrW(hwnd, -4, ctypes.cast(_main["proc"], ctypes.c_void_p))
    _main["on"] = True
    network._log("engine: main-thread hook on window {:x}".format(hwnd))
    return True


def on_main_thread(job):
    """Queue job() to run on the game's UI thread. Returns False if that isn't available."""
    from multimod import native

    ctypes, _how = native.load_ctypes()
    with _main_lock:
        if not _install_main_hook():
            return False
        _main["jobs"].append(job)
        ctypes.windll.user32.PostMessageW(_main["hwnd"], WM_S4OM, 0, 0)
    return True


(WM_KEYDOWN, WM_KEYUP) = (256, 257)

(VK_F2, SC_F2) = (113, 60)


def post_key(vk=VK_F2, scan=SC_F2):
    """Send a key press to the game's OWN window (not a global keypress - nothing else on the
    desktop gets it). F2 = Build mode, like the player pressing it."""
    from multimod import native

    ctypes, _how = native.load_ctypes()
    hwnd = _main["hwnd"] or _game_hwnd(ctypes)
    if not hwnd:
        return False
    u32 = ctypes.windll.user32
    u32.PostMessageW(hwnd, WM_KEYDOWN, vk, 1 | scan << 16)
    u32.PostMessageW(hwnd, WM_KEYUP, vk, 1 | scan << 16 | 1073741824 | 2147483648)
    return True


def in_build_mode():
    try:
        import services

        return bool(services.current_zone().is_in_build_buy)
    except Exception:
        return False


def thread_info():
    from multimod import native

    ctypes, _how = native.load_ctypes()
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = _game_hwnd(ctypes)
    return {
        "python_thread": (k32.GetCurrentThreadId)(),
        "window_thread": u32.GetWindowThreadProcessId(hwnd, None) if hwnd else None,
    }


_events = set()


def _install_event_logs():
    import build_buy

    for name in (
        "c_api_on_apply_blueprint_lot_begin",
        "c_api_on_apply_blueprint_lot_end",
        "c_api_on_lot_clearing_begin",
        "c_api_on_lot_clearing_end",
    ):
        orig = getattr(build_buy, name, None)
        if orig is None or getattr(orig, "_s4om_patched", False):
            continue

        def make(orig, name):
            def wrapper(*args, **kwargs):
                network._log("engine: game event {}".format(name.replace("c_api_on_", "")))
                _events.add(name.replace("c_api_on_", ""))
                return orig(*args, **kwargs)

            wrapper._s4om_patched = True
            return wrapper

        setattr(build_buy, name, make(orig, name))


try:
    _install_event_logs()
except Exception as _e:
    network._log("engine: event logs failed: {}".format(_e))

try:
    import sims4.commands

    @sims4.commands.Command("mp_lot_apply", command_type=(sims4.commands.CommandType.Live))
    def mp_lot_apply(tray_id=None, flags="1", _connection=None):
        out = sims4.commands.CheatOutput(_connection)
        if not tray_id:
            out("usage: mp_lot_apply <tray id hex, e.g. 0x0d4c1e2f3a4b5c6d> [flags 1=furnished]")
            return
        f = int(flags)
        network._log("engine: threads {}".format(thread_info()))

        def job():
            (ok, msg) = apply_lot(
                (int(str(tray_id), 16)),
                furnished=(bool(f & 1)),
                keep_venue=(bool(f & 2)),
                prepare=(not f & 4),
            )
            network._log("engine: main-thread apply -> {} ({})".format(ok, msg))

        try:
            if f & 8:
                job()
                out("apply ran on the Python thread")
            elif on_main_thread(job):
                out("apply queued on the game's main thread - see mp_debug.log")
            else:
                out("couldn't reach the game window")
        except Exception as e:
            out("apply error: {}".format(e))
            network._log("engine: mp_lot_apply error {}".format(e))

    @sims4.commands.Command("mp_bb_key", command_type=(sims4.commands.CommandType.Live))
    def mp_bb_key(_connection=None):
        """Testing: press F2 in the game window, then report build mode."""
        import threading, time as _t

        network._log("engine: F2 posted -> {} (in build mode before: {})".format(post_key(), in_build_mode()))

        def later():
            _t.sleep(4)
            network._log("engine: in build mode after F2: {}".format(in_build_mode()))

        threading.Thread(target=later, daemon=True).start()

    @sims4.commands.Command("mp_lot_place", command_type=(sims4.commands.CommandType.Live))
    def mp_lot_place(tray_id=None, settle="6", _connection=None):
        """Testing: F2 -> wait for build mode -> place Library lot -> leave build mode."""
        import threading, time as _t

        tid = int(str(tray_id), 16)
        settle = float(settle)
        confirm_after = 3.0
        _events.clear()
        _install_event_logs()

        def run():
            t0 = _t.time()
            if not in_build_mode():
                post_key()
                while not in_build_mode() and _t.time() - t0 < 15:
                    _t.sleep(0.3)

            network._log(
                "engine: place - build mode {} after {:.1f}s".format(in_build_mode(), _t.time() - t0)
            )
            if not in_build_mode():
                return
            _t.sleep(settle)
            on_main_thread((lambda: network._log("engine: place apply -> {}".format(apply_lot(tid)))))
            _t.sleep(confirm_after)
            on_main_thread((lambda: network._log("engine: place confirm -> {}".format(confirm_lot()))))
            t1 = _t.time()
            while "apply_blueprint_lot_end" not in _events and _t.time() - t1 < 60:
                _t.sleep(0.3)

            network._log(
                "engine: place - lot events {} after {:.1f}s".format(sorted(_events), _t.time() - t1)
            )
            _t.sleep(1.0)
            try:
                import build_buy, services

                build_buy.build_buy_force_exit(services.current_zone_id())
            except Exception as e:
                network._log("engine: place - exit failed {}".format(e))

            _t.sleep(3)
            network._log(
                "engine: place done in {:.1f}s, build mode now {}".format(_t.time() - t0, in_build_mode())
            )

        threading.Thread(target=run, daemon=True).start()

    @sims4.commands.Command("mp_lot_save", command_type=(sims4.commands.CommandType.Live))
    def mp_lot_save(name="S4OM test", _connection=None):
        """Testing: save the active lot to My Library (no click)."""

        def job():
            try:
                (ok, msg) = save_lot(name)
            except Exception as e:
                ok, msg = False, str(e)

            network._log("engine: save_lot -> {} ({})".format(ok, msg))

        if not on_main_thread(job):
            network._log("engine: save_lot - couldn't reach the game window")

except Exception:
    pass
