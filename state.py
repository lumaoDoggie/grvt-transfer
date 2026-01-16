# Shared in-process state (used by rebalance loop + Telegram bot thread).
# This is best-effort runtime state only; it is not persisted across restarts.

import threading
import time

_lock = threading.Lock()
_last_check_time: str | None = None
_last_status: dict | None = None
_unwind_progress: dict = {"in_progress": False}


def set_last_check_time(t: str):
    global _last_check_time
    with _lock:
        _last_check_time = t


def get_last_check_time() -> str | None:
    with _lock:
        return _last_check_time


def set_last_status(obj: dict):
    """Store the most recent status snapshot for Telegram '查看'."""
    global _last_status
    with _lock:
        _last_status = dict(obj or {})


def get_last_status() -> dict | None:
    with _lock:
        return dict(_last_status) if isinstance(_last_status, dict) else None


def set_unwind_progress(
    in_progress: bool,
    iteration: int | None = None,
    pct_a: str | None = None,
    pct_b: str | None = None,
    trigger_pct: str | None = None,
    recovery_pct: str | None = None,
):
    """Track unwind progress so status can show '第 N 轮' and current ratios."""
    with _lock:
        _unwind_progress["in_progress"] = bool(in_progress)
        if iteration is not None:
            _unwind_progress["iteration"] = int(iteration)
        if pct_a is not None:
            _unwind_progress["pct_a"] = str(pct_a)
        if pct_b is not None:
            _unwind_progress["pct_b"] = str(pct_b)
        if trigger_pct is not None:
            _unwind_progress["trigger_pct"] = str(trigger_pct)
        if recovery_pct is not None:
            _unwind_progress["recovery_pct"] = str(recovery_pct)
        _unwind_progress["updated_ts"] = time.time()


def get_unwind_progress() -> dict:
    with _lock:
        return dict(_unwind_progress)
