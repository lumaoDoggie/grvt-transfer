import os
from pathlib import Path


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
    env = os.getenv("GRVT_ENV", "prod").lower()

    load_dotenv(dotenv_path=root / ".env", override=False)
    load_dotenv(dotenv_path=root / f".env.{env}", override=True)
    return True
