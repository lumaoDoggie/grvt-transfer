from bot.telegram_bot import start_bot_daemon, _get_chat_id
import time
import os
import json
import yaml
import logging
from logging.config import dictConfig

def _init_logging():
    try:
        os.makedirs("logs", exist_ok=True)
    except Exception:
        pass
    p = "log-config.yaml"
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            if isinstance(cfg, dict) and cfg.get("version"):
                dictConfig(cfg)
        except Exception:
            pass

if __name__ == "__main__":
    _init_logging()
    status = start_bot_daemon()
    logger = logging.getLogger("alerts")
    try:
        logger.info(json.dumps({"bot_run_started": True, "status": status}, default=str))
    except Exception:
        pass
    while True:
        try:
            logger.info(json.dumps({"bot_heartbeat": True, "chat_id": _get_chat_id()}))
            from bot.telegram_bot import _save_state
            try:
                import time as _t
                _save_state({"heartbeat_ts": _t.time(), "chat_id": _get_chat_id()})
            except Exception:
                pass
        except Exception:
            pass
        time.sleep(10)
