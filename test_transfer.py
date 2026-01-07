import json
from decimal import Decimal

from repository import ConfigRepository
from rebalance.services import SummaryService
from flow import TransferFlow
from rebalance_trading_equity import setup_logger


def main():
    repo = ConfigRepository()
    base_cfg = repo.base()
    logger = setup_logger(base_cfg)
    cfg1, cfg2 = repo.accounts()

    eq1_pre, _, _, _ = SummaryService.trading_summary(cfg1)
    eq2_pre, _, _, _ = SummaryService.trading_summary(cfg2)

    ok, info = TransferFlow.execute(cfg1, cfg2, Decimal("1.00"), throttle_ms=1500, logger=logger)

    eq1_post, _, _, _ = SummaryService.trading_summary(cfg1)
    eq2_post, _, _, _ = SummaryService.trading_summary(cfg2)

    out = {
        "success": bool(info.get("deposit_tx", {}).get("result", {}).get("ack", False)) if ok else False,
        "transfer_usdt": "1.000000",
        "tx_ids": {
            "internal": info.get("internal_tx", {}).get("result", {}).get("tx_id"),
            "funding_to_funding": info.get("funding_to_funding_tx", {}).get("result", {}).get("tx_id"),
            "deposit": info.get("deposit_tx", {}).get("result", {}).get("tx_id"),
        },
        "pre": {"eq1": str(eq1_pre), "eq2": str(eq2_pre)},
        "post": {"eq1": str(eq1_post), "eq2": str(eq2_post)},
    }
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
