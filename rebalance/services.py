import dataclasses
import json
import logging
from decimal import Decimal
import time
from pysdk.grvt_raw_base import GrvtError
from pysdk.grvt_raw_sync import types
from repository import ClientFactory, ConfigRepository
from utils import TimeUtil, FundingUtil, TxUtil
import state


class SummaryService:
    @staticmethod
    def trading_summary(cfg: dict, client=None):
        client = client or ClientFactory.trading_client(cfg)
        from pysdk import grvt_raw_types as rt
        sub_id = str(cfg.get("trading_account_id"))
        last_error = None
        for i in range(4):  # 4 retries with exponential backoff
            try:
                res = client.sub_account_summary_v1(rt.ApiSubAccountSummaryRequest(sub_account_id=sub_id))
                obj = dataclasses.asdict(res)["result"] if not isinstance(res, GrvtError) else {}
                eq = Decimal(obj.get("total_equity", "0"))
                mm = Decimal(obj.get("maintenance_margin", obj.get("maint_margin", "0")))
                avail = Decimal(obj.get("available_balance", "0"))
                return eq, mm, avail, obj
            except Exception as e:
                last_error = e
                try:
                    logging.getLogger("errors").info(json.dumps({"error": "trading_summary", "sub_account_id": sub_id, "attempt": i + 1, "exception": str(e)}, default=str))
                except Exception:
                    pass
                if i < 3:
                    time.sleep(2 ** i)  # Exponential backoff: 1s, 2s, 4s
        # Only alert after all retries exhausted
        if last_error:
            try:
                from alerts.services import AlertService
                AlertService.dispatch_warning({"trading_summary_error": str(last_error), "sub_account_id": sub_id, "retries_exhausted": True})
            except Exception:
                pass
        return Decimal("0"), Decimal("0"), Decimal("0"), {}

    @staticmethod
    def funding_summary(cfg: dict, client=None):
        client = client or ClientFactory.funding_client(cfg)
        last_error = None
        for i in range(4):  # 4 retries with exponential backoff
            try:
                res = client.funding_account_summary_v1(types.EmptyRequest())
                return dataclasses.asdict(res)
            except Exception as e:
                last_error = e
                try:
                    logging.getLogger("errors").info(json.dumps({"error": "funding_summary", "account": str(cfg.get("account_id")), "attempt": i + 1, "exception": str(e)}, default=str))
                except Exception:
                    pass
                if i < 3:
                    time.sleep(2 ** i)  # Exponential backoff: 1s, 2s, 4s
        # Only alert after all retries exhausted
        if last_error:
            try:
                from alerts.services import AlertService
                AlertService.dispatch_warning({"funding_summary_error": str(last_error), "account": str(cfg.get("account_id")), "retries_exhausted": True})
            except Exception:
                pass
        return {"result": {"spot_balances": []}}

    @staticmethod
    def funding_usdt_balance(cfg: dict, client=None):
        client = client or ClientFactory.funding_client(cfg)
        last_error = None
        for i in range(4):  # 4 retries with exponential backoff
            try:
                res = client.funding_account_summary_v1(types.EmptyRequest())
                obj = dataclasses.asdict(res)
                currency = str(cfg.get("currency", "USDT"))
                bal = Decimal("0")
                for b in obj.get("result", {}).get("spot_balances", []) or []:
                    if str(b.get("currency")) == currency:
                        try:
                            bal = Decimal(str(b.get("balance", "0")))
                        except Exception:
                            bal = Decimal("0")
                        break
                return bal, obj
            except Exception as e:
                last_error = e
                try:
                    logging.getLogger("errors").info(json.dumps({"error": "funding_balance", "account": str(cfg.get("account_id")), "attempt": i + 1, "exception": str(e)}, default=str))
                except Exception:
                    pass
                if i < 3:
                    time.sleep(2 ** i)  # Exponential backoff: 1s, 2s, 4s
        # Only alert after all retries exhausted
        if last_error:
            try:
                from alerts.services import AlertService
                AlertService.dispatch_warning({"funding_balance_error": str(last_error), "account": str(cfg.get("account_id")), "retries_exhausted": True})
            except Exception:
                pass
        return Decimal("0"), {"result": {"spot_balances": []}}


class TransferService:
    @staticmethod
    def build_req(api_config,
                  account,
                  from_addr: str,
                  from_sub: str,
                  to_addr: str,
                  to_sub: str,
                  currency: str,
                  amount: str,
                  chain_id: int,
                  t_type=None):
        import time
        import random
        from pysdk import grvt_fixed_types as ft
        from pysdk import grvt_raw_types as rt
        from pysdk import grvt_raw_signing as sign
        expiration_ns = str(int(time.time_ns() + 15 * 60 * 1_000_000_000))
        nonce = random.randint(1, 2**31 - 1)
        t_type = t_type or ft.TransferType.STANDARD
        t = ft.Transfer(
            from_account_id=str(from_addr),
            from_sub_account_id=str(from_sub),
            to_account_id=str(to_addr),
            to_sub_account_id=str(to_sub),
            currency=str(currency),
            num_tokens=str(amount),
            signature=rt.Signature(
                signer="",
                r="0x",
                s="0x",
                v=0,
                expiration=expiration_ns,
                nonce=nonce,
            ),
            transfer_type=t_type,
            transfer_metadata="",
        )
        signed_t = sign.sign_transfer(
            transfer=t,
            config=api_config,
            account=account,
            chainId=int(chain_id),
            currencyId=3,
        )
        from pysdk import grvt_raw_types as rt
        return rt.ApiTransferRequest(
            from_account_id=signed_t.from_account_id,
            from_sub_account_id=signed_t.from_sub_account_id,
            to_account_id=signed_t.to_account_id,
            to_sub_account_id=signed_t.to_sub_account_id,
            currency=signed_t.currency,
            num_tokens=signed_t.num_tokens,
            signature=signed_t.signature,
            transfer_type=rt.TransferType.STANDARD,
            transfer_metadata=signed_t.transfer_metadata,
        )

    @staticmethod
    def try_transfer(client, req, retries: int = 2, backoff_ms: int = 1500):
        import time
        attempt = 0
        while True:
            from pysdk.grvt_raw_base import GrvtError
            try:
                res = client.transfer_v1(req)
            except Exception as e:
                if attempt < retries:
                    time.sleep(backoff_ms / 1000.0)
                    attempt += 1
                    backoff_ms = int(backoff_ms * 1.5)
                    continue
                try:
                    logging.getLogger("errors").info(json.dumps({"error": "transfer_exception", "exception": str(e)}, default=str))
                except Exception:
                    pass
                return False, {"exception": str(e)}
            if isinstance(res, GrvtError):
                d = dataclasses.asdict(res)
                code = d.get("code")
                status = d.get("status")
                if attempt < retries and (code == 1006 or status == 429):
                    time.sleep(backoff_ms / 1000.0)
                    attempt += 1
                    backoff_ms = int(backoff_ms * 1.5)
                    continue
                try:
                    logging.getLogger("errors").info(json.dumps({"error": "transfer_business_error", "detail": d}, default=str))
                except Exception:
                    pass
                return False, d
            return True, dataclasses.asdict(res)


class RebalanceService:
    def __init__(self, cfg_repo: ConfigRepository, logger: logging.Logger, noop_logger: logging.Logger):
        self.cfg_repo = cfg_repo
        self.logger = logger
        self.noop_logger = noop_logger

    def rebalance_once(self, trigger: Decimal, throttle_ms: int = 0):
        from flow import TransferFlow, BalanceSweeper
        cfg1, cfg2 = self.cfg_repo.accounts()

        base_cfg = self.cfg_repo.base()
        sweep_threshold = Decimal(str(base_cfg.get("fundingSweepThreshold", "0.1")))

        BalanceSweeper.sweep(cfg1, sweep_threshold, throttle_ms=throttle_ms, logger=self.logger)
        BalanceSweeper.sweep(cfg2, sweep_threshold, throttle_ms=throttle_ms, logger=self.logger)

        client1 = ClientFactory.trading_client(cfg1)
        client2 = ClientFactory.trading_client(cfg2)
        eq1, mm1, avail1, t1 = SummaryService.trading_summary(cfg1, client1)
        eq2, mm2, avail2, t2 = SummaryService.trading_summary(cfg2, client2)
        state.set_last_check_time(TimeUtil.event_time_sh(t1))
        f_client1 = ClientFactory.funding_client(cfg1)
        f_client2 = ClientFactory.funding_client(cfg2)
        f1 = SummaryService.funding_summary(cfg1, f_client1)
        f2 = SummaryService.funding_summary(cfg2, f_client2)

        pct1 = (avail1 / eq1 * Decimal("100")) if eq1 > Decimal("0") else Decimal("0")
        pct2 = (avail2 / eq2 * Decimal("100")) if eq2 > Decimal("0") else Decimal("0")
        alert_pct = Decimal(str(base_cfg.get("minAvailableBalanceAlertPercentage", 20)))
        try:
            from alerts.services import AlertService
            # Skip alert if equity is 0 (likely API error)
            if pct1 < alert_pct and eq1 > Decimal("0"):
                AlertService.dispatch_availability_alert("A", {
                    "event_time_sh": TimeUtil.event_time_sh(t1),
                    "equity": str(eq1),
                    "available": str(avail1),
                    "avail_pct": f"{pct1:.4f}",
                })
            if pct2 < alert_pct and eq2 > Decimal("0"):
                AlertService.dispatch_availability_alert("B", {
                    "event_time_sh": TimeUtil.event_time_sh(t2),
                    "equity": str(eq2),
                    "available": str(avail2),
                    "avail_pct": f"{pct2:.4f}",
                })
        except Exception:
            pass

        # Emergency position unwinding check
        unwind_cfg = base_cfg.get("unwind", {})
        if unwind_cfg.get("enabled", False):
            try:
                from unwind.services import UnwindService
                dry_run = unwind_cfg.get("dryRun", True)
                unwind_svc = UnwindService(self.cfg_repo, self.logger)
                unwind_result = unwind_svc.check_and_unwind(cfg1, cfg2, eq1, mm1, eq2, mm2, dry_run=dry_run)
                if unwind_result.get("action") not in ("disabled", "no_trigger"):
                    self.logger.info(json.dumps({"unwind_result": unwind_result}, default=str))
                    # Refresh balances after unwinding
                    eq1, mm1, avail1, t1 = SummaryService.trading_summary(cfg1, client1)
                    eq2, mm2, avail2, t2 = SummaryService.trading_summary(cfg2, client2)
            except Exception as e:
                self.logger.info(json.dumps({"error": "unwind_check_failed", "exception": str(e)}, default=str))


        if (eq1 == Decimal("0") or eq2 == Decimal("0")):
            # Retry once after 3 seconds before alerting (might be temporary API issue)
            import time as _time
            _time.sleep(3)
            try:
                eq1_retry, _, _, _ = SummaryService.trading_summary(cfg1, client1)
                eq2_retry, _, _, _ = SummaryService.trading_summary(cfg2, client2)
            except Exception:
                eq1_retry, eq2_retry = eq1, eq2  # Keep original on error
            
            if (eq1_retry == Decimal("0") or eq2_retry == Decimal("0")):
                # Still zero after retry - log it
                try:
                    logging.getLogger("errors").info(json.dumps({"error": "rebalance_skip_zero_equity", "eq1": str(eq1_retry), "eq2": str(eq2_retry)}, default=str))
                except Exception:
                    pass
                # Only alert if ONE account is zero (real concern), not both (likely API failure)
                if not (eq1_retry == Decimal("0") and eq2_retry == Decimal("0")):
                    try:
                        from alerts.services import AlertService
                        AlertService.dispatch_warning({"rebalance_skipped": "zero_equity_detected", "eq1": str(eq1_retry), "eq2": str(eq2_retry)})
                    except Exception:
                        pass
                return {"action": "blocked_zero_equity", "eq1": str(eq1_retry), "eq2": str(eq2_retry), "mm1": str(mm1), "mm2": str(mm2)}
            else:
                # Recovered after retry - use new values
                eq1, eq2 = eq1_retry, eq2_retry
        delta = eq1 - eq2
        if abs(delta) <= trigger:
            one_line = {
                "event_time_sh": TimeUtil.event_time_sh(t1),
                "action": "noop",
                "trigger": str(trigger),
                "delta": str(delta),
                "eq1": str(eq1),
                "eq2": str(eq2),
                "mm1": str(mm1),
                "mm2": str(mm2),
                "totalEquity": str(eq1 + eq2),
                "avail1": str(avail1),
                "avail2": str(avail2),
                "avail_pct1": f"{pct1:.4f}",
                "avail_pct2": f"{pct2:.4f}",
            }
            self.noop_logger.info(json.dumps(one_line, default=str))
            try:
                from alerts.services import AlertService
                AlertService.dispatch_rebalance_event(one_line)
            except Exception:
                pass
            return {"action": "noop", "eq1": str(eq1), "eq2": str(eq2), "mm1": str(mm1), "mm2": str(mm2)}

        if delta > 0:
            src_cfg, dst_cfg = cfg1, cfg2
            src_eq, src_mm, src_avail = eq1, mm1, avail1
        else:
            src_cfg, dst_cfg = cfg2, cfg1
            src_eq, src_mm, src_avail = eq2, mm2, avail2

        needed = abs(delta) / Decimal("2")
        max_by_avail = src_avail
        max_by_mm = src_eq - (src_mm * Decimal("2"))
        if max_by_mm <= Decimal("0"):
            return {"action": "blocked_mm", "eq1": str(eq1), "eq2": str(eq2), "mm1": str(mm1), "mm2": str(mm2)}
        transfer_amt = min(needed, max_by_avail, max_by_mm)
        if transfer_amt <= Decimal("0"):
            return {"action": "blocked_avail", "eq1": str(eq1), "eq2": str(eq2), "mm1": str(mm1), "mm2": str(mm2)}

        start_time_sh = TimeUtil.event_time_sh(t1)

        ok, info = TransferFlow.execute(src_cfg, dst_cfg, transfer_amt, throttle_ms=throttle_ms, logger=self.logger)
        if not ok:
            try:
                from alerts.services import AlertService
                AlertService.dispatch_warning(info)
            except Exception:
                pass

        eq1_post, mm1_post, avail1_post, t1_post = SummaryService.trading_summary(cfg1, client1)
        eq2_post, mm2_post, avail2_post, t2_post = SummaryService.trading_summary(cfg2, client2)
        pct1_post = (avail1_post / eq1_post * Decimal("100")) if eq1_post > Decimal("0") else Decimal("0")
        pct2_post = (avail2_post / eq2_post * Decimal("100")) if eq2_post > Decimal("0") else Decimal("0")
        f1_post = SummaryService.funding_summary(cfg1, f_client1)
        f2_post = SummaryService.funding_summary(cfg2, f_client2)
        success_all = TxUtil.success(info, "internal_tx") and TxUtil.success(info, "funding_to_funding_tx") and TxUtil.success(info, "deposit_tx")
        one_line = {
            "event_time_sh": start_time_sh,
            "success": success_all,
            "transfer_usdt": str(transfer_amt),
            "totalEquity": str(eq1_post + eq2_post),
            "trading_a": {"equity": t1_post.get("total_equity"), "mm": t1_post.get("maintenance_margin"), "available": str(avail1_post), "avail_pct": f"{pct1_post:.4f}"},
            "trading_b": {"equity": t2_post.get("total_equity"), "mm": t2_post.get("maintenance_margin"), "available": str(avail2_post), "avail_pct": f"{pct2_post:.4f}"},
            "funding_a_pre": FundingUtil.funding_usdt_from_summary(f1),
            "funding_b_pre": FundingUtil.funding_usdt_from_summary(f2),
            "funding_a_post": FundingUtil.funding_usdt_from_summary(f1_post),
            "funding_b_post": FundingUtil.funding_usdt_from_summary(f2_post),
            "tx_ids": {
                "internal": TxUtil.tx_id(info, "internal_tx"),
                "funding_to_funding": TxUtil.tx_id(info, "funding_to_funding_tx"),
                "deposit": TxUtil.tx_id(info, "deposit_tx"),
            },
        }
        self.logger.info(json.dumps(one_line, default=str))
        try:
            from alerts.services import AlertService
            AlertService.dispatch_rebalance_event(one_line)
        except Exception:
            pass
        print(json.dumps(one_line, default=str))

        return {"action": "executed" if ok else "failed", "transfer": str(transfer_amt), "info": info, "eq1": str(eq1_post), "eq2": str(eq2_post)}
