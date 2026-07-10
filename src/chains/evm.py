"""Базовый EVM-клиент: подпись EIP-1559 (fallback legacy), отправка, ожидание receipt."""
from __future__ import annotations

import time

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3

from src.core.errors import PermanentError, RetryableError, RpcError
from src import logger

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]


class EvmClient:
    """Один кошелёк + одна сеть. Приватный ключ живёт только в памяти."""

    priority_fee_wei: int | None = None  # None = eth_max_priority_fee; 0 — для zksync-стека

    def __init__(self, w3: Web3, private_key: str, chain_id: int, name: str):
        self.w3 = w3
        self.chain_id = chain_id
        self.name = name
        self.account: LocalAccount = Account.from_key(private_key)
        self.address = self.account.address

    # ---------- чтение ----------

    def native_balance(self, address: str | None = None) -> int:
        return self.w3.eth.get_balance(Web3.to_checksum_address(address or self.address))

    def erc20(self, token: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)

    def erc20_balance(self, token: str, holder: str | None = None) -> int:
        return self.erc20(token).functions.balanceOf(
            Web3.to_checksum_address(holder or self.address)
        ).call()

    def erc20_allowance(self, token: str, spender: str) -> int:
        return self.erc20(token).functions.allowance(
            self.address, Web3.to_checksum_address(spender)
        ).call()

    def gas_price(self) -> int:
        return self.w3.eth.gas_price

    # ---------- отправка ----------

    def build_tx(self, to: str, data: str | None, value: int, gas: int | None = None) -> dict:
        tx: dict = {
            "from": self.address,
            "to": Web3.to_checksum_address(to),
            "value": value,
            "chainId": self.chain_id,
            "nonce": self.w3.eth.get_transaction_count(self.address, "pending"),
        }
        if data:
            tx["data"] = data

        gas_price = self.gas_price()
        if self.priority_fee_wei is not None:
            priority = self.priority_fee_wei
        else:
            try:
                priority = self.w3.eth.max_priority_fee
            except Exception:  # noqa: BLE001
                priority = 0
        tx["maxFeePerGas"] = int(gas_price * 1.3) + priority
        tx["maxPriorityFeePerGas"] = min(priority, tx["maxFeePerGas"])
        tx["type"] = 2

        if gas:
            tx["gas"] = int(gas)
        else:
            try:
                tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.3)
            except Exception as e:  # noqa: BLE001
                text = str(e).lower()
                if "insufficient" in text:
                    raise PermanentError(f"estimate_gas: {e}") from e
                raise RetryableError(f"estimate_gas: {e}") from e
        return tx

    def sign_and_send(self, tx: dict) -> str:
        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        try:
            tx_hash = self.w3.eth.send_raw_transaction(raw)
        except ValueError as e:
            text = str(e).lower()
            if "nonce" in text or "known" in text or "underpriced" in text:
                raise RetryableError(f"send_raw: {e}") from e
            if "insufficient" in text:
                raise PermanentError(f"send_raw: {e}") from e
            raise self._maybe_legacy_fallback(tx, e)
        return tx_hash.hex() if not isinstance(tx_hash, str) else tx_hash

    def _maybe_legacy_fallback(self, tx: dict, original: Exception) -> Exception:
        """Некоторые ноды/сети не принимают type-2 — пробуем legacy на месте."""
        text = str(original).lower()
        if "type" in text or "eip-1559" in text or "not supported" in text:
            legacy = {k: v for k, v in tx.items() if k not in ("maxFeePerGas", "maxPriorityFeePerGas", "type")}
            legacy["gasPrice"] = self.gas_price()
            try:
                signed = self.account.sign_transaction(legacy)
                raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
                self.w3.eth.send_raw_transaction(raw)
                return RetryableError("legacy fallback отправлен, проверьте receipt")  # pragma: no cover
            except Exception as e2:  # noqa: BLE001
                return RetryableError(f"legacy fallback: {e2}")
        return RetryableError(f"send_raw: {original}")

    def send(self, to: str, data: str | None = None, value: int = 0, gas: int | None = None) -> str:
        tx = self.build_tx(to, data, value, gas)
        tx_hash = self.sign_and_send(tx)
        logger.info("tx отправлена", wallet=self.address, step=self.name.upper(), tx=_h(tx_hash))
        return tx_hash

    def wait_receipt(self, tx_hash: str, timeout: int = 300, confirmations: int = 1) -> dict:
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            raise RetryableError(f"receipt timeout {_h(tx_hash)}: {e}") from e
        if receipt["status"] != 1:
            raise PermanentError(f"tx reverted: {_h(tx_hash)}")
        if confirmations > 1:
            target = receipt["blockNumber"] + confirmations - 1
            deadline = time.time() + timeout
            while self.w3.eth.block_number < target:
                if time.time() > deadline:
                    raise RpcError("confirmation timeout")
                time.sleep(2)
        return dict(receipt)


def _h(tx_hash: str) -> str:
    h = tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"
    return f"{h[:10]}..{h[-6:]}"
