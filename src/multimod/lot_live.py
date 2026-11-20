# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\lot_live.py
# Compiled at: 2026-10-27 01:17:18
# Size of source mod 2**32: 14218 bytes

"""S4OM live building: the host's walls/floors/paint reach everyone in seconds, no reload.

The lot's structure lives in the game's C++ side; what the game CAN do is save a lot to My
Library and place a Library lot onto the current lot with the Sims still on it. Both are
done here without clicks (engine.py):
  host, leaving build mode after changing the structure (resync.py decides):
    save_lot -> the new Library files (Tray) -> "lot_live" message with the files, the
    household's funds and where every object is
  joiner:
    files into its Tray -> F2 (build mode) -> place the lot -> confirm -> leave build mode
    -> funds as on the host -> buy_sync learns the new object ids (it is held while the lot
    is replaced, or it would send every cleared object to the host as deleted)
Anything failing -> the old way: a full re-sync (host) / "lot_live_failed" to the host.
"""

import base64, os, re, threading, time

from multimod import network

MAX_BYTES = 4194304

_TRAY_NAME = re.compile("^0x[0-9a-fA-F]{8}!0x([0-9a-fA-F]{16})\\.(blueprint|bpi|trayitem)$")

_busy = False

FALLBACK_GAP = 60.0

_last_fallback = 0.0


def may_fallback(last, now, gap=FALLBACK_GAP):
    """Pure (tested)."""
    return now - last >= gap


def _tray_dir():
    from multimod import lot_share

    return lot_share._tray_dir()


def new_lot_files(names_before, names_after):
    """Tray file names of lots that appeared -> {tray_id_hex: [names]} (only complete lots:
    a .blueprint and a .trayitem)."""
    out = {}
    for n in set(names_after) - set(names_before):
        m = _TRAY_NAME.match(n)
        if m:
            out.setdefault(m.group(1).lower(), []).append(n)

    return {
        k: sorted(v)
        for k, v in out.items()
        if any((x.endswith(".blueprint") for x in v))
        if any((x.endswith(".trayitem") for x in v))
    }


def write_order(names):
    """Pure (tested). The game watches its Tray folder and registers an item the moment its
    .trayitem appears - so that file goes LAST, after everything it refers to (verified on the box:
    written in the middle, the game refuses to place the lot)."""
    return sorted(names, key=(lambda n: (n.endswith(".trayitem"), n)))


def safe_name(name):
    return bool(_TRAY_NAME.match(name or ""))


def match_objects(host_objs, local_objs, max_dist=0.6):
    """host_objs [(id, def, x, y, z)], local_objs [(id, def, x, y, z)] -> {host_id: local_id}:
    same object type, nearest spot, each local object used once."""
    used, out = set(), {}
    for hid, hdef, hx, hy, hz in host_objs:
        best, bd = None, max_dist
        for lid, ldef, lx, ly, lz in local_objs:
            if not ldef != hdef:
                if lid in used:
                    continue
                d = abs(lx - hx) + abs(ly - hy) + abs(lz - hz)
                if d <= bd:
                    best, bd = lid, d

        if best is not None:
            used.add(best)
            out[int(hid)] = int(best)

    return out


def _objs():
    from multimod import buy_sync

    out = []
    for o in buy_sync._iter_lot_objects():
        st = buy_sync._obj_state(o)
        if st:
            out.append((int(o.id), int(st[5]), round(st[0], 2), round(st[1], 2), round(st[2], 2)))

    return out


def send():
    """Host: save the lot and send it. Returns False if it can't start (-> caller re-syncs)."""
    global _busy
    from multimod import engine

    if _busy:
        return False
    tray = _tray_dir()
    try:
        before = set(os.listdir(tray))
    except OSError:
        return False
    result = {}

    def save():
        try:
            result["save"] = engine.save_lot("S4OM build")
        except Exception as e:
            result["save"] = (False, str(e))

    if not engine.on_main_thread(save):
        return False
    _busy = True

    def wait_and_send():
        global _busy
        try:
            try:
                end = time.time() + 15
                lots = {}
                while time.time() < end:
                    time.sleep(0.5)
                    if result.get("save") and not result["save"][0]:
                        break
                    lots = new_lot_files(before, os.listdir(tray))
                    if lots:
                        time.sleep(1.0)
                        lots = new_lot_files(before, os.listdir(tray))
                        break

                if not lots:
                    network._log(
                        "lot_live: no Library lot appeared ({}) - full re-sync".format(result.get("save"))
                    )
                    _fallback("the lot couldn't be saved for live building")
                    return
                (tid, names) = sorted(lots.items())[0]
                files, total = {}, 0
                for n in names:
                    with open(os.path.join(tray, n), "rb") as f:
                        raw = f.read()
                    total += len(raw)
                    files[n] = base64.b64encode(raw).decode()

                if total > MAX_BYTES:
                    network._log("lot_live: lot too big ({} B) - full re-sync".format(total))
                    _fallback("the lot is too big for live building")
                    return
                funds = None
                try:
                    import services

                    funds = int(services.active_household().funds.money)
                except Exception:
                    pass

                network._broadcast(
                    {
                        "type": "lot_live",
                        "tray_id": tid,
                        "files": files,
                        "funds": funds,
                        "objs": _objs(),
                        "ts": (time.time)(),
                    }
                )
                network._log(
                    "lot_live: sent lot {} ({} files, {} B) to everyone".format(tid, len(files), total)
                )
                _cleanup(tray, names)
            except Exception as e:
                network._log("lot_live: send failed: {} - full re-sync".format(e))
                _fallback("live building failed")

        finally:
            _busy = False

    threading.Thread(target=wait_and_send, daemon=True).start()
    return True


def _cleanup(tray, names, delay=20.0):
    """Our sync lots shouldn't pile up in the player's Library."""

    def later():
        time.sleep(delay)
        for n in names:
            try:
                os.remove(os.path.join(tray, n))
            except OSError:
                pass

    threading.Thread(target=later, daemon=True).start()


def _fallback(reason):
    global _last_fallback
    if not may_fallback(_last_fallback, time.time()):
        network._log("lot_live: a re-sync was just started - not another ({})".format(reason))
        return
    _last_fallback = time.time()
    try:
        from multimod import resync

        (ok, msg) = resync.request(reason, auto=True)
        network._log("lot_live: fallback re-sync -> {}".format(msg))
    except Exception as e:
        network._log("lot_live: fallback failed: {}".format(e))


def on_failed(data, sender_pid=None):
    """Host: a joiner couldn't place the lot."""
    if network._is_host:
        network._log("lot_live: player {} couldn't place the lot ({})".format(sender_pid, data.get("why")))
        _fallback("a player couldn't apply the build live")


def on_lot(data):
    global _busy
    if network._is_host or _busy:
        return
    tid = str(data.get("tray_id") or "").lower()
    files = data.get("files") or {}
    if not (re.match("^[0-9a-f]{16}$", tid) and files and all((safe_name(n) for n in files))):
        network._log("lot_live: bad lot message")
        return
    tray = _tray_dir()
    try:
        for n in write_order(files):
            tmp = os.path.join(tray, n + ".tmp")
            with open(tmp, "wb") as f:
                f.write(base64.b64decode(files[n]))
            os.replace(tmp, os.path.join(tray, n))

    except Exception as e:
        _report_fail("couldn't write the lot: {}".format(e))
        return

    _busy = True
    threading.Thread(target=_place, args=(int(tid, 16), data, tray, sorted(files)), daemon=True).start()


def _report_fail(why):
    network._log("lot_live: " + why)
    try:
        network._send_json(network._client_socket, {"type": "lot_live_failed", "why": why[:120]})
    except Exception:
        pass


def _main_call(fn, timeout=15.0):
    """Run fn() on the game's main thread and wait for its (ok, message)."""
    from multimod import engine

    box = {}
    done = threading.Event()

    def job():
        try:
            box["r"] = fn()
        except Exception as e:
            box["r"] = (False, str(e))

        done.set()

    if not engine.on_main_thread(job):
        return (False, "couldn't reach the game window")
    done.wait(timeout)
    return box.get("r", (False, "timed out"))


def swap_targets(host_names, own_names):
    """Pure (tested). {host file: our file} for the files that carry a lot's content (blueprint +
    bpi thumbnails) - matched by the part before the id ('0x00000003!') and the extension. The
    .trayitem stays ours: it is what the game has registered."""

    def key(n):
        if "!" in n:
            return (n.split("!")[0], n.rsplit(".", 1)[-1])

    mine = {key(n): n for n in own_names if key(n)}
    out = {}
    for n in host_names:
        k = key(n)
        if k:
            if k[1] in ("blueprint", "bpi"):
                if k in mine:
                    out[n] = mine[k]

    return out


def _swap_in(files, tray):
    """The game refused the host's lot as unknown. Make the game register an item of its own (save
    the current lot - works at once), then put the host's lot CONTENT into that item's files.
    Returns (tray id as int, [file names]) or None."""
    from multimod import engine

    before = set(os.listdir(tray))
    res = _main_call((lambda: engine.save_lot("S4OM live")))
    network._log("lot_live: swap - own item saved -> {}".format(res))
    mine = {}
    end = time.time() + 12
    while time.time() < end and not mine:
        time.sleep(0.5)
        mine = new_lot_files(before, os.listdir(tray))

    if not mine:
        return
    time.sleep(1.0)
    (tid, own) = sorted(new_lot_files(before, os.listdir(tray)).items())[0]
    pairs = swap_targets(list(files), own)
    for host_name, own_name in pairs.items():
        tmp = os.path.join(tray, own_name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(base64.b64decode(files[host_name]))
        os.replace(tmp, os.path.join(tray, own_name))

    network._log("lot_live: swap - {} files of the host's lot put into our item {}".format(len(pairs), tid))
    return (int(tid, 16), own)


def _place(tray_id, data, tray, names):
    global _busy
    from multimod import engine, buy_sync

    t0 = time.time()
    try:
        try:
            buy_sync.hold(True)
            time.sleep(2.0)
            engine._install_event_logs()
            engine._events.clear()
            if not engine.in_build_mode():
                engine.post_key()
                while not engine.in_build_mode() and time.time() - t0 < 12:
                    time.sleep(0.25)

            if not engine.in_build_mode():
                _report_fail("build mode didn't open (game minimized?)")
                return
            time.sleep(2.5)
            res = _main_call((lambda: engine.apply_lot(tray_id)))
            network._log("lot_live: apply -> {}".format(res))
            if not res[0]:
                swapped = _swap_in(data.get("files") or {}, tray)
                if swapped:
                    names = list(names) + swapped[1]
                    res = _main_call((lambda: engine.apply_lot(swapped[0])))
                    network._log("lot_live: apply (swapped item) -> {}".format(res))
            if not res[0]:
                try:
                    import build_buy, services

                    build_buy.build_buy_force_exit(services.current_zone_id())
                except Exception:
                    pass

                _report_fail("the game refused the lot")
                return
            time.sleep(2.0)
            network._log("lot_live: confirm -> {}".format(_main_call((lambda: engine.confirm_lot()))))
            t1 = time.time()
            while "apply_blueprint_lot_end" not in engine._events and time.time() - t1 < 20:
                time.sleep(0.25)

            placed = "apply_blueprint_lot_end" in engine._events
            time.sleep(0.5)
            try:
                import build_buy, services

                build_buy.build_buy_force_exit(services.current_zone_id())
            except Exception as e:
                network._log("lot_live: leaving build mode failed: {}".format(e))

            if not placed:
                _report_fail("the game didn't place the lot")
                return
            time.sleep(1.5)
            if data.get("funds") is not None:
                try:
                    from multimod import money_sync

                    money_sync.process_message({"type": "money_sync", "funds": data["funds"]})
                except Exception as e:
                    network._log("lot_live: funds failed: {}".format(e))

            mapped = buy_sync.rebaseline(data.get("objs") or [])
            network._log(
                "lot_live: placed the host's lot in {:.1f}s ({} objects matched)".format(
                    time.time() - t0, mapped
                )
            )
            network._notify("The host's build is here.")
        except Exception as e:
            _report_fail("placing failed: {}".format(e))

    finally:
        buy_sync.hold(False)
        _busy = False
        _cleanup(tray, names, delay=5.0)


try:
    import sims4.commands

    @sims4.commands.Command("mp_lot_live", command_type=(sims4.commands.CommandType.Live))
    def mp_lot_live(_connection=None):
        """Testing (host): send the current lot to everyone like after building."""
        network._log("lot_live test: send -> {}".format(send()))

except Exception:
    pass
