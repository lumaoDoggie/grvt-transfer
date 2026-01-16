import os
import json
import time
import atexit
import threading
import yaml
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import logging
import time

from envutil import load_env as _load_env
from decimal import Decimal

_load_env()


def _load_yaml(path: str):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


def _get_env_config_path():
    """Get config path based on GRVT_ENV environment variable."""
    env = os.getenv("GRVT_ENV", "prod").lower()
    env_config = os.path.join("config", env, "config.yaml")
    if os.path.exists(env_config):
        return env_config
    return "config.yaml"


def _config():
    base = _load_yaml("bot/config.yaml")
    local = _load_yaml("bot/config.local.yaml")
    # Load from environment-specific config (respects GRVT_ENV)
    root = _load_yaml(_get_env_config_path())
    out = {}
    out.update(base or {})
    out.update(local or {})
    out.update((root or {}).get("bot", {}) if isinstance(root, dict) else {})
    if not out.get("noop_log_path"):
        out["noop_log_path"] = "logs/rebalance_noop.log"
    return out


def _token():
    env = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if env:
        return env
    cfg = _config()
    t = cfg.get("token") or cfg.get("bot_token") or cfg.get("telegramBotToken")
    env_config_path = _get_env_config_path()
    if not t and os.path.exists(env_config_path):
        try:
            root = _load_yaml(env_config_path) or {}
            t = root.get("telegramBotToken") or (root.get("bot") or {}).get("token")
        except Exception:
            t = None
    return str(t or "")


def _state_path():
    state_dir = os.getenv("GRVT_STATE_DIR", "").strip() or "bot"
    return os.path.join(state_dir, "state.json")

def _runtime_state_path():
    state_dir = os.getenv("GRVT_STATE_DIR", "").strip() or "bot"
    return os.path.join(state_dir, "runtime.json")


def _read_runtime_state(max_age_sec: int = 6 * 60 * 60) -> dict:
    """Best-effort: read runtime settings written by the runner (GUI/CLI)."""
    try:
        p = _runtime_state_path()
        if not os.path.exists(p):
            return {}
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        if not isinstance(d, dict):
            return {}
        # Ignore stale data.
        ts = d.get("ts")
        if ts and (time.time() - float(ts)) > float(max_age_sec):
            return {}
        # Only apply when env matches (prod/test).
        env = str(os.getenv("GRVT_ENV", "prod")).lower()
        if str(d.get("env", env)).lower() != env:
            return {}
        return d
    except Exception:
        return {}


def _get_chat_id():
    env_cid = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if env_cid:
        return env_cid
    cfg = _config()
    cid = cfg.get("chat_id")
    if cid:
        return str(cid)
    try:
        p = _state_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f) or {}
            cid = d.get("chat_id")
            if cid:
                return str(cid)
    except Exception:
        pass
    return None


def _save_chat_id(chat_id: str):
    allowed_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if allowed_chat_id and str(chat_id) != str(allowed_chat_id):
        return
    try:
        os.makedirs(os.path.dirname(_state_path()) or ".", exist_ok=True)
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump({"chat_id": str(chat_id)}, f)
    except Exception:
        pass


def _read_state():
    try:
        p = _state_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


def _save_state(data: dict):
    try:
        os.makedirs(os.path.dirname(_state_path()) or ".", exist_ok=True)
        cur = _read_state()
        cur.update(data or {})
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(cur, f)
    except Exception:
        pass


def _heartbeat_stale(max_age: int = 30):
    try:
        s = _read_state()
        ts = s.get("heartbeat_ts")
        if not ts:
            return True
        return (time.time() - float(ts)) > max_age
    except Exception:
        return True


def _post_json(url: str, obj: dict):
    data = json.dumps(obj).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    for i in range(3):
        try:
            with urlopen(req, timeout=30) as resp:
                s = resp.read().decode("utf-8")
                return json.loads(s)
        except Exception as e:
            try:
                logging.getLogger("errors").info(json.dumps({"error": "telegram_post_json", "exception": str(e)}, default=str))
            except Exception:
                pass
            if i < 2:
                try:
                    time.sleep(1)
                except Exception:
                    pass
                continue
            raise


def send_message(text: str, reply_markup: dict | None = None, chat_id: str | int | None = None):
    token = _token()
    resolved_chat_id = str(chat_id) if chat_id is not None else _get_chat_id()
    if not token or not resolved_chat_id:
        return False, {"error": "missing_token_or_chat_id"}
    if not str(text or "").strip():
        return False, {"error": "empty_message_text"}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": resolved_chat_id, "text": str(text)}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = _post_json(url, payload)
        return True, res
    except Exception as e:
        try:
            logging.getLogger("errors").info(json.dumps({"error": "telegram_send_message", "exception": str(e)}, default=str))
        except Exception:
            pass
        return False, {"error": str(e)}


def send_rebalance(event: dict):
    t = str(event.get("event_time_sh") or event.get("time") or "")
    s = "成功" if event.get("success") else "失败"
    amt = str(event.get("transfer_usdt"))
    te = str(event.get("totalEquity"))
    aeq = str((event.get("trading_a") or {}).get("equity"))
    beq = str((event.get("trading_b") or {}).get("equity"))
    text = f"💰 再平衡已触发\n时间: {t}\n状态: {s}\n转账金额: ${amt}\n总余额: ${te}\n账户A余额: ${aeq}\n账户B余额: ${beq}"
    kb = {"inline_keyboard": [[{"text": "查看状态", "callback_data": "view_noop"}]]}
    return send_message(text, reply_markup=kb)


def _menu_keyboard():
    # Persistent menu so users don't need to type commands like /view.
    return {"keyboard": [[{"text": "查看"}]], "resize_keyboard": True}


def send_warning(error):
    text = f"⚠️ 警告: API调用失败\n错误: {error}"
    return send_message(text)


def _get_updates(offset: int | None = None, timeout: int = 25):
    token = _token()
    if not token:
        return []
    qs = {"timeout": timeout}
    if offset is not None:
        qs["offset"] = offset
    url = f"https://api.telegram.org/bot{token}/getUpdates?{urlencode(qs)}"
    try:
        with urlopen(url, timeout=timeout + 10) as resp:
            s = resp.read().decode("utf-8")
            data = json.loads(s)
            return data.get("result", [])
    except Exception as e:
        try:
            logging.getLogger("errors").info(json.dumps({"error": "telegram_get_updates", "exception": str(e)}, default=str))
        except Exception:
            pass
        return []


def _answer_callback_query(cid: str, text: str | None = None):
    token = _token()
    if not token:
        return False, {"error": "missing_token"}
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload = {"callback_query_id": str(cid)}
    if text:
        payload["text"] = str(text)
    try:
        res = _post_json(url, payload)
        return True, res
    except Exception as e:
        try:
            logging.getLogger("errors").info(json.dumps({"error": "telegram_answer_callback", "exception": str(e)}, default=str))
        except Exception:
            pass
        return False, {"error": str(e)}


def _delete_webhook(drop_pending_updates: bool = False):
    token = _token()
    if not token:
        return False, {"error": "missing_token"}
    url = f"https://api.telegram.org/bot{token}/deleteWebhook"
    payload = {"drop_pending_updates": bool(drop_pending_updates)}
    try:
        res = _post_json(url, payload)
        return True, res
    except Exception as e:
        try:
            logging.getLogger("errors").info(json.dumps({"error": "telegram_delete_webhook", "exception": str(e)}, default=str))
        except Exception:
            pass
        return False, {"error": str(e)}


def _last_noop_line():
    p = _config().get("noop_log_path", "logs/rebalance_noop.log")
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            if lines:
                return lines[-1].strip()
    except Exception:
        pass
    return ""


def _get_margin_status():
    """Fetch live margin percentages and status for both accounts."""
    def _format_status(
        now_str: str,
        trigger: float,
        trigger_pct: float,
        recovery_pct: float,
        show_unwind_thresholds: bool,
        eq_a: Decimal,
        mm_a: Decimal,
        avail_a: Decimal,
        eq_b: Decimal,
        mm_b: Decimal,
        avail_b: Decimal,
    ) -> str:
        def calc_pct(eq, mm):
            if eq <= 0:
                return "N/A"
            if mm <= 0:
                return "0.0%"
            pct = (mm / eq) * Decimal("100")
            return f"{pct:.1f}%"

        def avail_pct(eq, avail):
            if eq <= 0:
                return "N/A"
            return f"{(avail / eq) * Decimal('100'):.1f}%"

        pct_a = calc_pct(eq_a, mm_a)
        pct_b = calc_pct(eq_b, mm_b)
        delta = eq_a - eq_b
        total_eq = eq_a + eq_b

        text = (
            f"📊 上次检查时间 @ {now_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"触发转账阈值: ${trigger:,.0f} | 账户差额: ${delta:,.0f}\n"
            f"总余额: ${total_eq:,.0f}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"账户A: {pct_a} 保证金使用率\n"
            f"  余额=${eq_a:,.0f} | 可用金额={avail_pct(eq_a, avail_a)}\n"
            f"账户B: {pct_b} 保证金使用率\n"
            f"  余额=${eq_b:,.0f} | 可用金额={avail_pct(eq_b, avail_b)}"
        )
        if show_unwind_thresholds:
            text += (
                f"\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"紧急减仓触发: {trigger_pct:.0f}% | 紧急减仓停止: <{recovery_pct:.0f}%"
            )
        return text

    # Prefer in-process snapshots from the running loop (rebalance/unwind).
    try:
        import state as _state
        snap = _state.get_last_status()
        prog = _state.get_unwind_progress()
        if isinstance(snap, dict) and snap:
            now_str = _state.get_last_check_time() or str(snap.get("event_time_sh") or "")
            if not str(now_str or "").strip():
                now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

            # trigger stored as string in service logs
            try:
                trigger = float(str(snap.get("trigger", "0")).strip() or 0)
            except Exception:
                trigger = 0.0

            def _d(v) -> Decimal:
                try:
                    return Decimal(str(v))
                except Exception:
                    return Decimal("0")

            eq_a = _d(snap.get("eq1"))
            eq_b = _d(snap.get("eq2"))
            mm_a = _d(snap.get("mm1"))
            mm_b = _d(snap.get("mm2"))
            avail_a = _d(snap.get("avail1"))
            avail_b = _d(snap.get("avail2"))

            # Unwind thresholds: prefer live progress fields; fall back to runtime/config defaults.
            try:
                trigger_pct = float(str(prog.get("trigger_pct", "0")).replace("%", "").strip() or 0)
            except Exception:
                trigger_pct = 0.0
            try:
                recovery_pct = float(str(prog.get("recovery_pct", "0")).replace("%", "").strip() or 0)
            except Exception:
                recovery_pct = 0.0

            show_unwind_thresholds = False
            if trigger_pct or recovery_pct:
                show_unwind_thresholds = True
            else:
                # Prefer the runtime state written by GUI/CLI runner (matches current running settings).
                try:
                    rs = _read_runtime_state()
                    uw = (rs.get("unwind") or {}) if isinstance(rs, dict) else {}
                    if isinstance(uw, dict) and bool(uw.get("enabled", False)):
                        trigger_pct = float(uw.get("triggerPct") or 0)
                        recovery_pct = float(uw.get("recoveryPct") or 0)
                        show_unwind_thresholds = bool(trigger_pct or recovery_pct)
                except Exception:
                    pass

                # Finally fall back to env YAML config.
                if not show_unwind_thresholds:
                    try:
                        root_cfg = _load_yaml(_get_env_config_path()) or {}
                        uw = (root_cfg.get("unwind") or {}) if isinstance(root_cfg, dict) else {}
                        if isinstance(uw, dict) and bool(uw.get("enabled", False)):
                            trigger_pct = float(uw.get("triggerPct") or 0)
                            recovery_pct = float(uw.get("recoveryPct") or 0)
                            show_unwind_thresholds = bool(trigger_pct or recovery_pct)
                    except Exception:
                        pass

            text = _format_status(
                now_str=str(now_str),
                trigger=trigger,
                trigger_pct=(trigger_pct or 0.0),
                recovery_pct=(recovery_pct or 0.0),
                show_unwind_thresholds=show_unwind_thresholds,
                eq_a=eq_a,
                mm_a=mm_a,
                avail_a=avail_a,
                eq_b=eq_b,
                mm_b=mm_b,
                avail_b=avail_b,
            )

            if bool(prog.get("in_progress")):
                it = int(prog.get("iteration", 0) or 0)
                p1 = str(prog.get("pct_a") or "")
                p2 = str(prog.get("pct_b") or "")
                # Inject unwind banner after the first line.
                lines = text.splitlines()
                banner = f"🛠 正在紧急减仓中（第 {it} 轮） A保证金使用率={p1} | B保证金使用率={p2}"
                if lines:
                    lines.insert(1, banner)
                    text = "\n".join(lines)
            return text
    except Exception:
        pass

    last_error = None
    for attempt in range(3):
        try:
            from repository import ConfigRepository, ClientFactory
            from rebalance.services import SummaryService
            from datetime import datetime

            repo = ConfigRepository()
            base_cfg = repo.base()
            runtime = _read_runtime_state()
            if runtime:
                # Keep account configs from repository; only override display thresholds.
                if "triggerValue" in runtime and runtime.get("triggerValue") is not None:
                    base_cfg["triggerValue"] = runtime.get("triggerValue")
                ru = runtime.get("unwind")
                if isinstance(ru, dict):
                    if "unwind" not in base_cfg or not isinstance(base_cfg.get("unwind"), dict):
                        base_cfg["unwind"] = {}
                    for k in ("enabled", "triggerPct", "recoveryPct"):
                        if k in ru and ru.get(k) is not None:
                            base_cfg["unwind"][k] = ru.get(k)
            trigger = base_cfg.get("triggerValue", 2000)
            unwind_cfg = base_cfg.get("unwind", {})
            trigger_pct = unwind_cfg.get("triggerPct", 60)
            recovery_pct = unwind_cfg.get("recoveryPct", 40)
            show_unwind_thresholds = bool(unwind_cfg.get("enabled", False))

            # Prefer last known snapshot from the running loop (no API calls needed).
            last_event = (runtime or {}).get("last_event") if isinstance(runtime, dict) else None
            if isinstance(last_event, dict):
                try:
                    import state
                    last_check = state.get_last_check_time()
                    now_str = last_check if last_check else str(last_event.get("event_time_sh") or "")
                    if not now_str.strip():
                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    def _d(v):
                        try:
                            return Decimal(str(v))
                        except Exception:
                            return Decimal("0")

                    if "trading_a" in last_event and "trading_b" in last_event:
                        ta = last_event.get("trading_a") or {}
                        tb = last_event.get("trading_b") or {}
                        eq_a = _d(ta.get("equity"))
                        eq_b = _d(tb.get("equity"))
                        mm_a = _d(ta.get("mm"))
                        mm_b = _d(tb.get("mm"))
                        avail_a = _d(ta.get("available"))
                        avail_b = _d(tb.get("available"))
                    else:
                        eq_a = _d(last_event.get("eq1"))
                        eq_b = _d(last_event.get("eq2"))
                        mm_a = _d(last_event.get("mm1"))
                        mm_b = _d(last_event.get("mm2"))
                        avail_a = _d(last_event.get("avail1"))
                        avail_b = _d(last_event.get("avail2"))

                    return _format_status(
                        now_str=now_str,
                        trigger=float(trigger),
                        trigger_pct=float(trigger_pct),
                        recovery_pct=float(recovery_pct),
                        show_unwind_thresholds=show_unwind_thresholds,
                        eq_a=eq_a,
                        mm_a=mm_a,
                        avail_a=avail_a,
                        eq_b=eq_b,
                        mm_b=mm_b,
                        avail_b=avail_b,
                    )
                except Exception:
                    pass

            # Next best: parse last noop line (still no API calls).
            try:
                ln = _last_noop_line()
                if ln:
                    evt = json.loads(ln)
                    if isinstance(evt, dict) and "eq1" in evt and "eq2" in evt:
                        import state
                        last_check = state.get_last_check_time()
                        now_str = last_check if last_check else str(evt.get("event_time_sh") or "")
                        if not now_str.strip():
                            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        eq_a = Decimal(str(evt.get("eq1", "0")))
                        eq_b = Decimal(str(evt.get("eq2", "0")))
                        mm_a = Decimal(str(evt.get("mm1", "0")))
                        mm_b = Decimal(str(evt.get("mm2", "0")))
                        avail_a = Decimal(str(evt.get("avail1", "0")))
                        avail_b = Decimal(str(evt.get("avail2", "0")))
                        return _format_status(
                            now_str=now_str,
                            trigger=float(trigger),
                            trigger_pct=float(trigger_pct),
                            recovery_pct=float(recovery_pct),
                            show_unwind_thresholds=show_unwind_thresholds,
                            eq_a=eq_a,
                            mm_a=mm_a,
                            avail_a=avail_a,
                            eq_b=eq_b,
                            mm_b=mm_b,
                            avail_b=avail_b,
                        )
            except Exception:
                pass

            cfg_a, cfg_b = repo.accounts()
            client_a = ClientFactory.trading_client(cfg_a)
            client_b = ClientFactory.trading_client(cfg_b)

            eq_a, mm_a, avail_a, _ = SummaryService.trading_summary(cfg_a, client_a)
            eq_b, mm_b, avail_b, _ = SummaryService.trading_summary(cfg_b, client_b)

            if eq_a == 0 and eq_b == 0:
                if attempt < 2:
                    time.sleep(1)
                    continue
                return "API returned zero equity - try again"

            import state
            last_check = state.get_last_check_time()
            now_str = last_check if last_check else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return _format_status(
                now_str=now_str,
                trigger=float(trigger),
                trigger_pct=float(trigger_pct),
                recovery_pct=float(recovery_pct),
                show_unwind_thresholds=show_unwind_thresholds,
                eq_a=eq_a,
                mm_a=mm_a,
                avail_a=avail_a,
                eq_b=eq_b,
                mm_b=mm_b,
                avail_b=avail_b,
            )
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(1)
    return f"Error fetching status: {str(last_error)[:100]}"


def start_polling():
    offset = None
    logger = logging.getLogger("alerts")
    try:
        logger.info(json.dumps({"bot_polling": "started"}))
    except Exception:
        pass
    try:
        _delete_webhook(drop_pending_updates=False)
    except Exception:
        pass
    while not _stop_event.is_set():
        try:
            updates = _get_updates(offset=offset)
        except Exception as e:
            try:
                logging.getLogger("errors").info(json.dumps({"error": "polling_get_updates", "exception": str(e)}, default=str))
            except Exception:
                pass
            time.sleep(5)  # backoff on error
            continue
        for u in updates:
            try:
                uid = int(u.get("update_id", 0))
                offset = (uid + 1) if uid else offset
            except Exception:
                pass
            m = u.get("message")
            if m:
                cid = (m.get("chat") or {}).get("id")
                allowed_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
                if cid and allowed_chat_id and str(cid) != str(allowed_chat_id):
                    continue
                if cid:
                    _save_chat_id(cid)
                txt = str(m.get("text", ""))
                if txt.strip() == "/start":
                    send_message("ok", chat_id=cid, reply_markup=_menu_keyboard())
                elif txt.strip().lower() in ("/view", "view", "查看"):
                    status = _get_margin_status()
                    ok, _ = send_message(status, chat_id=cid, reply_markup=_menu_keyboard())
                    try:
                        logging.getLogger("alerts").info(json.dumps({"text_cmd": "view", "sent": ok}))
                    except Exception:
                        pass
            cq = u.get("callback_query")
            if cq:
                data = str(cq.get("data", ""))
                cid = ((cq.get("message") or {}).get("chat") or {}).get("id")
                allowed_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
                if cid and allowed_chat_id and str(cid) != str(allowed_chat_id):
                    continue
                if cid:
                    _save_chat_id(cid)
                if data == "view_noop":
                    status = _get_margin_status()
                    ok, res = send_message(status, chat_id=cid, reply_markup=_menu_keyboard())
                    try:
                        logging.getLogger("alerts").info(json.dumps({"callback": "view_noop", "sent": ok}))
                    except Exception:
                        pass
                    _answer_callback_query(str(cq.get("id")), text=("sent" if ok else "failed"))
        try:
            _save_state({"heartbeat_ts": time.time(), "chat_id": _get_chat_id()})
        except Exception:
            pass
        time.sleep(1)


_started = False
_lock_pid = None
_stop_event = threading.Event()
_polling_thread = None
_watchdog_thread = None
_watchdog_stop_event = threading.Event()


def _start_polling_thread():
    """Start the polling thread and return it."""
    global _polling_thread
    _stop_event.clear()
    t = threading.Thread(target=start_polling, daemon=True)
    t.start()
    _polling_thread = t
    return t


def _watchdog():
    """Watchdog that monitors the polling thread and restarts it if it crashes or becomes stale."""
    logger = logging.getLogger("alerts")
    stale_threshold = 60  # seconds without heartbeat update = stale
    check_interval = 30  # check every 30 seconds

    while not _watchdog_stop_event.is_set():
        try:
            # Allow stop_bot() to terminate the watchdog promptly.
            if _watchdog_stop_event.wait(check_interval):
                break

            # Check if polling thread is alive
            if _polling_thread is None or not _polling_thread.is_alive():
                try:
                    logger.info(json.dumps({"watchdog": "polling_thread_dead", "restarting": True}))
                except Exception:
                    pass
                _stop_event.clear()
                _start_polling_thread()
                continue

            # Check heartbeat staleness
            if _heartbeat_stale(stale_threshold):
                try:
                    logger.info(json.dumps({"watchdog": "heartbeat_stale", "restarting": True}))
                except Exception:
                    pass
                # Signal old thread to stop, wait, then start new one
                _stop_event.set()
                time.sleep(3)
                _stop_event.clear()
                _start_polling_thread()

        except Exception as e:
            try:
                logging.getLogger("errors").info(json.dumps({"error": "watchdog_error", "exception": str(e)}, default=str))
            except Exception:
                pass
            _watchdog_stop_event.wait(5)  # backoff on error


def _lock_path():
    state_dir = os.getenv("GRVT_STATE_DIR", "").strip() or "bot"
    return os.path.join(state_dir, ".botlock")


def _acquire_lock():
    global _lock_pid
    try:
        os.makedirs(os.path.dirname(_lock_path()) or ".", exist_ok=True)
        lp = _lock_path()
        fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            _lock_pid = os.getpid()
            os.write(fd, str(_lock_pid).encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def _release_lock():
    try:
        lp = _lock_path()
        if os.path.exists(lp):
            # best-effort: only remove if we own it
            try:
                with open(lp, "r", encoding="utf-8") as f:
                    s = (f.read() or "").strip()
                if s == str(_lock_pid):
                    os.remove(lp)
            except Exception:
                pass
    except Exception:
        pass


def start_bot_daemon():
    global _started
    global _watchdog_thread
    # Allow restarting within the same process after stop_bot().
    if _started and _stop_event.is_set():
        _started = False
    if _started:
        try:
            logging.getLogger("alerts").info(json.dumps({"bot_started": False, "reason": "already_started"}))
        except Exception:
            pass
        return {"started": False, "reason": "already_started", "chat_id": _get_chat_id()}
    lp_ok = _acquire_lock()
    if not lp_ok:
        if _heartbeat_stale(30):
            try:
                os.remove(_lock_path())
            except Exception:
                pass
            lp_ok = _acquire_lock()
            if not lp_ok:
                _started = True
                try:
                    logging.getLogger("alerts").info(json.dumps({"bot_started": False, "reason": "lock_exists"}))
                except Exception:
                    pass
                return {"started": False, "reason": "lock_exists", "chat_id": _get_chat_id()}
        _started = True
        try:
            logging.getLogger("alerts").info(json.dumps({"bot_started": False, "reason": "lock_exists"}))
        except Exception:
            pass
        return {"started": False, "reason": "lock_exists", "chat_id": _get_chat_id()}
    # Start the polling thread using the new helper
    _start_polling_thread()
    # Start watchdog thread to monitor and restart polling if needed
    _watchdog_stop_event.clear()
    watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
    watchdog_thread.start()
    _watchdog_thread = watchdog_thread
    _started = True
    try:
        logging.getLogger("alerts").info(json.dumps({"bot_started": True, "watchdog_enabled": True, "chat_id": _get_chat_id()}))
    except Exception:
        pass
    atexit.register(_release_lock)
    return {"started": True, "chat_id": _get_chat_id()}


def stop_bot():
    global _started
    global _polling_thread
    global _watchdog_thread
    try:
        _watchdog_stop_event.set()
        _stop_event.set()
        logging.getLogger("alerts").info(json.dumps({"bot_stopped": True}))
    except Exception:
        pass
    # Best-effort: allow start/stop cycles (GUI use case).
    try:
        if _polling_thread is not None and _polling_thread.is_alive():
            _polling_thread.join(timeout=5)
    except Exception:
        pass
    try:
        if _watchdog_thread is not None and _watchdog_thread.is_alive():
            _watchdog_thread.join(timeout=5)
    except Exception:
        pass
    _polling_thread = None
    _watchdog_thread = None
    _started = False
    _release_lock()
