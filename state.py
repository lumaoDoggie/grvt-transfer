# Shared state for tracking last check time
_last_check_time: str | None = None

def set_last_check_time(t: str):
    global _last_check_time
    _last_check_time = t

def get_last_check_time() -> str | None:
    return _last_check_time
