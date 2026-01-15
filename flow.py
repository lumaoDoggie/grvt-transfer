import json
import time
import logging
from decimal import Decimal
from eth_account import Account as EthAccount
from repository import ClientFactory, _get_env, get_chain_id
from pysdk.grvt_raw_base import GrvtApiConfig
from pysdk.grvt_raw_env import GrvtEnv
from rebalance.services import TransferService


def _get_grvt_env():
    """Get GrvtEnv based on GRVT_ENV environment variable."""
    env = _get_env()
    if env == "test":
        return GrvtEnv.TESTNET
    return GrvtEnv.PROD


class TransferFlow:
    @staticmethod
    def execute(a_cfg: dict, b_cfg: dict, amount_dec: Decimal, throttle_ms: int = 0, logger: logging.Logger | None = None):
        currency = "USDT"
        chain_id = get_chain_id()
        amt_str = f"{amount_dec:.6f}"

        a_funding_addr = str(a_cfg.get("funding_account_address"))
        a_trading_sub = str(a_cfg.get("trading_account_id"))
        b_funding_addr = str(b_cfg.get("funding_account_address"))
        b_trading_sub = str(b_cfg.get("trading_account_id"))

        api_a_trading = GrvtApiConfig(
            env=_get_grvt_env(),
            trading_account_id=a_trading_sub,
            private_key=str(a_cfg.get("tradingAccountSecret")),
            api_key=str(a_cfg.get("tradingAccountKey", a_cfg.get("tradingAcccountKey", a_cfg.get("fundingAccountKey", "")))),
            logger=None,
        )
        api_a_funding = GrvtApiConfig(
            env=_get_grvt_env(),
            trading_account_id=str(a_cfg.get("account_id", a_trading_sub)),
            private_key=str(a_cfg.get("fundingAccountSecret")),
            api_key=str(a_cfg.get("fundingAccountKey")),
            logger=None,
        )
        api_b_funding = GrvtApiConfig(
            env=_get_grvt_env(),
            trading_account_id=str(b_cfg.get("account_id", b_trading_sub)),
            private_key=str(b_cfg.get("fundingAccountSecret")),
            api_key=str(b_cfg.get("fundingAccountKey")),
            logger=None,
        )
        api_b_trading = GrvtApiConfig(
            env=_get_grvt_env(),
            trading_account_id=b_trading_sub,
            private_key=str(b_cfg.get("tradingAccountSecret")),
            api_key=str(b_cfg.get("tradingAccountKey", b_cfg.get("tradingAcccountKey", b_cfg.get("fundingAccountKey", "")))),
            logger=None,
        )

        client_a_trading = ClientFactory.GrvtRawSync(api_a_trading)
        client_a_funding = ClientFactory.GrvtRawSync(api_a_funding)
        client_b_funding = ClientFactory.GrvtRawSync(api_b_funding)
        client_b_trading = ClientFactory.GrvtRawSync(api_b_trading)

        acct_a_trading = EthAccount.from_key(str(a_cfg.get("tradingAccountSecret")))
        acct_a_funding = EthAccount.from_key(str(a_cfg.get("fundingAccountSecret")))
        acct_b_funding = EthAccount.from_key(str(b_cfg.get("fundingAccountSecret")))
        acct_b_trading = EthAccount.from_key(str(b_cfg.get("tradingAccountSecret")))

        req_a_internal = TransferService.build_req(api_a_trading, acct_a_trading, a_funding_addr, a_trading_sub, a_funding_addr, "0", currency, amt_str, chain_id)
        if throttle_ms > 0:
            time.sleep(throttle_ms / 1000.0)
        ok1, info1 = TransferService.try_transfer(client_a_trading, req_a_internal)
        if not ok1:
            try:
                logging.getLogger("errors").info(json.dumps({"error": "internal_transfer_failed", "detail": info1}, default=str))
            except Exception:
                pass
            return False, info1

        req_ff = TransferService.build_req(api_a_funding, acct_a_funding, a_funding_addr, "0", b_funding_addr, "0", currency, amt_str, chain_id)
        ok2, info2 = TransferService.try_transfer(client_a_funding, req_ff)
        if not ok2:
            try:
                logging.getLogger("errors").info(json.dumps({"error": "funding_to_funding_failed", "detail": info2}, default=str))
            except Exception:
                pass
            return False, info2

        req_b_deposit = TransferService.build_req(api_b_funding, acct_b_funding, b_funding_addr, "0", b_funding_addr, b_trading_sub, currency, amt_str, chain_id)
        ok3, info3 = TransferService.try_transfer(client_b_funding, req_b_deposit)
        if not ok3:
            try:
                logging.getLogger("errors").info(json.dumps({"error": "deposit_failed", "detail": info3}, default=str))
            except Exception:
                pass
            return False, info3

        return True, {
            "internal_tx": info1,
            "funding_to_funding_tx": info2,
            "deposit_tx": info3,
        }


class BalanceSweeper:
    @staticmethod
    def sweep(cfg: dict, threshold: Decimal, throttle_ms: int = 0, logger: logging.Logger | None = None):
        from rebalance.services import SummaryService
        from pysdk.grvt_raw_base import GrvtApiConfig
        from pysdk.grvt_raw_env import GrvtEnv
        bal, _ = SummaryService.funding_usdt_balance(cfg)
        if bal <= threshold:
            return False, {"balance": str(bal)}
        chain_id = get_chain_id()
        currency = "USDT"
        amt_str = f"{bal:.6f}"
        funding_addr = str(cfg.get("funding_account_address"))
        trading_sub = str(cfg.get("trading_account_id"))
        api_funding = GrvtApiConfig(
            env=_get_grvt_env(),
            trading_account_id=str(cfg.get("account_id", trading_sub)),
            private_key=str(cfg.get("fundingAccountSecret")),
            api_key=str(cfg.get("fundingAccountKey")),
            logger=None,
        )
        client_funding = ClientFactory.GrvtRawSync(api_funding)
        acct_funding = EthAccount.from_key(str(cfg.get("fundingAccountSecret")))
        req_deposit = TransferService.build_req(api_funding, acct_funding, funding_addr, "0", funding_addr, trading_sub, currency, amt_str, chain_id)
        if throttle_ms > 0:
            time.sleep(throttle_ms / 1000.0)
        ok, info = TransferService.try_transfer(client_funding, req_deposit)
        if logger:
            logger.info(json.dumps({"funding_sweep": {"pre_balance": str(bal), "result": info}}, default=str))
        if not ok:
            try:
                logging.getLogger("errors").info(json.dumps({"error": "funding_sweep_failed", "detail": info}, default=str))
            except Exception:
                pass
        return ok, info
