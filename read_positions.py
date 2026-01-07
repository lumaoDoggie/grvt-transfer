import json
from repository import ConfigRepository
from rebalance.services import SummaryService


def main():
    repo = ConfigRepository()
    cfg1, cfg2 = repo.accounts()
    eq1, mm1, avail1, obj1 = SummaryService.trading_summary(cfg1)
    eq2, mm2, avail2, obj2 = SummaryService.trading_summary(cfg2)
    out = {
        "account_1": {
            "equity": str(eq1),
            "maintenance_margin": str(mm1),
            "available": str(avail1),
            "raw": obj1,
        },
        "account_2": {
            "equity": str(eq2),
            "maintenance_margin": str(mm2),
            "available": str(avail2),
            "raw": obj2,
        },
    }
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()

