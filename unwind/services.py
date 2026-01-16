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

    @staticmethod
    def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
        if step <= Decimal("0"):
            return value
        rounded = (value / step).to_integral_value(rounding=ROUND_DOWN) * step
        return rounded.quantize(step, rounding=ROUND_DOWN)

    @staticmethod
    def _decimal_to_str(value: Decimal) -> str:
        # Avoid scientific notation in JSON payloads (e.g. "1E+3")
        return format(value, "f")

    def should_trigger(self, equity: Decimal, maint_margin: Decimal, trigger_pct: Decimal) -> bool:
        """Check if margin usage is above trigger threshold.

        Margin usage = maintenance_margin / equity (as percentage).
        Liquidation occurs at 100% (equity = maintenance_margin).
        We trigger unwind when margin usage >= trigger_pct (e.g., 60%).

        Returns False if:
        - equity <= 0 (API error)
        - maint_margin <= 0 (no positions)
        - margin_pct >= 100 (likely erroneous data or already liquidated)
        """
        if equity <= Decimal("0"):
            return False  # Invalid data, don't trigger
        if maint_margin <= Decimal("0"):
            return False  # No margin = no positions = no trigger
        margin_pct = (maint_margin / equity) * Decimal("100")
        # Reject if margin >= 100% (erroneous data or liquidation already happened)
        if margin_pct >= Decimal("100"):
            return False
        return margin_pct >= trigger_pct

    def is_recovered(self, equity: Decimal, maint_margin: Decimal, recovery_pct: Decimal) -> bool:
        """Check if margin usage is below recovery threshold.

        Stop unwinding when margin usage < recovery_pct (e.g., 40%).
        Returns True if equity <= 0 (API error) to stop unwinding on bad data.
        """
        if equity <= Decimal("0"):
            return True  # Invalid data, stop unwinding
        if maint_margin <= Decimal("0"):
            return True  # No margin = recovered
        margin_pct = (maint_margin / equity) * Decimal("100")
        return margin_pct < recovery_pct

    def calc_margin_pct(self, equity: Decimal, maint_margin: Decimal) -> Decimal | None:
        """Calculate margin usage percentage. Returns None if invalid data."""
        if equity <= Decimal("0") or maint_margin < Decimal("0"):
            return None
        if maint_margin == Decimal("0"):
            return Decimal("0")  # No positions = 0% margin usage
        return (maint_margin / equity) * Decimal("100")

    def match_positions_by_instrument(self, positions1: list, positions2: list) -> tuple[dict, list]:
        """Match positions by instrument. Returns (matched, unmatched)."""
        p1_by_inst = {p["instrument"]: p for p in positions1}
        p2_by_inst = {p["instrument"]: p for p in positions2}
        matched = {}
        unmatched = []
        all_instruments = set(p1_by_inst.keys()) | set(p2_by_inst.keys())
        for inst in all_instruments:
            p1 = p1_by_inst.get(inst)
            p2 = p2_by_inst.get(inst)
            if p1 and p2:
                matched[inst] = (p1, p2)
            else:
                unmatched.append({"instrument": inst, "has_a": bool(p1), "has_b": bool(p2)})
        return matched, unmatched

    def calc_hedged_unwind_size(self, eq1: Decimal, mm1: Decimal, eq2: Decimal, mm2: Decimal,
                                 pos1_size: Decimal, pos2_size: Decimal, recovery_pct: Decimal) -> Decimal:
        """Calculate unwind size to reach recovery within ~5 iterations."""
        iterations = Decimal("5")
        pct1 = (mm1 / eq1) * Decimal("100") if eq1 > 0 else Decimal("0")
        pct2 = (mm2 / eq2) * Decimal("100") if eq2 > 0 else Decimal("0")
        max_pct = max(pct1, pct2)
        excess = max_pct - recovery_pct
        if excess <= Decimal("0"):
            return Decimal("0")
        # Reduction ratio per iteration
        reduction_ratio = excess / (max_pct * iterations)
        # Use min position size (hedged constraint)
        min_size = min(abs(pos1_size), abs(pos2_size))
        unwind_size = min_size * reduction_ratio
        return max(unwind_size, Decimal("0.01"))

    def calc_unwind_ratio(self, eq1: Decimal, mm1: Decimal, eq2: Decimal, mm2: Decimal, recovery_pct: Decimal, iterations: int) -> Decimal:
        """Compute a per-iteration unwind ratio based on margin stress.

        Ratio is applied proportionally to each position size (same % across instruments).
        """
        iters = Decimal(str(max(1, iterations)))
        pct1 = (mm1 / eq1) * Decimal("100") if eq1 > 0 else Decimal("0")
        pct2 = (mm2 / eq2) * Decimal("100") if eq2 > 0 else Decimal("0")
        max_pct = max(pct1, pct2)
        if max_pct <= Decimal("0"):
            return Decimal("0")
        excess = max_pct - recovery_pct
        if excess <= Decimal("0"):
            return Decimal("0")
        ratio = excess / (max_pct * iters)
        if ratio <= Decimal("0"):
            return Decimal("0")
        return min(ratio, Decimal("1"))

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
            # base_decimals is the smallest supported denomination, but venues may enforce coarser size increments.
            size_step = Decimal("1") / size_multiplier
            min_size = Decimal(str(inst.get("min_size", "0")))
            if min_size > Decimal("0"):
                # Treat min_size as the effective step when it's coarser than base_decimals granularity.
                size_step = max(size_step, min_size)
            else:
                min_size = size_step

            max_reduce_size = self._round_down_to_step(size, size_step)
            reduce_size = self._round_down_to_step(reduce_size_raw, size_step)
            if reduce_size > max_reduce_size:
                reduce_size = max_reduce_size
            if reduce_size < min_size:
                if max_reduce_size >= min_size:
                    reduce_size = self._round_down_to_step(min_size, size_step)
                else:
                    self.logger.info(json.dumps({"error": "position_too_small_to_close", "size": str(size), "min_size": str(min_size), "size_step": str(size_step)}, default=str))
                    return None
            if reduce_size <= Decimal("0"):
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
            if contract_size <= 0:
                self.logger.info(json.dumps({"error": "invalid_contract_size", "contract_size": contract_size, "reduce_size": str(reduce_size), "size_step": str(size_step), "min_size": str(min_size)}, default=str))
                return None
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
                    "size": self._decimal_to_str(reduce_size),
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
        try:
            log_entry["order_size"] = str(order_payload.get("legs", [{}])[0].get("size"))
        except Exception:
            pass

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

    def execute_unwind_fixed_size(
        self,
        cfg: dict,
        position: dict,
        fixed_size: Decimal,
        dry_run: bool = True,
    ) -> dict:
        """Execute unwind with fixed absolute size (for hedged unwinding)."""
        import requests
        instrument = position.get("instrument", "unknown")
        current_size = Decimal(str(position.get("size", "0")))
        notional = position.get("notional", "0")
        pnl = position.get("unrealized_pnl", "0")

        log_entry = {
            "action": "unwind_hedged",
            "dry_run": dry_run,
            "instrument": instrument,
            "current_size": str(current_size),
            "reduce_size": str(fixed_size),
            "notional": notional,
            "unrealized_pnl": pnl,
        }

        if dry_run:
            self.logger.info(json.dumps({"DRY_RUN_UNWIND": log_entry}, default=str))
            return {"success": True, "dry_run": True, "detail": log_entry}

        # Build order with fixed size instead of percentage
        order_payload = self._build_order_fixed_size(cfg, position, fixed_size)
        if not order_payload:
            return {"success": False, "error": "failed_to_build_order", "detail": log_entry}
        try:
            log_entry["order_size"] = str(order_payload.get("legs", [{}])[0].get("size"))
        except Exception:
            pass

        try:
            from repository import _get_env
            env = _get_env()
            base_url = "https://trades.testnet.grvt.io" if env == "test" else "https://trades.grvt.io"
            url = f"{base_url}/full/v1/create_order"
            client = ClientFactory.trading_client(cfg)
            try:
                _ = client.sub_account_summary_v1(rt.ApiSubAccountSummaryRequest(sub_account_id=str(cfg.get("trading_account_id"))))
            except Exception:
                pass
            cookie_obj = getattr(client, '_cookie', None)
            if not cookie_obj or not hasattr(cookie_obj, 'gravity'):
                return {"success": False, "error": "no_gravity_cookie", "detail": log_entry}
            headers = {
                "Content-Type": "application/json",
                "X-Grvt-Account-Id": str(cfg.get("trading_account_id")),
                "Cookie": f"gravity={cookie_obj.gravity}",
            }
            payload = {"order": order_payload}
            self.logger.info(json.dumps({"unwind_request": payload}, default=str))
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response_data = response.json()
            if response.status_code != 200:
                return {"success": False, "error": response_data, "detail": log_entry}
            self.logger.info(json.dumps({"unwind_order_placed": response_data}, default=str))
            return {"success": True, "result": response_data, "detail": log_entry}
        except Exception as e:
            return {"success": False, "error": str(e), "detail": log_entry}

    def _build_order_fixed_size(self, cfg: dict, position: dict, fixed_size: Decimal) -> dict | None:
        """Build reduce order with fixed size."""
        try:
            sub_id = str(cfg.get("trading_account_id"))
            instrument_name = str(position.get("instrument", ""))
            current_size = Decimal(str(position.get("size", "0")))
            is_buying = current_size < Decimal("0")

            from repository import _get_env
            env = _get_env()
            grvt_env = GrvtEnv.TESTNET if env == "test" else GrvtEnv.PROD
            chain_id = 326 if env == "test" else 325

            api_config = GrvtApiConfig(
                env=grvt_env,
                trading_account_id=sub_id,
                private_key=str(cfg.get("tradingAccountSecret")),
                api_key=str(cfg.get("tradingAccountKey", cfg.get("tradingAcccountKey", cfg.get("fundingAccountKey", "")))),
                logger=None,
            )
            account = EthAccount.from_key(str(cfg.get("tradingAccountSecret")))
            client = GrvtRawSync(api_config)
            inst_res = client.get_instrument_v1(rt.ApiGetInstrumentRequest(instrument=instrument_name))
            if isinstance(inst_res, GrvtError) or not inst_res.result:
                self.logger.info(json.dumps({"error": "build_order_fixed_size", "reason": "instrument_fetch_failed", "instrument": instrument_name}, default=str))
                return None
            inst = dataclasses.asdict(inst_res.result)
            base_decimals = int(inst.get("base_decimals", 0))
            size_multiplier = Decimal(10) ** Decimal(base_decimals)
            size_step = Decimal("1") / size_multiplier
            min_size = Decimal(str(inst.get("min_size", "0")))
            if min_size > Decimal("0"):
                size_step = max(size_step, min_size)
            else:
                min_size = size_step

            max_reduce_size = self._round_down_to_step(abs(current_size), size_step)
            reduce_size = self._round_down_to_step(fixed_size, size_step)
            if reduce_size > max_reduce_size:
                reduce_size = max_reduce_size
            if reduce_size < min_size:
                if max_reduce_size >= min_size:
                    reduce_size = self._round_down_to_step(min_size, size_step)
                else:
                    self.logger.info(json.dumps({"error": "build_order_fixed_size", "reason": "position_too_small", "fixed_size": str(fixed_size), "min_size": str(min_size), "size_step": str(size_step), "max_reduce_size": str(max_reduce_size)}, default=str))
                    return None
            if reduce_size <= Decimal("0"):
                self.logger.info(json.dumps({"error": "build_order_fixed_size", "reason": "size_too_small", "fixed_size": str(fixed_size), "min_size": str(min_size), "size_step": str(size_step), "reduce_size": str(reduce_size)}, default=str))
                return None
            instrument_hash = inst.get("instrument_hash")
            if instrument_hash is None:
                self.logger.info(json.dumps({"error": "build_order_fixed_size", "reason": "no_instrument_hash"}, default=str))
                return None
            asset_id = int(instrument_hash, 16) if isinstance(instrument_hash, str) and instrument_hash.startswith("0x") else int(instrument_hash)

            nonce = random.randint(0, 2**32 - 1)
            expiration_ns = int(time.time_ns() + 15 * 60 * 1_000_000_000)
            contract_size = int((reduce_size * size_multiplier).to_integral_value(rounding=ROUND_DOWN))
            if contract_size <= 0:
                self.logger.info(json.dumps({"error": "build_order_fixed_size", "reason": "invalid_contract_size", "contract_size": contract_size, "reduce_size": str(reduce_size), "size_step": str(size_step), "min_size": str(min_size)}, default=str))
                return None

            typed_legs = [{"assetID": asset_id, "contractSize": contract_size, "limitPrice": 0, "isBuyingContract": is_buying}]
            message_data = {
                "subAccountID": int(sub_id), "isMarket": True,
                "timeInForce": TIME_IN_FORCE_TO_SIGN_CODE["IMMEDIATE_OR_CANCEL"],
                "postOnly": False, "reduceOnly": True, "legs": typed_legs,
                "nonce": nonce, "expiration": expiration_ns,
            }
            domain_data = {"name": "GRVT Exchange", "version": "0", "chainId": chain_id}
            typed_msg = encode_typed_data(domain_data, EIP712_ORDER_MESSAGE_TYPE, message_data)
            signed = account.sign_message(typed_msg)
            client_order_id = str(random.randint(2**63, 2**64 - 1))

            return {
                "sub_account_id": sub_id, "is_market": True, "time_in_force": "IMMEDIATE_OR_CANCEL",
                "post_only": False, "reduce_only": True,
                "legs": [{"instrument": instrument_name, "size": self._decimal_to_str(reduce_size), "limit_price": None, "is_buying_asset": is_buying}],
                "signature": {
                    "signer": str(account.address),
                    "r": "0x" + hex(signed.r)[2:].zfill(64),
                    "s": "0x" + hex(signed.s)[2:].zfill(64),
                    "v": int(signed.v), "expiration": str(expiration_ns), "nonce": nonce,
                },
                "metadata": {"client_order_id": client_order_id},
            }
        except Exception as e:
            self.logger.info(json.dumps({"error": "build_order_fixed_size", "exception": str(e)}, default=str))
            return None

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

        Trigger: margin usage >= triggerPct (e.g., 60%)
        Recovery: margin usage < recoveryPct (e.g., 40%)
        Margin usage = maintenance_margin / equity × 100
        """
        base_cfg = self.cfg_repo.base()
        unwind_cfg = base_cfg.get("unwind", {})

        if not unwind_cfg.get("enabled", False):
            return {"action": "disabled"}

        trigger_pct = Decimal(str(unwind_cfg.get("triggerPct", 60)))
        recovery_pct = Decimal(str(unwind_cfg.get("recoveryPct", 40)))
        unwind_pct = Decimal(str(unwind_cfg.get("unwindPct", 10.0)))
        # No limit by default: unwind continues until both accounts recover.
        max_iterations = 999
        wait_seconds = int(unwind_cfg.get("waitSecondsBetweenIterations", 2))
        min_notional = Decimal(str(unwind_cfg.get("minPositionNotional", 100)))

        # Calculate margin percentages for logging
        pct1 = self.calc_margin_pct(eq1, mm1)
        pct2 = self.calc_margin_pct(eq2, mm2)
        pct1_str = f"{pct1:.1f}%" if pct1 is not None else "N/A"
        pct2_str = f"{pct2:.1f}%" if pct2 is not None else "N/A"

        # Check if either account triggers
        trigger1 = self.should_trigger(eq1, mm1, trigger_pct)
        trigger2 = self.should_trigger(eq2, mm2, trigger_pct)

        if not trigger1 and not trigger2:
            try:
                import state
                state.set_unwind_progress(in_progress=False)
            except Exception:
                pass
            return {
                "action": "no_trigger",
                "eq1": str(eq1), "mm1": str(mm1), "pct1": pct1_str,
                "eq2": str(eq2), "mm2": str(mm2), "pct2": pct2_str,
                "trigger_at": f"{trigger_pct}%",
            }

        self.logger.info(json.dumps({
            "unwind_triggered": True,
            "trigger1": trigger1,
            "trigger2": trigger2,
            "eq1": str(eq1), "mm1": str(mm1), "pct1": pct1_str,
            "eq2": str(eq2), "mm2": str(mm2), "pct2": pct2_str,
            "trigger_at": f"{trigger_pct}%",
            "recover_at": f"{recovery_pct}%",
        }, default=str))

        # Send alert
        try:
            from alerts.services import AlertService
            AlertService.dispatch_unwind_event({
                "triggered": True,
                "eq1": str(eq1),
                "mm1": str(mm1),
                "pct1": pct1_str,
                "eq2": str(eq2),
                "mm2": str(mm2),
                "pct2": pct2_str,
                "trigger_at": f"{trigger_pct}%",
                "trigger1": trigger1,
                "trigger2": trigger2,
                "dry_run": dry_run,
            })
        except Exception:
            pass

        # Fetch initial positions
        positions1 = PositionService.get_positions(cfg1)
        positions2 = PositionService.get_positions(cfg2)

        # Check for unmatched positions and alert
        matched, unmatched = self.match_positions_by_instrument(positions1, positions2)
        if unmatched:
            try:
                from alerts.services import AlertService
                AlertService.dispatch_warning({
                    "unmatched_positions": unmatched,
                    "message": "Hedge mismatch detected"
                })
            except Exception:
                pass

        results = []
        for iteration in range(max_iterations):
            # Refresh equity and margin
            from rebalance.services import SummaryService
            eq1, mm1, avail1, _ = SummaryService.trading_summary(cfg1)
            eq2, mm2, avail2, _ = SummaryService.trading_summary(cfg2)
            positions1 = PositionService.get_positions(cfg1)
            positions2 = PositionService.get_positions(cfg2)

            # Check recovery using percentage-based logic
            recovered1 = self.is_recovered(eq1, mm1, recovery_pct)
            recovered2 = self.is_recovered(eq2, mm2, recovery_pct)

            # Update shared status so Telegram '查看' reflects unwind progress.
            try:
                import state
                pct1 = self.calc_margin_pct(eq1, mm1)
                pct2 = self.calc_margin_pct(eq2, mm2)
                pct1_str = f"{pct1:.1f}%" if pct1 is not None else "N/A"
                pct2_str = f"{pct2:.1f}%" if pct2 is not None else "N/A"
                state.set_unwind_progress(
                    in_progress=True,
                    iteration=int(iteration) + 1,
                    pct_a=pct1_str,
                    pct_b=pct2_str,
                    trigger_pct=f"{trigger_pct}%",
                    recovery_pct=f"{recovery_pct}%",
                )
                state.set_last_check_time(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
                state.set_last_status({
                    "event_time_sh": state.get_last_check_time(),
                    "action": "unwind",
                    "trigger": str(base_cfg.get("triggerValue", "")),
                    "delta": str(eq1 - eq2),
                    "eq1": str(eq1),
                    "eq2": str(eq2),
                    "mm1": str(mm1),
                    "mm2": str(mm2),
                    "avail1": str(avail1),
                    "avail2": str(avail2),
                })
            except Exception:
                pass
            if recovered1 and recovered2:
                pct1 = self.calc_margin_pct(eq1, mm1)
                pct2 = self.calc_margin_pct(eq2, mm2)
                pct1_str = f"{pct1:.1f}%" if pct1 is not None else "N/A"
                pct2_str = f"{pct2:.1f}%" if pct2 is not None else "N/A"
                self.logger.info(json.dumps({
                    "unwind_recovered": True,
                    "iteration": iteration,
                    "pct1": pct1_str,
                    "pct2": pct2_str,
                }, default=str))
                # Send recovery alert
                try:
                    from alerts.services import AlertService
                    AlertService.dispatch_unwind_recovery({
                        "pct1": pct1_str,
                        "pct2": pct2_str,
                        "recovery_at": f"{recovery_pct}%",
                        "iteration": iteration,
                    })
                except Exception:
                    pass
                break

            # Unwind all matched (hedged) instruments, with order sizes proportional to current position sizes.
            # A single dynamic ratio is computed from margin stress and then applied to each position size.
            target_iters = min(max_iterations, 5) if max_iterations > 0 else 5
            computed_ratio = self.calc_unwind_ratio(eq1, mm1, eq2, mm2, recovery_pct, target_iters)
            max_ratio = (unwind_pct / Decimal("100")) if unwind_pct > Decimal("0") else Decimal("1")
            unwind_ratio = min(computed_ratio, max_ratio)
            if unwind_ratio <= Decimal("0"):
                continue

            matched, _ = self.match_positions_by_instrument(positions1, positions2)

            def _combined_pnl_per_notional(pos1: dict, pos2: dict) -> Decimal:
                try:
                    n1 = abs(Decimal(str(pos1.get("notional", "0"))))
                    n2 = abs(Decimal(str(pos2.get("notional", "0"))))
                    denom = n1 + n2
                    if denom <= Decimal("0"):
                        return Decimal("0")
                    p1 = abs(Decimal(str(pos1.get("unrealized_pnl", "0"))))
                    p2 = abs(Decimal(str(pos2.get("unrealized_pnl", "0"))))
                    return (p1 + p2) / denom
                except Exception:
                    return Decimal("0")

            batch: list[tuple[Decimal, str, dict, dict]] = []
            for instrument, (pos1, pos2) in matched.items():
                try:
                    n1 = abs(Decimal(str(pos1.get("notional", "0"))))
                    n2 = abs(Decimal(str(pos2.get("notional", "0"))))
                except Exception:
                    continue
                if min(n1, n2) < min_notional:
                    continue
                score = _combined_pnl_per_notional(pos1, pos2)
                batch.append((score, instrument, pos1, pos2))

            batch.sort(key=lambda t: t[0], reverse=True)

            try:
                self.logger.info(json.dumps({
                    "hedged_unwind_batch": True,
                    "iteration": iteration,
                    "num_instruments": len(batch),
                    "computed_ratio": str(computed_ratio),
                    "max_ratio": str(max_ratio),
                    "unwind_ratio": str(unwind_ratio),
                    "recovery_pct": str(recovery_pct),
                }, default=str))
            except Exception:
                pass

            for score, instrument, pos1, pos2 in batch:
                size1 = abs(Decimal(str(pos1.get("size", "0"))))
                size2 = abs(Decimal(str(pos2.get("size", "0"))))
                base_size = min(size1, size2)
                unwind_size = base_size * unwind_ratio
                if unwind_size <= Decimal("0"):
                    continue

                try:
                    self.logger.info(json.dumps({
                        "hedged_unwind_selected": instrument,
                        "iteration": iteration,
                        "score_pnl_per_notional": str(score),
                        "base_size": str(base_size),
                        "unwind_size_raw": str(unwind_size),
                    }, default=str))
                except Exception:
                    pass

                result1 = self.execute_unwind_fixed_size(cfg1, pos1, unwind_size, dry_run=dry_run)
                results.append({"account": "A", "iteration": iteration, **result1})
                result2 = self.execute_unwind_fixed_size(cfg2, pos2, unwind_size, dry_run=dry_run)
                results.append({"account": "B", "iteration": iteration, **result2})

                for label, result in [("A", result1), ("B", result2)]:
                    if not dry_run and not result.get("success"):
                        try:
                            from alerts.services import AlertService
                            AlertService.dispatch_unwind_order({
                                "success": False,
                                "account": label,
                                "instrument": instrument,
                                "size": (result.get("detail") or {}).get("order_size"),
                                "error": result.get("error", "unknown"),
                            })
                        except Exception:
                            pass

            if iteration < max_iterations - 1:
                time.sleep(wait_seconds)

        # Get final margin percentages
        from rebalance.services import SummaryService
        final_eq1, final_mm1, _, _ = SummaryService.trading_summary(cfg1)
        final_eq2, final_mm2, _, _ = SummaryService.trading_summary(cfg2)
        final_pct1 = self.calc_margin_pct(final_eq1, final_mm1)
        final_pct2 = self.calc_margin_pct(final_eq2, final_mm2)
        final_pct1_str = f"{final_pct1:.1f}%" if final_pct1 is not None else "N/A"
        final_pct2_str = f"{final_pct2:.1f}%" if final_pct2 is not None else "N/A"

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
            "final_pct1": final_pct1_str,
            "final_pct2": final_pct2_str,
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

        try:
            import state
            state.set_unwind_progress(in_progress=False)
        except Exception:
            pass

        return summary
