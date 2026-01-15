import os
import json
from pathlib import Path


def _gui_settings_path() -> Path:
    # Match GUI behavior: prefer APPDATA on Windows, otherwise fall back to home dir.
    base = os.getenv("APPDATA") or str(Path.home())
    return Path(base) / "grvt-transfer" / "settings.json"


def _read_gui_settings() -> dict:
    p = _gui_settings_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _select_env_from_gui_settings(settings: dict) -> str | None:
    try:
        env = str((settings or {}).get("selected_env") or "").strip().lower()
        return env if env in ("prod", "test") else None
    except Exception:
        return None


def _apply_gui_env_overrides(env: str, settings: dict) -> None:
    """
    If GUI settings exist locally, prefer them over .env/.env.<env>.

    We only override environment variables when the GUI value is non-empty.
    """
    try:
        envs = (settings or {}).get("envs") or {}
        blob = envs.get(env) or {}
        if not isinstance(blob, dict):
            return

        def _set(name: str, value: str | None):
            v = ("" if value is None else str(value)).strip()
            if v:
                os.environ[name] = v

        _set("TELEGRAM_BOT_TOKEN", blob.get("telegram_token"))
        _set("TELEGRAM_CHAT_ID", blob.get("telegram_chat_id"))

        a = blob.get("account_a") or {}
        b = blob.get("account_b") or {}
        if not isinstance(a, dict) or not isinstance(b, dict):
            return

        def _apply_account(prefix: str, acc: dict):
            _set(f"{prefix}_ACCOUNT_ID", acc.get("account_id"))
            _set(f"{prefix}_FUNDING_ACCOUNT_ADDRESS", acc.get("funding_account_address"))
            _set(f"{prefix}_TRADING_ACCOUNT_ID", acc.get("trading_account_id"))
            _set(f"{prefix}_FUNDING_ACCOUNT_KEY", acc.get("fundingAccountKey"))
            _set(f"{prefix}_FUNDING_ACCOUNT_SECRET", acc.get("fundingAccountSecret"))
            _set(f"{prefix}_TRADING_ACCOUNT_KEY", acc.get("tradingAccountKey"))
            _set(f"{prefix}_TRADING_ACCOUNT_SECRET", acc.get("tradingAccountSecret"))

        _apply_account("ACC1", a)
        _apply_account("ACC2", b)
    except Exception:
        # Best-effort only; never break app startup because of local GUI settings.
        return


def load_env() -> bool:
    """
    Load environment variables from .env files (if python-dotenv is installed).

    Loading order:
      1) .env (no override)
      2) .env.<GRVT_ENV> (override), e.g. .env.prod / .env.test
    """
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        return False

    root = Path(__file__).resolve().parent

    # First, load base .env so it can define GRVT_ENV for env-specific loading.
    load_dotenv(dotenv_path=root / ".env", override=False)

    settings = _read_gui_settings()
    env = (os.getenv("GRVT_ENV") or _select_env_from_gui_settings(settings) or "prod").lower()
    if "GRVT_ENV" not in os.environ:
        os.environ["GRVT_ENV"] = env
    load_dotenv(dotenv_path=root / f".env.{env}", override=True)

    # Finally, prefer GUI-stored local settings when present (non-empty fields only).
    _apply_gui_env_overrides(env, settings)
    return True
