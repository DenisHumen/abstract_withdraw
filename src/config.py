"""Загрузка конфигурации: .env (секреты) + config.yaml (параметры запуска)."""
from __future__ import annotations

import random
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

NATIVE_TOKEN = "0x0000000000000000000000000000000000000000"
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
RELAY_API_BASE = "https://api.relay.link"


class EnvSettings(BaseSettings):
    """Секреты/эндпоинты из .env. Ключи сторонних API не используются by design."""

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    wallet_encryption_key: str = ""
    abstract_rpc_urls: str = "https://api.mainnet.abs.xyz"
    base_rpc_urls: str = "https://mainnet.base.org"
    adspower_api_key: str = ""
    adspower_base_url: str = "http://local.adspower.net:50325"

    def rpc_pool(self, chain: str) -> list[str]:
        raw = self.abstract_rpc_urls if chain == "abstract" else self.base_rpc_urls
        return [u.strip() for u in raw.split(",") if u.strip()]


class ModeCfg(BaseModel):
    forward_mode: str = "two_step"
    use_browser_fallback: str = "never"
    dry_run: bool = False


class RoutingCfg(BaseModel):
    origin_chain_id: int = 2741
    dest_chain_id: int = 8453
    dest_currency: str = NATIVE_TOKEN
    trade_type: str = "EXACT_INPUT"
    slippage_bps: int = 50


class AmountsCfg(BaseModel):
    bridge_full_balance: bool = True
    gas_estimate_multiplier: float = 1.5
    gas_reserve_abstract_floor_wei: str = "300000000000000"
    gas_reserve_base_floor_wei: str = "100000000000000"
    min_native_out_wei: str = "200000000000000"
    skip_if_out_lte_forward_gas: bool = True

    @property
    def abstract_floor(self) -> int:
        return int(self.gas_reserve_abstract_floor_wei)

    @property
    def base_floor(self) -> int:
        return int(self.gas_reserve_base_floor_wei)

    @property
    def min_out(self) -> int:
        return int(self.min_native_out_wei)


class ExecutionCfg(BaseModel):
    concurrency: int = 3
    quote_ttl_sec: int = 30
    status_poll_interval_sec: int = 2
    status_timeout_sec: int = 900
    tx_confirmations: int = 1
    wallet_delay_sec: tuple[float, float] = (3, 10)

    def random_delay(self) -> float:
        lo, hi = self.wallet_delay_sec
        return random.uniform(lo, hi)


class RpcCfg(BaseModel):
    rotate_on_failure: bool = True
    health_check: bool = True
    timeout_sec: int = 20
    max_rotations_per_call: int = 3


class RetryCfg(BaseModel):
    max_attempts: int = 5
    backoff_base_sec: float = 2
    backoff_max_sec: float = 60


class ProxyCfg(BaseModel):
    enabled: bool = True
    scheme: str = "http"
    pool_file: str = "data/proxies.txt"
    per_wallet_from_xlsx: bool = True
    rotate_on_failure: bool = True
    max_rotations_per_job: int = 3
    health_check: bool = True
    health_check_timeout_sec: int = 8
    sticky: bool = True
    persist_assignment: bool = True


class TokensCfg(BaseModel):
    discovery: str = "relay_currencies_plus_onchain"
    include_native_eth: bool = True
    verified_only: bool = False
    use_external_search: bool = False
    currencies_limit: int = 5000
    multicall_chunk: int = 200
    allowlist: list[str] = Field(default_factory=list)
    denylist: list[str] = Field(default_factory=list)


class PathsCfg(BaseModel):
    wallets_xlsx: str = "data/wallets.xlsx"
    db: str = "data/state.db"
    logs_dir: str = "logs"


class AppConfig(BaseModel):
    mode: ModeCfg = Field(default_factory=ModeCfg)
    routing: RoutingCfg = Field(default_factory=RoutingCfg)
    amounts: AmountsCfg = Field(default_factory=AmountsCfg)
    execution: ExecutionCfg = Field(default_factory=ExecutionCfg)
    rpc: RpcCfg = Field(default_factory=RpcCfg)
    retry: RetryCfg = Field(default_factory=RetryCfg)
    proxy: ProxyCfg = Field(default_factory=ProxyCfg)
    tokens: TokensCfg = Field(default_factory=TokensCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)

    env: EnvSettings = Field(default_factory=EnvSettings)

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else PROJECT_ROOT / p


def load_config(yaml_path: str | Path | None = None) -> AppConfig:
    load_dotenv(PROJECT_ROOT / ".env")
    path = Path(yaml_path) if yaml_path else PROJECT_ROOT / "config.yaml"
    data: dict = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return AppConfig(**data)
