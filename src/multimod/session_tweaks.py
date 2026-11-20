# decompyle3 version 3.9.3
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.9 (tags/v3.7.9:13c94747c7, Aug 17 2020, 18:58:18) [MSC v.1900 64 bit (AMD64)]
# Embedded file name: C:\simlink\mod\src\multimod\session_tweaks.py
# Compiled at: 2026-10-11 22:32:01
# Size of source mod 2**32: 3155 bytes

"""S4OM session tweaks - small game behaviours that break multiplayer.

  - Joiners don't autosave during a session. Their game saving in the middle of a host's
    travel used to drop them (services/autosave_service.AutosaveService._request_autosave,
    gameplay_options/misc_options_service.MiscOptionsService.autosave_on_travel).
  - After travel, each player gets their own Sim selected again (the game picks the
    household's first Sim on arrival). server/client.Client.set_active_sim_by_id.
"""

import time

from multimod import network

_installed = False

_my_sim_id = None

_last_zone = None

_restore_until = 0.0


def joiner_in_session():
    return not network._is_host and network._client_socket is not None


def install():
    global _installed
    if _installed:
        return True
    try:
        from services.autosave_service import AutosaveService
        from gameplay_options.misc_options_service import MiscOptionsService
    except Exception:
        return False
    if getattr(AutosaveService, "_s4om_patched", False):
        _installed = True
        return True
    AutosaveService._s4om_patched = True
    orig_request = AutosaveService._request_autosave

    def _request_autosave(self, *args, **kwargs):
        try:
            if joiner_in_session():
                network._log("autosave skipped (joiner in a session)")
                return
        except Exception:
            pass

        return orig_request(self, *args, **kwargs)

    AutosaveService._request_autosave = _request_autosave
    orig_prop = MiscOptionsService.autosave_on_travel

    def _autosave_on_travel(self):
        try:
            if joiner_in_session():
                return False
        except Exception:
            pass

        return orig_prop.fget(self)

    MiscOptionsService.autosave_on_travel = property(_autosave_on_travel)
    _installed = True
    network._log("session tweaks installed (no joiner autosave)")
    return True


def tick(now=None):
    """Remember our Sim; after a zone change, select it again."""
    global _last_zone
    global _my_sim_id
    global _restore_until
    now = now or time.time()
    try:
        import services

        zone = services.current_zone_id()
        client = services.client_manager().get_first_client()
    except Exception:
        return
    if client is None or zone is None:
        return
    if zone != _last_zone:
        if _last_zone is not None and _my_sim_id:
            _restore_until = now + 30
        _last_zone = zone
    active = getattr(client, "active_sim_info", None)
    if now < _restore_until:
        if _my_sim_id:
            if active is not None and int(active.id) == int(_my_sim_id):
                _restore_until = 0.0
            else:
                try:
                    if client.set_active_sim_by_id(int(_my_sim_id)):
                        network._log("travel: selected our own Sim again ({})".format(_my_sim_id))
                        _restore_until = 0.0
                except Exception:
                    pass

            return
    if active is not None:
        _my_sim_id = int(active.id)


install()
