import json
import logging
import os
import threading
from decimal import Decimal

from rebalance.services import RebalanceService
from bot.telegram_bot import start_bot_daemon, stop_bot
from rebalance_trading_equity import setup_logger, setup_noop_logger


class InMemoryConfigRepository:
    """
    Minimal ConfigRepository-like object that backs config with in-memory dicts.

    This lets the GUI run the existing RebalanceService without requiring users
    to edit YAML files under config/.
    """

    def __init__(self, env: str, base_cfg: dict, acc1_cfg: dict, acc2_cfg: dict):
        self._env = str(env or "prod").lower()
        self._base_cfg = dict(base_cfg or {})
        self._a = dict(acc1_cfg or {})
        self._b = dict(acc2_cfg or {})

    def env(self) -> str:
        return self._env

    def base(self) -> dict:
        return dict(self._base_cfg)

    def accounts(self) -> tuple[dict, dict]:
        return dict(self._a), dict(self._b)

    def logger(self) -> dict:
        return {}


class RebalanceRunner:
    def __init__(self, cfg_repo: InMemoryConfigRepository, throttle_ms: int = 2000):
        self._cfg_repo = cfg_repo
        self._throttle_ms = int(throttle_ms)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()

    def start(self) -> bool:
        if self.running():
            return False
        self._stop_event.clear()
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        self._thread = t
        return True

    def request_stop(self) -> None:
        """Signal the loop to stop ASAP (non-blocking)."""
        self._stop_event.set()
        try:
            stop_bot()
        except Exception:
            pass

    def stop(self, timeout_sec: float = 10.0) -> None:
        self.request_stop()
        try:
            if self._thread is not None:
                self._thread.join(timeout=float(timeout_sec))
        except Exception:
            pass
        self._thread = None

    def _run(self) -> None:
        os.environ["GRVT_ENV"] = self._cfg_repo.env()

        base = self._cfg_repo.base()
        trigger = Decimal(str(base.get("triggerValue", "0")))
        interval = int(base.get("rebalanceIntervalSec", 15))

        logger = setup_logger(base)
        noop_logger = setup_noop_logger(base)

        bot_status = start_bot_daemon()
        try:
            logger.info(json.dumps({"loop_started": True, "pid": os.getpid(), "bot_status": bot_status}, default=str))
        except Exception:
            pass

        svc = RebalanceService(self._cfg_repo, logger, noop_logger)

        # Loop until stopped. Use Event.wait() so stop is responsive.
        while not self._stop_event.is_set():
            try:
                out = svc.rebalance_once(trigger, throttle_ms=self._throttle_ms)
                try:
                    logger.info(json.dumps({"rebalance_once": out}, default=str))
                except Exception:
                    pass
            except Exception as e:
                try:
                    logger.info(json.dumps({"rebalance_loop_error": str(e)}, default=str))
                    from alerts.services import AlertService

                    AlertService.dispatch_warning({"rebalance_error": str(e)})
                except Exception:
                    pass

            # Stop promptly, otherwise wait for the next interval.
            if self._stop_event.wait(timeout=max(1, interval)):
                break

        try:
            logger.info(json.dumps({"loop_stopped": True, "pid": os.getpid()}, default=str))
        except Exception:
            pass

        try:
            stop_bot()
        except Exception:
            pass
