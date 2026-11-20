# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\world_flow.py
# Compiled at: 2026-11-15 17:19:38
# Size of source mod 2**32: 6760 bytes

"""Leaving the lot: Manage Worlds, the main menu, or travel the mod didn't start.

The game has no Python event for Manage Worlds - from Python it's just "this lot shut down
and no new one has loaded yet". Two game hooks cover it:

  Zone.on_teardown                          a player's lot is shutting down
  Zone.on_loading_screen_animation_finished a lot finished loading

Host leaves  -> joiners get "host_left" : notification + their clock is paused, so nobody plays
                on alone while the host is in Manage Worlds or loading somewhere
Host is back -> joiners get "host_back" : same lot -> carry on (host's speed comes with it);
                another lot -> the usual travel follow (lobby.on_host_zone_changed) takes them
Joiner leaves on their own (their Manage Worlds / main menu) -> the host and others are told;
                when they come back, desync repair puts them on the host's lot again.
"""

import time

from multimod import network

_hooked = False

_host_away_since = None

_following = False


def _my_name():
    try:
        return str((network._load_launcher_config() or {}).get("name") or "A player")[:16]
    except Exception:
        return "A player"


def _in_session():
    return bool(network._clients) or network._client_socket is not None


def _zone_id():
    try:
        import services

        return int(services.current_zone_id() or 0)
    except Exception:
        return 0


def _send(msg):
    if network._is_host:
        network._broadcast(msg)
    else:
        if network._client_socket is not None:
            network._send_json(network._client_socket, msg)


def on_lot_teardown(zone_id):
    global _following
    network._log(
        "world_flow: lot {} teardown (session={}, host={})".format(zone_id, _in_session(), network._is_host)
    )
    try:
        from multimod import buy_sync

        buy_sync.lot_down()
    except Exception as e:
        network._log("world_flow: buy hold failed: {}".format(e))

    if not _in_session():
        return
    if network._is_host:
        network._log("world_flow: host left lot {}".format(zone_id))
        _send({"type": "host_left", "zone": zone_id})
    else:
        if not _following:
            network._log("world_flow: left lot {} on my own".format(zone_id))
            _send({"type": "player_left_lot", "name": _my_name(), "zone": zone_id})


def on_lot_loaded(zone_id):
    global _following
    _following = False
    network._log("world_flow: lot {} loaded (session={})".format(zone_id, _in_session()))
    try:
        from multimod import buy_sync

        buy_sync.lot_up()
    except Exception as e:
        network._log("world_flow: buy baseline failed: {}".format(e))

    try:
        if (network._load_launcher_config() or {}).get("dev_console") is True:
            from multimod import core_hooks

            core_hooks.install_on_tick_hook()
    except Exception as e:
        network._log("world_flow: dev tick failed: {}".format(e))

    if not _in_session():
        return
    if network._is_host:
        try:
            from multimod import clock_sync

            speed = clock_sync.shareable_speed(clock_sync.get_current_speed() or 1)
        except Exception:
            speed = 1

        network._log("world_flow: host back on lot {} (speed {})".format(zone_id, speed))
        _send({"type": "host_back", "zone": zone_id, "speed": speed})


def on_host_left(data):
    global _host_away_since
    _host_away_since = time.time()
    try:
        from multimod import clock_sync

        clock_sync.apply_remote_clock(0)
    except Exception as e:
        network._log("world_flow pause: {}".format(e))

    network._notify(
        "S4OM: the host left the lot (Manage Worlds, the main menu or travel). Your game is paused - you'll follow them when they're back."
    )


def on_host_back(data):
    global _following
    global _host_away_since
    _host_away_since = None
    hz = int(data.get("zone") or 0)
    if hz:
        if hz != _zone_id():
            _following = True
            network._notify("S4OM: the host is on another lot - taking you there.")
            return
    try:
        from multimod import clock_sync

        clock_sync.apply_remote_clock(int(data.get("speed", 1)))
    except Exception as e:
        network._log("world_flow resume: {}".format(e))

    network._notify("S4OM: the host is back - carry on!")


def on_player_left(data):
    network._notify(
        "S4OM: {} left the lot - they'll be brought back when they return.".format(
            str(data.get("name", "A player"))[:16]
        )
    )


def handle(mtype, data):
    {"host_left": on_host_left, "host_back": on_host_back, "player_left_lot": on_player_left}[mtype](data)


def install():
    global _hooked
    if _hooked:
        return
    import zone as zone_mod

    Zone = zone_mod.Zone
    if not getattr(Zone.on_teardown, "_s4om_patched", False):
        orig_down = Zone.on_teardown

        def on_teardown(self, *args, **kwargs):
            try:
                on_lot_teardown(int(getattr(self, "id", 0) or 0))
            except Exception as e:
                network._log("world_flow teardown: {}".format(e))

            return orig_down(self, *args, **kwargs)

        on_teardown._s4om_patched = True
        Zone.on_teardown = on_teardown
    if not getattr(Zone.on_loading_screen_animation_finished, "_s4om_patched", False):
        orig_up = Zone.on_loading_screen_animation_finished

        def on_loaded(self, *args, **kwargs):
            res = orig_up(self, *args, **kwargs)
            try:
                on_lot_loaded(int(getattr(self, "id", 0) or 0))
            except Exception as e:
                network._log("world_flow loaded: {}".format(e))

            return res

        on_loaded._s4om_patched = True
        Zone.on_loading_screen_animation_finished = on_loaded
    _hooked = True
    network._log("world_flow: lot leave/arrive hooks installed")


try:
    install()
except Exception as _e:
    network._log("world_flow: hook failed: {}".format(_e))
