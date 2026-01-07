import os
import yaml

from envutil import load_env as _load_env

_load_env()


def _get_env() -> str:
    """Get environment from GRVT_ENV env var, defaults to 'prod'."""
    return os.getenv("GRVT_ENV", "prod").lower()


def _config_dir() -> str:
    """Get config directory based on environment."""
    env = _get_env()
    env_dir = os.path.join("config", env)
    if os.path.isdir(env_dir):
        return env_dir
    # Fallback to root directory for backward compatibility
    return "."


def _load_yaml_optional(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _env_str(name: str) -> str | None:
    v = os.getenv(name)
    if v is None:
        return None
    v = str(v).strip()
    return v if v else None


def _apply_account_env_overrides(cfg: dict, prefix: str) -> dict:
    """
    Overlay account secrets from environment variables, e.g.:
      ACC1_FUNDING_ACCOUNT_KEY, ACC1_FUNDING_ACCOUNT_SECRET,
      ACC1_TRADING_ACCOUNT_KEY, ACC1_TRADING_ACCOUNT_SECRET
    """
    out = dict(cfg or {})

    account_id = _env_str(f"{prefix}_ACCOUNT_ID")
    funding_addr = _env_str(f"{prefix}_FUNDING_ACCOUNT_ADDRESS")
    trading_sub_id = _env_str(f"{prefix}_TRADING_ACCOUNT_ID")
    chain_id = _env_str(f"{prefix}_CHAIN_ID")
    currency = _env_str(f"{prefix}_CURRENCY")

    funding_key = _env_str(f"{prefix}_FUNDING_ACCOUNT_KEY")
    funding_secret = _env_str(f"{prefix}_FUNDING_ACCOUNT_SECRET")
    trading_key = _env_str(f"{prefix}_TRADING_ACCOUNT_KEY")
    trading_secret = _env_str(f"{prefix}_TRADING_ACCOUNT_SECRET")

    if account_id is not None:
        out["account_id"] = account_id
    if funding_addr is not None:
        out["funding_account_address"] = funding_addr
    if trading_sub_id is not None:
        out["trading_account_id"] = trading_sub_id
    if chain_id is not None:
        out["chain_id"] = chain_id
    if currency is not None:
        out["currency"] = currency

    if funding_key is not None:
        out["fundingAccountKey"] = funding_key
    if funding_secret is not None:
        out["fundingAccountSecret"] = funding_secret
    if trading_key is not None:
        # Support both the historical typo and the corrected key name.
        out["tradingAccountKey"] = trading_key
        out["tradingAcccountKey"] = trading_key
    if trading_secret is not None:
        out["tradingAccountSecret"] = trading_secret

    return out


class ConfigRepository:
    def __init__(self):
        self._env = _get_env()
        self._config_dir = _config_dir()

    def env(self) -> str:
        """Return current environment name (test/prod)."""
        return self._env

    def base(self) -> dict:
        cfg_path = os.path.join(self._config_dir, "config.yaml")
        if not os.path.exists(cfg_path):
            # Fallback to root config.yaml
            cfg_path = "config.yaml"
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}

    def accounts(self) -> tuple[dict, dict]:
        c1_path = os.path.join(self._config_dir, "account_1_config.yaml")
        c2_path = os.path.join(self._config_dir, "account_2_config.yaml")
        # Fallback to root if not in env folder
        if not os.path.exists(c1_path):
            c1_path = "account_1_config.yaml"
        if not os.path.exists(c2_path):
            c2_path = "account_2_config.yaml"

        cfg1 = _apply_account_env_overrides(_load_yaml_optional(c1_path), "ACC1")
        cfg2 = _apply_account_env_overrides(_load_yaml_optional(c2_path), "ACC2")
        return cfg1, cfg2

    def logger(self) -> dict:
        log_path = os.path.join(self._config_dir, "log-config.yaml")
        if not os.path.exists(log_path):
            log_path = "log-config.yaml"
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}


class ClientFactory:
    from pysdk.grvt_raw_env import GrvtEnv
    from pysdk.grvt_raw_base import GrvtApiConfig
    from pysdk.grvt_raw_sync import GrvtRawSync

    @staticmethod
    def _get_grvt_env():
        """Get GrvtEnv based on GRVT_ENV environment variable."""
        env = _get_env()
        if env == "test":
            return ClientFactory.GrvtEnv.TESTNET
        return ClientFactory.GrvtEnv.PROD

    @staticmethod
    def trading_client(cfg: dict) -> "ClientFactory.GrvtRawSync":
        api_config = ClientFactory.GrvtApiConfig(
            env=ClientFactory._get_grvt_env(),
            trading_account_id=str(cfg.get("trading_account_id", "")),
            private_key=str(cfg.get("tradingAccountSecret", "")),
            api_key=str(cfg.get("tradingAccountKey", cfg.get("tradingAcccountKey", cfg.get("fundingAccountKey", "")))),
            logger=None,
        )
        return ClientFactory.GrvtRawSync(api_config)

    @staticmethod
    def funding_client(cfg: dict) -> "ClientFactory.GrvtRawSync":
        api_config = ClientFactory.GrvtApiConfig(
            env=ClientFactory._get_grvt_env(),
            trading_account_id=str(cfg.get("account_id", cfg.get("trading_account_id", ""))),
            private_key=str(cfg.get("fundingAccountSecret", "")),
            api_key=str(cfg.get("fundingAccountKey", "")),
            logger=None,
        )
        return ClientFactory.GrvtRawSync(api_config)
