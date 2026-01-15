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
                send_rebalance(event)
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
                f"⚠️ 可用余额不足 [{account_label}]\n"
                f"时间: {payload.get('event_time_sh')}\n"
                f"权益: {payload.get('equity')}\n"
                f"可用: {payload.get('available')} ({payload.get('avail_pct')}%)"
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
                # Unwind triggered - show margin percentages and which accounts triggered
                pct1 = event.get('pct1', '?')
                pct2 = event.get('pct2', '?')
                trigger_at = event.get('trigger_at', '?')
                trigger1 = "⚠️" if event.get('trigger1') else "✅"
                trigger2 = "⚠️" if event.get('trigger2') else "✅"

                text = (
                    f"🚨 {dry_run_tag}触发紧急平仓\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"{trigger1} 账户A: {pct1} 保证金使用率\n"
                    f"{trigger2} 账户B: {pct2} 保证金使用率\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"触发条件: ≥{trigger_at} 保证金使用率"
                )
            else:
                # Unwind completed
                iterations = event.get("iterations", 0)
                successful = event.get("successful", 0)
                failed = event.get("failed", 0)
                final_pct1 = event.get("final_pct1", "?")
                final_pct2 = event.get("final_pct2", "?")
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

                def format_tokens(tokens):
                    if not tokens:
                        return "  (无)"
                    lines = []
                    for t, v in tokens.items():
                        lines.append(f"  {t}: {v['size']:.2f} (${v['notional']:,.0f})")
                    return "\n".join(lines)

                status = "✅" if failed == 0 else "⚠️"
                text = (
                    f"{status} {dry_run_tag}紧急平仓完成\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"订单: {successful}✓ {failed}✗\n"
                    f"\n"
                    f"账户A:\n{format_tokens(a_tokens)}\n"
                    f"账户B:\n{format_tokens(b_tokens)}\n"
                    f"\n"
                    f"最终保证金使用率:\n"
                    f"  A: {final_pct1} | B: {final_pct2}"
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
            pct1 = event.get('pct1', '?')
            pct2 = event.get('pct2', '?')
            recovery_at = event.get('recovery_at', '?')
            iteration = event.get('iteration', '?')
            text = (
                f"✅ 保证金已恢复\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"账户A: {pct1} 保证金使用率\n"
                f"账户B: {pct2} 保证金使用率\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"恢复条件: <{recovery_at} 经过 {iteration} 轮"
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
                size = event.get('size')
                error = str(event.get('error', 'unknown'))[:80]
                size_line = f"\nsize={str(size)[:32]}" if size else ""
                text = f"❌ 紧急平仓失败: {account} {instrument}{size_line}\n{error}"
                send_message(text)
            except Exception:
                pass
