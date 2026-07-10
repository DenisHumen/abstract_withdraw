"""Discovery токенов на Abstract БЕЗ эксплорер-API.

Схема (PLAN.md §5, шаг 1):
  токен-юниверс = Relay POST /currencies/v1 {chainIds:[2741]}  (∪ allowlist, − denylist)
  балансы       = Multicall3.balanceOf по публичному RPC + eth_getBalance (native)
"""
from __future__ import annotations

from dataclasses import dataclass

from web3 import Web3

from src.chains.multicall import batch_balance_of
from src.config import NATIVE_TOKEN, TokensCfg
from src.relay.client import RelayClient
from src import logger


@dataclass
class DiscoveredToken:
    address: str  # lower-case; NATIVE_TOKEN = native ETH
    symbol: str
    decimals: int
    balance: int
    routable: bool


def discover_tokens(
    w3: Web3,
    relay: RelayClient,
    holder: str,
    chain_id: int,
    cfg: TokensCfg,
) -> list[DiscoveredToken]:
    """Все токены с ненулевым балансом, потенциально маршрутизируемые Relay."""
    currencies = relay.get_currencies(
        chain_id,
        verified_only=cfg.verified_only,
        use_external_search=cfg.use_external_search,
        limit=cfg.currencies_limit,
    )
    logger.info(f"Relay знает {len(currencies)} токенов на chainId={chain_id}", step="DISCOVER")

    deny = {a.lower() for a in cfg.denylist}
    meta: dict[str, tuple[str, int]] = {}
    for c in currencies:
        addr = c.address.lower()
        # в списке Relay встречаются невалидные адреса ('0x' и т.п.) — отбрасываем
        if not Web3.is_address(addr) or addr in deny or addr == NATIVE_TOKEN:
            continue
        meta[addr] = (c.symbol, c.decimals)
    for extra in cfg.allowlist:
        addr = extra.lower()
        if Web3.is_address(addr) and addr not in meta and addr not in deny and addr != NATIVE_TOKEN:
            meta[addr] = ("?", 18)

    result: list[DiscoveredToken] = []

    # native ETH
    if cfg.include_native_eth:
        native_bal = w3.eth.get_balance(Web3.to_checksum_address(holder))
        if native_bal > 0:
            result.append(DiscoveredToken(NATIVE_TOKEN, "ETH", 18, native_bal, True))

    # ERC-20 батчем через Multicall3
    balances = batch_balance_of(w3, holder, list(meta.keys()), chunk=cfg.multicall_chunk)
    for addr, bal in balances.items():
        symbol, decimals = meta[addr]
        result.append(DiscoveredToken(addr, symbol, decimals, bal, True))

    logger.ok(
        f"обнаружено {len(result)} токен(ов) с балансом",
        wallet=holder,
        step="DISCOVER",
        tokens=",".join(t.symbol for t in result) or "-",
    )
    return result
