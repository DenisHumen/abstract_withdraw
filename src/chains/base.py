"""Клиент Base (chainId 8453): финальный native transfer + watch-баланс recipient."""
from __future__ import annotations

import time

from web3 import Web3

from src.chains.evm import EvmClient
from src.core.errors import RetryableError
from src import logger

BASE_CHAIN_ID = 8453
NATIVE_TRANSFER_GAS = 21_000


class BaseClient(EvmClient):
    def __init__(self, w3: Web3, private_key: str):
        super().__init__(w3, private_key, BASE_CHAIN_ID, "base")

    def forward_gas_cost(self, multiplier: float = 1.5) -> int:
        """Стоимость финального transfer (для резерва и порога пыли)."""
        return int(self.gas_price() * NATIVE_TRANSFER_GAS * multiplier)

    def transfer_native(self, to: str, amount_wei: int) -> str:
        return self.send(to=to, data=None, value=amount_wei, gas=NATIVE_TRANSFER_GAS)

    def watch_balance_increase(
        self,
        baseline_wei: int,
        min_increase_wei: int,
        timeout_sec: int,
        poll_interval_sec: float = 2,
        address: str | None = None,
    ) -> int:
        """Источник истины прихода средств — публичный RPC (eth_getBalance).

        Ждём, пока баланс address вырастет от baseline минимум на min_increase.
        Возвращает новый баланс.
        """
        target = Web3.to_checksum_address(address or self.address)
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            bal = self.native_balance(target)
            if bal >= baseline_wei + min_increase_wei:
                logger.ok(
                    "зачисление на Base подтверждено",
                    wallet=target,
                    step="BRIDGE",
                    delta_wei=bal - baseline_wei,
                )
                return bal
            time.sleep(poll_interval_sec)
        raise RetryableError(
            f"таймаут ожидания зачисления на Base (ждали +{min_increase_wei} wei за {timeout_sec}s)"
        )
