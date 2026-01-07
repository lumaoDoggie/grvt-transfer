import dataclasses
import json
import logging
import time
import random
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict

from pysdk.grvt_raw_base import GrvtApiConfig, GrvtError
from pysdk.grvt_raw_env import GrvtEnv
from pysdk.grvt_raw_sync import GrvtRawSync
from pysdk import grvt_raw_types as rt
from eth_account import Account as EthAccount
from eth_account.messages import encode_typed_data

from repository import ClientFactory, ConfigRepository

# EIP-712 constants for GRVT order signing (matching official pysdk)
PRICE_MULTIPLIER = Decimal("1000000000")  # 1e9 - prices are 9 decimal precision

EIP712_ORDER_MESSAGE_TYPE: Dict[str, Any] = {
    "Order": [
        {"name": "subAccountID", "type": "uint64"},
        {"name": "isMarket", "type": "bool"},
        {"name": "timeInForce", "type": "uint8"},
        {"name": "postOnly", "type": "bool"},
        {"name": "reduceOnly", "type": "bool"},
        {"name": "legs", "type": "OrderLeg[]"},
        {"name": "nonce", "type": "uint32"},
        {"name": "expiration", "type": "int64"},
    ],
    "OrderLeg": [
        {"name": "assetID", "type": "uint256"},
        {"name": "contractSize", "type": "uint64"},
        {"name": "limitPrice", "type": "uint64"},
        {"name": "isBuyingContract", "type": "bool"},
    ],
}

TIME_IN_FORCE_TO_SIGN_CODE = {
    "GOOD_TILL_TIME": 1,
    "ALL_OR_NONE": 2,
    "IMMEDIATE_OR_CANCEL": 3,
    "FILL_OR_KILL": 4,
}



class PositionService:
    """Fetches and processes position data from GRVT API."""

    @staticmethod
    def get_positions(cfg: dict, client: GrvtRawSync | None = None) -> list[dict]:
        """Fetch all positions for a trading subaccount."""
        client = client or ClientFactory.trading_client(cfg)
        sub_id = str(cfg.get("trading_account_id"))
        for attempt in range(3):
            try:
                req = rt.ApiPositionsRequest(sub_account_id=sub_id, kind=[rt.Kind.PERPETUAL])
                res = client.positions_v1(req)
                if isinstance(res, GrvtError):
                    logging.getLogger("errors").info(json.dumps({"error": "positions_fetch", "detail": dataclasses.asdict(res)}, default=str))
                    if attempt < 2:
                        time.sleep(1)
                        continue
                    return []
                return [dataclasses.asdict(p) for p in (res.result or [])]
            except Exception as e:
                logging.getLogger("errors").info(json.dumps({"error": "positions_exception", "exception": str(e)}, default=str))
                if attempt < 2:
                    time.sleep(1)
                    continue
                return []
        return []

    @staticmethod
    def total_notional(positions: list[dict]) -> Decimal:
        """Sum of absolute notional values across all positions."""
        total = Decimal("0")
        for p in positions:
            try:
                total += abs(Decimal(str(p.get("notional", "0"))))
            except Exception:
                pass
        return total

    @staticmethod
    def prioritize_by_pnl_ratio(positions: list[dict]) -> list[dict]:
        """Sort positions by abs(unrealized_pnl) / notional descending."""
        def score(p: dict) -> Decimal:
            try:
                notional = abs(Decimal(str(p.get("notional", "0"))))
                pnl = abs(Decimal(str(p.get("unrealized_pnl", "0"))))
                if notional == Decimal("0"):
                    return Decimal("0")
                return pnl / notional
            except Exception:
                return Decimal("0")
        return sorted(positions, key=score, reverse=True)


class UnwindService:
    """Emergency position unwinding to prevent liquidation."""

    def __init__(self, cfg_repo: ConfigRepository, logger: logging.Logger):
        self.cfg_repo = cfg_repo
        self.logger = logger

    def should_trigger(self, equity: Decimal, maint_margin: Decimal, trigger_multiplier: Decimal) -> bool:
        """Check if equity is below trigger threshold (equity < trigger_multiplier × maint_margin).
        
        Liquidation occurs at equity = 1× maint_margin.
        We trigger unwind at equity < trigger_multiplier × maint_margin (e.g., 2.0×).
        """
        if maint_margin <= Decimal("0"):
            return False  # No margin = no positions = no trigger
        margin_ratio = equity / maint_margin
        return margin_ratio < trigger_multiplier

    def is_recovered(self, equity: Decimal, maint_margin: Decimal, recovery_multiplier: Decimal) -> bool:
        """Check if equity is above recovery threshold (equity > recovery_multiplier × maint_margin).
        
        Stop unwinding when equity > recovery_multiplier × maint_margin (e.g., 2.5×).
        """
        if maint_margin <= Decimal("0"):
            return True  # No margin = recovered
        margin_ratio = equity / maint_margin
        return margin_ratio > recovery_multiplier

    def build_reduce_order(
        self,
        cfg: dict,
        position: dict,
        reduce_pct: Decimal,
    ) -> dict | None:
        """Build a reduce-only market order to unwind a percentage of a position.
        
        Uses custom EIP-712 signing instead of SDK's sign_order for proper market order support.
        Returns a dict payload ready for REST API.
        """
        try:
            sub_id = str(cfg.get("trading_account_id"))
            instrument_name = str(position.get("instrument", ""))
            size = abs(Decimal(str(position.get("size", "0"))))
            reduce_size_raw = size * (reduce_pct / Decimal("100"))
            if reduce_size_raw <= Decimal("0"):
                return None

            # Determine direction: if currently long (size > 0), we sell to reduce
            current_size = Decimal(str(position.get("size", "0")))
            is_buying = current_size < Decimal("0")  # Short position → buy to reduce

            # Get environment-aware config
            from repository import _get_env
            env = _get_env()
            grvt_env = GrvtEnv.TESTNET if env == "test" else GrvtEnv.PROD
            chain_id = 326 if env == "test" else 325  # Testnet: 326, Prod: 325

            # Build API config
            api_config = GrvtApiConfig(
                env=grvt_env,
                trading_account_id=sub_id,
                private_key=str(cfg.get("tradingAccountSecret")),
                api_key=str(cfg.get("tradingAccountKey", cfg.get("tradingAcccountKey", cfg.get("fundingAccountKey", "")))),
                logger=None,
            )
            account = EthAccount.from_key(str(cfg.get("tradingAccountSecret")))

            # Fetch instrument metadata from API (needed for instrument_hash and base_decimals)
            client = GrvtRawSync(api_config)
            inst_res = client.get_instrument_v1(rt.ApiGetInstrumentRequest(instrument=instrument_name))
            if isinstance(inst_res, GrvtError) or not inst_res.result:
                self.logger.info(json.dumps({"error": "fetch_instrument", "instrument": instrument_name}, default=str))
                return None
            
            inst = dataclasses.asdict(inst_res.result)
            base_decimals = int(inst.get("base_decimals", 0))
            size_multiplier = Decimal(10) ** Decimal(base_decimals)
            # Round size to tick_size (minimum order step), not base_decimals
            # tick_size=0.01 means order size must be multiples of 0.01
            tick_size = Decimal(str(inst.get("tick_size", "0.01")))
            if tick_size <= Decimal("0"):
                tick_size = Decimal("0.01")  # Default fallback
            # Round down to nearest tick_size
            reduce_size = (reduce_size_raw / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size
            if reduce_size <= Decimal("0"):
                self.logger.info(json.dumps({"error": "reduce_size_too_small", "raw": str(reduce_size_raw), "tick_size": str(tick_size)}, default=str))
                return None
            instrument_hash = inst.get("instrument_hash")
            
            # Parse instrument_hash to int (handles hex strings)
            if instrument_hash is None:
                self.logger.info(json.dumps({"error": "missing_instrument_hash", "instrument": instrument_name}, default=str))
                return None
            if isinstance(instrument_hash, str) and instrument_hash.startswith("0x"):
                asset_id = int(instrument_hash, 16)
            else:
                asset_id = int(instrument_hash)

            # Generate nonce and expiration
            nonce = random.randint(0, 2**32 - 1)
            expiration_ns = int(time.time_ns() + 15 * 60 * 1_000_000_000)  # 15 minutes

            # Build EIP-712 typed data for signing
            # Market orders: limitPrice = 0
            contract_size = int((reduce_size * size_multiplier).to_integral_value(rounding=ROUND_DOWN))
            limit_price_int = 0  # Market order

            typed_legs = [{
                "assetID": asset_id,
                "contractSize": contract_size,
                "limitPrice": limit_price_int,
                "isBuyingContract": is_buying,
            }]

            message_data = {
                "subAccountID": int(sub_id),
                "isMarket": True,
                "timeInForce": TIME_IN_FORCE_TO_SIGN_CODE["IMMEDIATE_OR_CANCEL"],
                "postOnly": False,
                "reduceOnly": True,
                "legs": typed_legs,
                "nonce": nonce,
                "expiration": expiration_ns,
            }

            domain_data = {"name": "GRVT Exchange", "version": "0", "chainId": chain_id}
            typed_msg = encode_typed_data(domain_data, EIP712_ORDER_MESSAGE_TYPE, message_data)
            signed = account.sign_message(typed_msg)

            # Build the order payload for REST API
            # GRVT requires client_order_id in range [2^63, 2^64-1]
            client_order_id = str(random.randint(2**63, 2**64 - 1))
            order_payload = {
                "sub_account_id": sub_id,
                "is_market": True,
                "time_in_force": "IMMEDIATE_OR_CANCEL",
                "post_only": False,
                "reduce_only": True,
                "legs": [{
                    "instrument": instrument_name,
                    "size": str(reduce_size),
                    "limit_price": None,  # Market order
                    "is_buying_asset": is_buying,
                }],
                "signature": {
                    "signer": str(account.address),
                    "r": "0x" + hex(signed.r)[2:].zfill(64),
                    "s": "0x" + hex(signed.s)[2:].zfill(64),
                    "v": int(signed.v),
                    "expiration": str(expiration_ns),
                    "nonce": nonce,
                },
                "metadata": {"client_order_id": client_order_id},
            }

            return order_payload
        except Exception as e:
            self.logger.info(json.dumps({"error": "build_reduce_order", "exception": str(e)}, default=str))
            return None

    def execute_unwind(
        self,
        cfg: dict,
        position: dict,
        reduce_pct: Decimal,
        dry_run: bool = True,
    ) -> dict:
        """Execute a single unwind order (or log in dry-run mode)."""
        import requests
        
        instrument = position.get("instrument", "unknown")
        size = position.get("size", "0")
        notional = position.get("notional", "0")
        pnl = position.get("unrealized_pnl", "0")
        reduce_size = abs(Decimal(str(size))) * (reduce_pct / Decimal("100"))

        log_entry = {
            "action": "unwind",
            "dry_run": dry_run,
            "instrument": instrument,
            "current_size": size,
            "reduce_pct": str(reduce_pct),
            "reduce_size": str(reduce_size),
            "notional": notional,
            "unrealized_pnl": pnl,
        }

        if dry_run:
            self.logger.info(json.dumps({"DRY_RUN_UNWIND": log_entry}, default=str))
            return {"success": True, "dry_run": True, "detail": log_entry}

        order_payload = self.build_reduce_order(cfg, position, reduce_pct)
        if not order_payload:
            return {"success": False, "error": "failed_to_build_order", "detail": log_entry}

        # Use direct HTTP POST to GRVT trading API
        try:
            from repository import _get_env
            env = _get_env()
            base_url = "https://trades.testnet.grvt.io" if env == "test" else "https://trades.grvt.io"
            url = f"{base_url}/full/v1/create_order"
            
            # Get session cookie from SDK client
            client = ClientFactory.trading_client(cfg)
            
            # The SDK client stores the gravity cookie in _cookie after login
            # Force a cookie refresh by making an authenticated call first
            try:
                # This triggers cookie retrieval if needed - use correct method name
                _ = client.sub_account_summary_v1(rt.ApiSubAccountSummaryRequest(sub_account_id=str(cfg.get("trading_account_id"))))
            except Exception as auth_err:
                self.logger.info(json.dumps({"warning": "auth_call_failed", "exception": str(auth_err)}, default=str))
            
            # Get the gravity cookie value - _cookie is a GrvtCookie object
            cookie_obj = getattr(client, '_cookie', None)
            if not cookie_obj:
                self.logger.info(json.dumps({"error": "no_gravity_cookie", "cookie_obj": str(cookie_obj)}, default=str))
                return {"success": False, "error": "no_gravity_cookie", "detail": log_entry}
            if not hasattr(cookie_obj, 'gravity'):
                self.logger.info(json.dumps({"error": "no_gravity_attribute", "cookie_obj": str(type(cookie_obj))}, default=str))
                return {"success": False, "error": "no_gravity_attribute", "detail": log_entry}
            gravity_cookie = cookie_obj.gravity
            
            headers = {
                "Content-Type": "application/json",
                "X-Grvt-Account-Id": str(cfg.get("trading_account_id")),
                "Cookie": f"gravity={gravity_cookie}",
            }
            
            payload = {"order": order_payload}
            self.logger.info(json.dumps({"unwind_request": payload}, default=str))
            
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response_data = response.json()
            
            if response.status_code != 200:
                self.logger.info(json.dumps({"error": "unwind_order_failed", "status": response.status_code, "response": response_data, "request": order_payload}, default=str))
                return {"success": False, "error": response_data, "detail": log_entry}
            
            self.logger.info(json.dumps({"unwind_order_placed": response_data}, default=str))
            return {"success": True, "result": response_data, "detail": log_entry}
        except Exception as e:
            self.logger.info(json.dumps({"error": "unwind_order_exception", "exception": str(e)}, default=str))
            return {"success": False, "error": str(e), "detail": log_entry}

    def check_and_unwind(
        self,
        cfg1: dict,
        cfg2: dict,
        eq1: Decimal,
        mm1: Decimal,
        eq2: Decimal,
        mm2: Decimal,
        dry_run: bool = True,
    ) -> dict:
        """Main entry point: check both accounts and unwind if needed.
        
        Trigger: equity < trigger_multiplier × maintenance_margin
        Recovery: equity > recovery_multiplier × maintenance_margin
        """
        base_cfg = self.cfg_repo.base()
        unwind_cfg = base_cfg.get("unwind", {})

        if not unwind_cfg.get("enabled", False):
            return {"action": "disabled"}

        # Support both old and new config key names for backwards compatibility
        trigger_mult = Decimal(str(unwind_cfg.get("triggerMultiplier", unwind_cfg.get("triggerPct", 2.0))))
        recovery_mult = Decimal(str(unwind_cfg.get("recoveryMultiplier", unwind_cfg.get("recoveryPct", 2.5))))
        unwind_pct = Decimal(str(unwind_cfg.get("unwindPct", 10.0)))
        max_iterations = int(unwind_cfg.get("maxIterations", 5))
        wait_seconds = int(unwind_cfg.get("waitSecondsBetweenIterations", 5))
        min_notional = Decimal(str(unwind_cfg.get("minPositionNotional", 100)))

        # Calculate margin ratios for logging
        ratio1 = (eq1 / mm1) if mm1 > Decimal("0") else Decimal("999")
        ratio2 = (eq2 / mm2) if mm2 > Decimal("0") else Decimal("999")

        # Check if either account triggers
        trigger1 = self.should_trigger(eq1, mm1, trigger_mult)
        trigger2 = self.should_trigger(eq2, mm2, trigger_mult)

        if not trigger1 and not trigger2:
            return {
                "action": "no_trigger",
                "eq1": str(eq1), "mm1": str(mm1), "ratio1": f"{ratio1:.2f}",
                "eq2": str(eq2), "mm2": str(mm2), "ratio2": f"{ratio2:.2f}",
                "trigger_at": str(trigger_mult),
            }

        self.logger.info(json.dumps({
            "unwind_triggered": True,
            "trigger1": trigger1,
            "trigger2": trigger2,
            "eq1": str(eq1), "mm1": str(mm1), "ratio1": f"{ratio1:.2f}",
            "eq2": str(eq2), "mm2": str(mm2), "ratio2": f"{ratio2:.2f}",
            "trigger_at": str(trigger_mult),
            "recover_at": str(recovery_mult),
        }, default=str))

        # Send alert
        try:
            from alerts.services import AlertService
            AlertService.dispatch_unwind_event({
                "triggered": True,
                "eq1": str(eq1),
                "mm1": str(mm1),
                "ratio1": f"{ratio1:.2f}",
                "eq2": str(eq2),
                "mm2": str(mm2),
                "ratio2": f"{ratio2:.2f}",
                "trigger_at": str(trigger_mult),
                "trigger1": trigger1,
                "trigger2": trigger2,
                "dry_run": dry_run,
            })
        except Exception:
            pass

        # Fetch initial positions
        positions1 = PositionService.get_positions(cfg1)
        positions2 = PositionService.get_positions(cfg2)

        results = []
        for iteration in range(max_iterations):
            # Refresh equity and margin
            from rebalance.services import SummaryService
            eq1, mm1, _, _ = SummaryService.trading_summary(cfg1)
            eq2, mm2, _, _ = SummaryService.trading_summary(cfg2)
            positions1 = PositionService.get_positions(cfg1)
            positions2 = PositionService.get_positions(cfg2)

            # Check recovery using new margin-based logic
            recovered1 = self.is_recovered(eq1, mm1, recovery_mult)
            recovered2 = self.is_recovered(eq2, mm2, recovery_mult)
            if recovered1 and recovered2:
                ratio1 = (eq1 / mm1) if mm1 > Decimal("0") else Decimal("999")
                ratio2 = (eq2 / mm2) if mm2 > Decimal("0") else Decimal("999")
                self.logger.info(json.dumps({
                    "unwind_recovered": True,
                    "iteration": iteration,
                    "ratio1": f"{ratio1:.2f}",
                    "ratio2": f"{ratio2:.2f}",
                }, default=str))
                # Send recovery alert
                try:
                    from alerts.services import AlertService
                    AlertService.dispatch_unwind_recovery({
                        "ratio1": f"{ratio1:.2f}",
                        "ratio2": f"{ratio2:.2f}",
                        "recovery_at": str(recovery_mult),
                        "iteration": iteration,
                    })
                except Exception:
                    pass
                break

            # Prioritize positions
            priority1 = PositionService.prioritize_by_pnl_ratio(positions1)
            priority2 = PositionService.prioritize_by_pnl_ratio(positions2)

            # Unwind top position from each account
            for cfg, priority, label in [(cfg1, priority1, "A"), (cfg2, priority2, "B")]:
                for pos in priority:
                    pos_notional = abs(Decimal(str(pos.get("notional", "0"))))
                    if pos_notional < min_notional:
                        continue
                    result = self.execute_unwind(cfg, pos, unwind_pct, dry_run=dry_run)
                    results.append({"account": label, "iteration": iteration, **result})
                    # Alert on order failure (not dry run)
                    if not dry_run and not result.get("success"):
                        try:
                            from alerts.services import AlertService
                            AlertService.dispatch_unwind_order({
                                "success": False,
                                "account": label,
                                "instrument": pos.get("instrument", "?"),
                                "error": result.get("error", "unknown"),
                            })
                        except Exception:
                            pass
                    break  # Only unwind one position per account per iteration

            if iteration < max_iterations - 1:
                time.sleep(wait_seconds)

        # Get final margin ratios
        from rebalance.services import SummaryService
        final_eq1, final_mm1, _, _ = SummaryService.trading_summary(cfg1)
        final_eq2, final_mm2, _, _ = SummaryService.trading_summary(cfg2)
        final_ratio1 = (final_eq1 / final_mm1) if final_mm1 > Decimal("0") else Decimal("999")
        final_ratio2 = (final_eq2 / final_mm2) if final_mm2 > Decimal("0") else Decimal("999")
        
        successful = sum(1 for r in results if r.get("success"))
        failed = len(results) - successful
        
        # Build per-account breakdown of closed positions
        account_a_orders = [r for r in results if r.get("account") == "A" and r.get("success")]
        account_b_orders = [r for r in results if r.get("account") == "B" and r.get("success")]
        
        def format_orders(orders):
            """Format orders as list of {instrument, size, notional}"""
            formatted = []
            for o in orders:
                detail = o.get("detail", {})
                formatted.append({
                    "instrument": detail.get("instrument", "?"),
                    "size": detail.get("reduce_size", "?"),
                    "notional": detail.get("notional", "?"),
                })
            return formatted
        
        summary = {
            "action": "unwind_completed",
            "iterations": len(results),
            "successful": successful,
            "failed": failed,
            "dry_run": dry_run,
            "final_ratio1": f"{final_ratio1:.2f}",
            "final_ratio2": f"{final_ratio2:.2f}",
            "account_a": format_orders(account_a_orders),
            "account_b": format_orders(account_b_orders),
            "results": results,
        }
        self.logger.info(json.dumps(summary, default=str))

        try:
            from alerts.services import AlertService
            AlertService.dispatch_unwind_event(summary)
        except Exception:
            pass

        return summary
