"""Клиент Abstract (chainId 2741, zkSync ZK-stack).

Основной путь — стандартные EIP-1559 транзакции от EOA (maxPriorityFeePerGas=0).
Fallback — EIP-712 type-113 через опциональный пакет zksync2 (pip install zksync2).
"""
from __future__ import annotations

from web3 import Web3

from src.chains.evm import EvmClient
from src.core.errors import RetryableError
from src import logger

ABSTRACT_CHAIN_ID = 2741


class AbstractClient(EvmClient):
    priority_fee_wei = 0  # zksync-стек: priority fee не используется

    def __init__(self, w3: Web3, private_key: str):
        super().__init__(w3, private_key, ABSTRACT_CHAIN_ID, "abstract")

    def send(self, to: str, data: str | None = None, value: int = 0, gas: int | None = None) -> str:
        try:
            return super().send(to, data, value, gas)
        except RetryableError as e:
            # Если стандартная tx не проходит именно из-за формата — пробуем type-113 (zksync2)
            if _looks_like_tx_type_issue(str(e)):
                logger.warn("EIP-1559 отклонена, пробуем zksync2 (type-113)", wallet=self.address)
                return self._send_zksync_712(to, data, value, gas)
            raise

    def _send_zksync_712(self, to: str, data: str | None, value: int, gas: int | None) -> str:
        try:
            from zksync2.core.types import EthBlockParams  # noqa: F401
            from zksync2.module.module_builder import ZkSyncBuilder
            from zksync2.signer.eth_signer import PrivateKeyEthSigner
            from zksync2.transaction.transaction_builders import TxFunctionCall
        except ImportError as ie:
            raise RetryableError(
                "нужен fallback zksync2: выполните `pip install zksync2` (см. requirements.txt)"
            ) from ie

        zk = ZkSyncBuilder.build(self.w3.provider.endpoint_uri)  # type: ignore[attr-defined]
        signer = PrivateKeyEthSigner(self.account, ABSTRACT_CHAIN_ID)
        nonce = zk.zksync.get_transaction_count(self.address, "pending")
        gas_price = zk.zksync.gas_price

        call = TxFunctionCall(
            chain_id=ABSTRACT_CHAIN_ID,
            nonce=nonce,
            from_=self.address,
            to=Web3.to_checksum_address(to),
            value=value,
            data=data or "0x",
            gas_limit=gas or 0,
            gas_price=gas_price,
            max_priority_fee_per_gas=0,
        )
        if not gas:
            estimate = zk.zksync.eth_estimate_gas(call.tx)
            call.tx["gas"] = int(estimate * 1.3)
        tx712 = call.tx712(call.tx["gas"])
        signed = signer.sign_typed_data(tx712.to_eip712_struct())
        raw = tx712.encode(signed)
        tx_hash = zk.zksync.send_raw_transaction(raw)
        return tx_hash.hex() if not isinstance(tx_hash, str) else tx_hash


def _looks_like_tx_type_issue(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in ("transaction type", "txtype", "eip-1559", "not supported", "invalid transaction"))
