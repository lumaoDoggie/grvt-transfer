import json
import logging
import os
import time


class AlertService:
    @staticmethod
    def dispatch_rebalance_event(event: dict):
        logger = logging.getLogger("alerts")
        logger.info(json.dumps({"rebalance_event": event}, default=str))
        try:
            from bot.telegram_bot import send_rebalance
            if ("transfer_usdt" in event) or ("success" in event):
                st = AlertService._read_state()
                cnt = int(st.get("rebalance_alert_counter", 0))
                cnt += 1
                if (cnt % 5) == 0:
                    send_rebalance(event)
                AlertService._save_state({"rebalance_alert_counter": cnt})
        except Exception:
            pass

    @staticmethod
    def dispatch_warning(error: dict):
        logger = logging.getLogger("alerts")
        logger.info(json.dumps({"warning": error}, default=str))
        try:
            from bot.telegram_bot import send_warning
            send_warning(error)
        except Exception:
            pass

    @staticmethod
    def _state_path():
        return os.path.join("alerts", "state.json")

    @staticmethod
    def _read_state():
        try:
            p = AlertService._state_path()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f) or {}
        except Exception:
            pass
        return {}

    @staticmethod
    def _save_state(data: dict):
        try:
            os.makedirs("alerts", exist_ok=True)
            cur = AlertService._read_state()
            cur.update(data or {})
            with open(AlertService._state_path(), "w", encoding="utf-8") as f:
                json.dump(cur, f)
        except Exception:
            pass

    @staticmethod
    def dispatch_availability_alert(account_label: str, payload: dict, suppress_seconds: int = 120):
        logger = logging.getLogger("alerts")
        try:
            now_ts = time.time()
            key = f"avail_alert_last_ts_{account_label}"
            st = AlertService._read_state()
            last = float(st.get(key, 0))
            if (now_ts - last) < suppress_seconds:
                return False
            from bot.telegram_bot import send_message
            text = (
                f"⚠️ Low Collateral [{account_label}]\n"
                f"Time: {payload.get('event_time_sh')}\n"
                f"Equity: {payload.get('equity')}\n"
                f"Available: {payload.get('available')} ({payload.get('avail_pct')}%)"
            )
            ok, _ = send_message(text)
            if ok:
                AlertService._save_state({key: now_ts})
            logger.info(json.dumps({"availability_alert": payload, "sent": ok}, default=str))
            return ok
        except Exception:
            return False

    @staticmethod
    def dispatch_unwind_event(event: dict):
        """Critical alert for position unwinding - always sent immediately."""
        logger = logging.getLogger("alerts")
        logger.info(json.dumps({"unwind_event": event}, default=str))
        try:
            from bot.telegram_bot import send_message
            dry_run_tag = "[DRY RUN] " if event.get("dry_run") else ""
            
            if event.get("triggered"):
                # Unwind triggered - show margin ratios and which accounts triggered
                ratio1 = event.get('ratio1', '?')
                ratio2 = event.get('ratio2', '?')
                trigger_at = event.get('trigger_at', '?')
                trigger1 = "⚠️" if event.get('trigger1') else "✅"
                trigger2 = "⚠️" if event.get('trigger2') else "✅"
                
                text = (
                    f"🚨 {dry_run_tag}UNWIND TRIGGERED\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"{trigger1} Account A: {ratio1}× margin\n"
                    f"{trigger2} Account B: {ratio2}× margin\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Trigger at: <{trigger_at}× margin"
                )
            else:
                # Unwind completed
                iterations = event.get("iterations", 0)
                successful = event.get("successful", 0)
                failed = event.get("failed", 0)
                final_ratio1 = event.get("final_ratio1", "?")
                final_ratio2 = event.get("final_ratio2", "?")
                account_a = event.get("account_a", [])
                account_b = event.get("account_b", [])
                
                def sum_account(orders):
                    """Calculate total size and notional per token for an account"""
                    by_token = {}
                    for o in orders:
                        try:
                            inst = str(o.get("instrument", "?")).replace("_USDT_Perp", "")
                            size = abs(float(o.get("size", 0)))
                            notional = abs(float(o.get("notional", 0)))
                            if inst not in by_token:
                                by_token[inst] = {"size": 0, "notional": 0}
                            by_token[inst]["size"] += size
                            by_token[inst]["notional"] += notional
                        except:
                            pass
                    return by_token
                
                a_tokens = sum_account(account_a)
                b_tokens = sum_account(account_b)
                
                def format_tokens(tokens, label):
                    if not tokens:
                        return f"{label}: none"
                    parts = [f"{t} {v['size']:.2f} (${v['notional']:,.0f})" for t, v in tokens.items()]
                    return f"{label}: " + ", ".join(parts)
                
                status = "✅" if failed == 0 else "⚠️"
                text = (
                    f"{status} {dry_run_tag}UNWIND COMPLETED\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Orders: {successful}✓ {failed}✗\n"
                    f"{format_tokens(a_tokens, 'A')}\n"
                    f"{format_tokens(b_tokens, 'B')}\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"A: {final_ratio1}× | B: {final_ratio2}×"
                )
            send_message(text)
        except Exception:
            pass

    @staticmethod
    def dispatch_unwind_recovery(event: dict):
        """Alert when margin recovers and unwind is no longer needed."""
        logger = logging.getLogger("alerts")
        logger.info(json.dumps({"unwind_recovery": event}, default=str))
        try:
            from bot.telegram_bot import send_message
            ratio1 = event.get('ratio1', '?')
            ratio2 = event.get('ratio2', '?')
            recovery_at = event.get('recovery_at', '?')
            iteration = event.get('iteration', '?')
            text = (
                f"✅ MARGIN RECOVERED\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Account A: {ratio1}× margin\n"
                f"Account B: {ratio2}× margin\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Recovery: >{recovery_at}× after {iteration} iter"
            )
            send_message(text)
        except Exception:
            pass

    @staticmethod
    def dispatch_unwind_order(event: dict):
        """Alert for individual unwind order failures (successes logged only)."""
        logger = logging.getLogger("alerts")
        logger.info(json.dumps({"unwind_order": event}, default=str))
        if not event.get("success"):
            try:
                from bot.telegram_bot import send_message
                account = event.get('account', '?')
                instrument = event.get('instrument', '?')
                error = str(event.get('error', 'unknown'))[:80]
                text = f"❌ UNWIND FAILED: {account} {instrument}\n{error}"
                send_message(text)
            except Exception:
                pass
