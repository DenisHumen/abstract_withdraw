"""Pydantic-типы для Relay API (extra=allow: API может расширяться без поломки парсинга)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _Flexible(BaseModel):
    model_config = ConfigDict(extra="allow")


class Currency(_Flexible):
    chainId: int
    address: str
    symbol: str = "?"
    name: str = ""
    decimals: int = 18
    metadata: dict[str, Any] = {}

    @property
    def verified(self) -> bool:
        return bool(self.metadata.get("verified", False))

    @property
    def is_native(self) -> bool:
        return bool(self.metadata.get("isNative", False))


class StepItemData(_Flexible):
    """Данные транзакции для отправки on-chain (kind=transaction)."""

    from_: str | None = None
    to: str | None = None
    data: str | None = None
    value: str | None = None
    chainId: int | None = None
    gas: str | int | None = None
    maxFeePerGas: str | None = None
    maxPriorityFeePerGas: str | None = None

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def __init__(self, **kw):  # 'from' — зарезервированное слово
        if "from" in kw:
            kw["from_"] = kw.pop("from")
        super().__init__(**kw)


class StepItem(_Flexible):
    status: str = "incomplete"
    data: dict[str, Any] = {}
    check: dict[str, Any] | None = None

    @property
    def tx_data(self) -> StepItemData:
        return StepItemData(**self.data)


class Step(_Flexible):
    id: str = ""
    action: str = ""
    description: str = ""
    kind: str = "transaction"  # transaction | signature
    requestId: str | None = None
    items: list[StepItem] = []


class QuoteDetails(_Flexible):
    currencyIn: dict[str, Any] = {}
    currencyOut: dict[str, Any] = {}
    timeEstimate: int | None = None
    rate: str | None = None


class Quote(_Flexible):
    steps: list[Step] = []
    fees: dict[str, Any] = {}
    details: QuoteDetails = QuoteDetails()

    @property
    def request_id(self) -> str | None:
        for s in self.steps:
            if s.requestId:
                return s.requestId
        return None

    @property
    def amount_out(self) -> int:
        """Ожидаемый выход (wei) из квоты."""
        amt = self.details.currencyOut.get("amount")
        return int(amt) if amt else 0

    @property
    def amount_out_formatted(self) -> str:
        return str(self.details.currencyOut.get("amountFormatted", "?"))
