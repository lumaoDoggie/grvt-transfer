from datetime import datetime

try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


class TimeUtil:
    @staticmethod
    def _sh_tz():
        if ZoneInfo is None:
            return None
        try:
            return ZoneInfo("Asia/Shanghai")
        except Exception:
            return None

    @staticmethod
    def event_time_sh(obj: dict):
        tz = TimeUtil._sh_tz()
        try:
            ns = int(str(obj.get("event_time")))
            dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=tz) if tz else datetime.fromtimestamp(ns / 1_000_000_000)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return (datetime.now(tz) if tz else datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


class FundingUtil:
    @staticmethod
    def funding_usdt_from_summary(summary_obj: dict, currency: str = "USDT"):
        try:
            for b in (summary_obj.get("result", {}).get("spot_balances", []) or []):
                if str(b.get("currency")) == currency:
                    return str(b.get("balance"))
        except Exception:
            pass
        return "0"


class TxUtil:
    @staticmethod
    def success(info: dict, key: str):
        d = info.get(key, {})
        r = d.get("result", {}) if isinstance(d, dict) else {}
        return bool(r.get("ack", False))

    @staticmethod
    def tx_id(info: dict, key: str):
        d = info.get(key, {})
        r = d.get("result", {}) if isinstance(d, dict) else {}
        return r.get("tx_id")
