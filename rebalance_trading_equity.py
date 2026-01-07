import yaml
import time
from decimal import Decimal
import os
import json
import logging
from logging.config import dictConfig

from eth_account import Account as EthAccount

from pysdk.grvt_raw_env import GrvtEnv
from pysdk.grvt_raw_base import GrvtApiConfig, GrvtError
from pysdk.grvt_raw_sync import GrvtRawSync, types
from pysdk import grvt_fixed_types as ft
from pysdk import grvt_raw_types as rt
from repository import ConfigRepository, ClientFactory
from rebalance.services import SummaryService, TransferService
from flow import TransferFlow, BalanceSweeper
from utils import TimeUtil, FundingUtil, TxUtil
from rebalance.services import RebalanceService
from bot.telegram_bot import start_bot_daemon, stop_bot
import atexit


def load_yaml(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def acct_clients(cfg: dict, use_trading: bool):
    return ClientFactory.trading_client(cfg) if use_trading else ClientFactory.funding_client(cfg)


def get_trading_summary(cfg: dict, client=None):
    return SummaryService.trading_summary(cfg, client)


def get_funding_summary(cfg: dict, client=None):
    return SummaryService.funding_summary(cfg, client)


def get_funding_usdt_balance(cfg: dict, client=None):
    return SummaryService.funding_usdt_balance(cfg, client)


def sweep_funding_to_trading(cfg: dict, threshold: Decimal, throttle_ms: int = 0, logger: logging.Logger | None = None):
    return BalanceSweeper.sweep(cfg, threshold, throttle_ms=throttle_ms, logger=logger)


def init_logging():
    cfg_path = "log-config.yaml"
    logs_dir = "logs"
    try:
        os.makedirs(logs_dir, exist_ok=True)
    except Exception:
        pass
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict) and "version" in data:
            dictConfig(data)


def setup_logger(base_cfg: dict):
    init_logging()
    return logging.getLogger("rebalance")


def setup_noop_logger(base_cfg: dict):
    init_logging()
    return logging.getLogger("rebalance.noop")


def build_req(api_config: GrvtApiConfig,
              account: EthAccount,
              from_addr: str,
              from_sub: str,
              to_addr: str,
              to_sub: str,
              currency: str,
              amount: str,
              chain_id: int,
              t_type: ft.TransferType = ft.TransferType.STANDARD):
    return TransferService.build_req(api_config, account, from_addr, from_sub, to_addr, to_sub, currency, amount, chain_id, t_type)


def _try_transfer(client: GrvtRawSync, req: rt.ApiTransferRequest, retries: int = 2, backoff_ms: int = 1500):
    return TransferService.try_transfer(client, req, retries=retries, backoff_ms=backoff_ms)


def flow_transfer(a_cfg: dict, b_cfg: dict, amount_dec: Decimal, throttle_ms: int = 0, logger: logging.Logger | None = None):
    return TransferFlow.execute(a_cfg, b_cfg, amount_dec, throttle_ms=throttle_ms, logger=logger)


def rebalance_once(trigger: Decimal, throttle_ms: int = 0):
    repo = ConfigRepository()
    base_cfg = repo.base()
    logger = setup_logger(base_cfg)
    noop_logger = setup_noop_logger(base_cfg)
    return RebalanceService(repo, logger, noop_logger).rebalance_once(trigger, throttle_ms=throttle_ms)


def main_cli():
    import argparse
    repo = ConfigRepository()
    base = repo.base()
    parser = argparse.ArgumentParser(prog="rebalance", add_help=True)
    parser.add_argument("--interval", type=int, default=None)
    parser.add_argument("--trigger", type=float, default=None)
    parser.add_argument("--throttleMs", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    trigger = Decimal(str(args.trigger if args.trigger is not None else base.get("triggerValue", "0.03")))
    interval = int(args.interval if args.interval is not None else int(base.get("rebalanceIntervalSec", 10)))
    throttle_ms = int(args.throttleMs if args.throttleMs is not None else int(base.get("rebalanceThrottleMs", 0)))
    once = bool(args.once or bool(base.get("rebalanceOnce", False)))
    logger = setup_logger(base)
    noop_logger = setup_noop_logger(base)
    bot_status = start_bot_daemon()
    try:
        logger.info(json.dumps({"loop_started": True, "pid": os.getpid(), "bot_status": bot_status}, default=str))
    except Exception:
        pass
    pid_path = "rebalance_loop.pid"
    try:
        with open(pid_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    def _rm_pid():
        try:
            if os.path.exists(pid_path):
                os.remove(pid_path)
        except Exception:
            pass
    atexit.register(_rm_pid)
    atexit.register(stop_bot)
    svc = RebalanceService(repo, logger, noop_logger)
    if once:
        out = svc.rebalance_once(trigger, throttle_ms=throttle_ms)
        print(out)
        return
    while True:
        try:
            out = svc.rebalance_once(trigger, throttle_ms=throttle_ms)
            print(out)
        except Exception as e:
            try:
                logger.info(json.dumps({"rebalance_loop_error": str(e)}, default=str))
                from alerts.services import AlertService
                AlertService.dispatch_warning({"rebalance_error": str(e)})
            except Exception:
                pass
        time.sleep(interval)


if __name__ == "__main__":
    main_cli()
def _event_time_sh(obj: dict):
    return TimeUtil.event_time_sh(obj)


def _funding_usdt_from_summary(summary_obj: dict, currency: str = "USDT"):
    return FundingUtil.funding_usdt_from_summary(summary_obj, currency=currency)


def _tx_success(info: dict, key: str):
    return TxUtil.success(info, key)


def _tx_id(info: dict, key: str):
    return TxUtil.tx_id(info, key)
