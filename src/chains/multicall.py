"""Multicall3-агрегатор: батч balanceOf/allowance одним RPC-вызовом (без эксплорер-API)."""
from __future__ import annotations

from eth_abi import decode as abi_decode
from web3 import Web3

from src.config import MULTICALL3_ADDRESS

MULTICALL3_ABI = [
    {
        "name": "tryAggregate",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "requireSuccess", "type": "bool"},
            {
                "name": "calls",
                "type": "tuple[]",
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "callData", "type": "bytes"},
                ],
            },
        ],
        "outputs": [
            {
                "name": "returnData",
                "type": "tuple[]",
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "returnData", "type": "bytes"},
                ],
            }
        ],
    }
]

_BALANCE_OF_SELECTOR = Web3.keccak(text="balanceOf(address)")[:4]


def batch_balance_of(w3: Web3, holder: str, tokens: list[str], chunk: int = 200) -> dict[str, int]:
    """balanceOf(holder) для списка токенов. Возвращает {token_lower: balance_wei>0}."""
    if not tokens:
        return {}
    mc = w3.eth.contract(address=Web3.to_checksum_address(MULTICALL3_ADDRESS), abi=MULTICALL3_ABI)
    holder_addr = Web3.to_checksum_address(holder)
    calldata = _BALANCE_OF_SELECTOR + abi_encode_address(holder_addr)

    result: dict[str, int] = {}
    for i in range(0, len(tokens), chunk):
        batch = tokens[i : i + chunk]
        calls = [(Web3.to_checksum_address(t), calldata) for t in batch]
        returned = mc.functions.tryAggregate(False, calls).call()
        for token, (success, data) in zip(batch, returned):
            if not success or len(data) < 32:
                continue
            try:
                bal = abi_decode(["uint256"], data)[0]
            except Exception:  # noqa: BLE001 — нестандартный ответ токена
                continue
            if bal > 0:
                result[token.lower()] = int(bal)
    return result


def abi_encode_address(addr: str) -> bytes:
    return bytes(12) + bytes.fromhex(addr[2:])
