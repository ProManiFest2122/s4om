# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\menus.py
# Compiled at: 2026-10-11 21:38:22
# Size of source mod 2**32: 4888 bytes

"""S4OM unpaused menus.

Opening a menu or dialog (Sim info, map, phone, options, pop-up questions...) pauses
The Sims 4 - fine alone, but in multiplayer that froze one player's game while everyone
else kept going, so their game fell behind. During an S4OM session those pause requests
are ignored. Build/Buy mode doesn't pause either, so the builder keeps up with everyone
(their Sims keep living while they build); turn that off with "unpaused_build": false.

Source: clock.GameClock.push_speed - menus push ClockSpeedMode.PAUSED with source
GameSpeedChangeSource.UI_MODAL and reason 'Layout Manager Set Modal Visible' or
'Dialog Manager Set Dialog Visible' (server_commands/clock_commands.py); build mode uses
reason 'Build Buy'. Turn off with launcher config "unpaused_menus": false.
"""

from multimod import network

MENU_PAUSE_REASONS = ("Layout Manager Set Modal Visible", "Dialog Manager Set Dialog Visible")

BUILD_BUY_REASON = "Build Buy"

_installed = False

_orig_push_speed = None

_orig_pop_speed = None

_skipped_build = False


def in_session():
    if network._is_host:
        return bool(network._clients)
    return network._client_socket is not None


def enabled():
    try:
        return (network._load_launcher_config() or {}).get("unpaused_menus", True) is not False
    except Exception:
        return True


def build_unpaused_enabled():
    try:
        return (network._load_launcher_config() or {}).get("unpaused_build", True) is not False
    except Exception:
        return True


def should_skip(speed, source, reason):
    """True when this pause request comes from a menu (or build mode) and we're playing together."""
    try:
        from clock import ClockSpeedMode, GameSpeedChangeSource

        paused, ui_modal = ClockSpeedMode.PAUSED, GameSpeedChangeSource.UI_MODAL
    except Exception:
        (paused, ui_modal) = (0, 1)

    if speed != paused or source != ui_modal or not in_session():
        return False
    reason = str(reason)
    if reason in MENU_PAUSE_REASONS:
        return enabled()
    return reason == BUILD_BUY_REASON and build_unpaused_enabled()


def in_build_mode():
    """Is this player in Build/Buy mode right now?"""
    try:
        import services

        zone = services.current_zone()
        if zone is not None:
            if getattr(zone, "is_in_build_buy", False):
                return True
        from clock import GameSpeedChangeSource

        ctl = services.game_clock_service().speed_controllers[GameSpeedChangeSource.UI_MODAL]
        return any((getattr(r, "reason", "") == BUILD_BUY_REASON for r in ctl))
    except Exception:
        return False


def install():
    global _installed
    global _orig_push_speed
    if _installed:
        return True
    try:
        from clock import GameClock, SpeedRequest
    except Exception:
        return False
    if getattr(GameClock, "_s4om_menus_patched", False):
        _installed = True
        return True
    GameClock._s4om_menus_patched = True
    _orig_push_speed = GameClock.push_speed
    _orig_pop_speed = GameClock.pop_speed

    def _patched_push_speed(self, speed, *args, **kwargs):
        global _skipped_build
        try:
            source = kwargs.get("source", args[0] if args else None)
            reason = kwargs.get("reason", args[2] if len(args) > 2 else "")
            if should_skip(speed, source, reason):
                if str(reason) == BUILD_BUY_REASON:
                    _skipped_build = True
                return SpeedRequest(speed, reason=(str(reason)))
        except Exception as e:
            network._log("menus hook error (game pauses normally): {}".format(e))

        return _orig_push_speed(self, speed, *args, **kwargs)

    def _patched_pop_speed(self, speed=None, *args, **kwargs):
        global _skipped_build
        try:
            reason = kwargs.get("reason", args[1] if len(args) > 1 else "")
            if _skipped_build:
                if str(reason) == BUILD_BUY_REASON:
                    _skipped_build = False
                    return
        except Exception as e:
            network._log("menus pop hook error (game unpauses normally): {}".format(e))

        return _orig_pop_speed(self, speed, *args, **kwargs)

    GameClock.push_speed = _patched_push_speed
    GameClock.pop_speed = _patched_pop_speed
    _installed = True
    network._log("menus: unpaused menus hook installed")
    return True


install()
