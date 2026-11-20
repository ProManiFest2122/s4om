# -*- coding: utf-8 -*-

"""Sims4Multiplayer v8.2 - 金钱同步（家庭资金）

研究: sims.modify_funds cheat + household.funds API
功能: 房主家庭资金变化 → 广播 → 客机应用（同一存档同家庭，资金应一致）
命令:
  mp_moneysync      开始/停止金钱同步
  mp_money <金额>   广播资金变更（房主用）
"""

import time, sims4.commands

try:
    from multimod import network
except Exception:
    network = None

SYNC_INTERVAL = 5.0  # 资金检查间隔（秒）

_last_funds = None

_running = False

_err = None

def _get_funds():
    """获取当前家庭资金（sim_info.household.funds 或 house_hold 资金）"""
    try:
        import services
        hh = services.active_household()
        if hh is not None:
            return int(hh.funds.money)
        return
    except Exception:
        return

def _broadcast_funds():
    """广播当前资金（变化时）"""
    global _err
    global _last_funds
    try:
        funds = _get_funds()
        if funds is None:
            return
        if _last_funds is not None and abs(funds - _last_funds) < 1:
            return
        _last_funds = funds
        payload = {'type':"money_sync",  'funds':funds,  'ts':(time.time)()}
        if network is None:
            return
        if network._is_host:
            network._broadcast(payload)
        else:
            network._send_json(network._client_socket, payload)
        network._log("money sync: sent {}".format(funds))
    except Exception as e:
        if network is not None:
            if str(e) != _err:
                _err = str(e)
                network._log("money sync send error: {!r}".format(e))

_REASON = 0

def money_delta(have, want, tol=1):
    """Pure (tested): how much to add (+) or take (-) so we show the same funds."""
    d = int(want) - int(have)
    if abs(d) >= tol:
        return d
    return 0

def process_message(data):
    """处理金钱同步消息（客机应用资金）"""
    global _last_funds
    try:
        if data.get("type") != "money_sync":
            return
        funds = data.get("funds")
        if funds is None:
            return
        import services
        household = services.active_household()
        if household is None:
            return
        funds_obj = getattr(household, "funds", None)
        if funds_obj is not None:
            delta = money_delta(int(funds_obj.money), int(funds))
            if delta:
                _last_funds = int(funds)
                if delta > 0:
                    funds_obj.add(delta, _REASON)
                else:
                    funds_obj.try_remove((-delta), _REASON, require_full_amount=False)
                network._log("money sync: {:+d} -> {}".format(delta, funds))
    except Exception as e:
        if network:
            network._log("money sync error: {}".format(e))

def _money_loop():
    """资金检查循环"""
    global _running
    while _running:
        try:
            _broadcast_funds()
        except Exception:
            pass
        time.sleep(SYNC_INTERVAL)

@sims4.commands.Command('mp_moneysync', command_type=sims4.commands.CommandType.Live)
def mp_moneysync(_connection=None):
    """开始/停止金钱同步"""
    global _running, _last_funds
    if _running:
        _running = False
        if network:
            network._notify('Money sync stopped')
        return
    _running = True
    _last_funds = None
    import threading
    threading.Thread(target=_money_loop, daemon=True).start()
    if network:
        network._notify('Money sync started (checks every {}s)'.format(SYNC_INTERVAL))
        network._log("money sync started")

@sims4.commands.Command('mp_money', command_type=sims4.commands.CommandType.Live)
def mp_money(funds=None, _connection=None):
    """手动广播资金变更（房主用）"""
    global _last_funds
    try:
        _last_funds = None  # 强制广播
        _broadcast_funds()
        if network:
            network._notify('Sent current household funds')
    except Exception as e:
        if network:
            network._log("mp_money error: {}".format(e))

def start():
    """fork: started with position sync - money cheats/purchases on a joiner's PC used to
    never reach the host, and desync repair then put the old amount back."""
    global _last_funds
    global _running
    if _running:
        return
    _running = True
    _last_funds = None
    import threading
    threading.Thread(target=_money_loop, daemon=True).start()

def collect_snapshot():
    """返回当前家庭资金（快照用，None=获取失败）"""
    try:
        import services
        hh = services.active_household()
        if hh is None:
            return
        return int(hh.funds.money)
    except Exception:
        return
