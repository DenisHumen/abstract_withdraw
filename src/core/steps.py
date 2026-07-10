"""Шаги обработки одного джоба (wallet, token): QUOTE -> APPROVE -> DEPOSIT -> BRIDGE -> TRANSFER.

Идемпотентность: каждый шаг сначала проверяет БД/ончейн-состояние и не повторяет сделанное.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from eth_account.messages import encode_defunct, encode_typed_data

from src.chains.abstract import AbstractClient
from src.chains.base import BaseClient
from src.chains.tokens import DiscoveredToken
from src.config import AppConfig, NATIVE_TOKEN
from src.core.errors import DustSkip, PermanentError, RetryableError
from src.db.dao import Dao
from src.db.models import Job, Wallet
from src.relay.client import RelayClient
from src.relay.types import Quote, Step
from src import logger

DEPOSIT_GAS_HINT = 600_000  # верхняя оценка газа deposit на Abstract для расчёта резерва


@dataclass
class WalletCtx:
    cfg: AppConfig
    dao: Dao
    wallet: Wallet
    abstract: AbstractClient
    base: BaseClient
    relay: RelayClient


# ---------------------------------------------------------------- QUOTE

def make_quote(ctx: WalletCtx, job: Job, token: DiscoveredToken) -> Quote:
    cfg = ctx.cfg
    amount = token.balance
    if token.address == NATIVE_TOKEN:
        reserve = max(
            cfg.amounts.abstract_floor,
            int(ctx.abstract.gas_price() * DEPOSIT_GAS_HINT * cfg.amounts.gas_estimate_multiplier),
        )
        amount = token.balance - reserve
        if amount <= 0:
            raise DustSkip(f"native баланс {token.balance} wei меньше резерва газа {reserve}")

    quote = ctx.relay.quote(
        user=ctx.wallet.address,
        recipient=ctx.wallet.address,  # two_step: мост на СВОЙ Base-адрес
        origin_chain_id=cfg.routing.origin_chain_id,
        dest_chain_id=cfg.routing.dest_chain_id,
        origin_currency=token.address,
        dest_currency=cfg.routing.dest_currency,
        amount=amount,
        trade_type=cfg.routing.trade_type,
        slippage_bps=cfg.routing.slippage_bps,
    )

    out = quote.amount_out
    if out < cfg.amounts.min_out:
        raise DustSkip(f"выход {out} wei < min_native_out_wei {cfg.amounts.min_out}")
    if cfg.amounts.skip_if_out_lte_forward_gas and out <= ctx.base.forward_gas_cost(
        cfg.amounts.gas_estimate_multiplier
    ):
        raise DustSkip(f"выход {out} wei не покрывает газ финального transfer")

    ctx.dao.update_job(
        job.id,
        status="QUOTED",
        amount_in=str(amount),
        amount_out=str(out),
        request_id=quote.request_id,
    )
    logger.info(
        "квота получена",
        wallet=ctx.wallet.address,
        token=token.symbol,
        step="QUOTE",
        amount_in=amount,
        out_eth=quote.amount_out_formatted,
        req=quote.request_id,
    )
    return quote


# ---------------------------------------------------------------- EXECUTE STEPS

def execute_quote_steps(ctx: WalletCtx, job: Job, quote: Quote) -> None:
    """Исполнить steps[] квоты по порядку (approve -> deposit | signature)."""
    for step in quote.steps:
        for item in step.items:
            if item.status == "complete":
                continue
            if step.kind == "transaction":
                _execute_tx_item(ctx, job, step, item)
            elif step.kind == "signature":
                _execute_signature_item(ctx, job, step, item)
            else:
                raise PermanentError(f"неизвестный kind шага: {step.kind}")

    new_status = "DEPOSITED"
    ctx.dao.update_job(job.id, status=new_status)


def _step_name(step: Step) -> str:
    sid = (step.id or step.action or "step").lower()
    if "approve" in sid or "authorize" in sid:
        return "approve"
    if "deposit" in sid or "swap" in sid or "send" in sid:
        return "deposit"
    return sid[:20]


def _execute_tx_item(ctx: WalletCtx, job: Job, step: Step, item) -> None:
    name = _step_name(step)
    prev = ctx.dao.get_tx(job.id, name)
    if prev is not None and prev["status"] == "confirmed":
        logger.skip(f"{name}: уже подтверждён ранее", wallet=ctx.wallet.address, step=name.upper())
        return
    if prev is not None and prev["status"] == "sent" and prev["tx_hash"]:
        # повторный запуск: дожидаемся судьбы отправленной tx, не дублируем
        logger.info(f"{name}: найдена отправленная tx, ждём receipt", wallet=ctx.wallet.address, step=name.upper())
        receipt = ctx.abstract.wait_receipt(prev["tx_hash"], timeout=300)
        ctx.dao.log_tx(job.id, name, ctx.abstract.chain_id, prev["tx_hash"], prev["nonce"],
                       status="confirmed", gas_used=receipt.get("gasUsed"))
        return

    tx = item.tx_data
    if not tx.to:
        raise PermanentError(f"{name}: в item.data нет 'to'")
    value = int(tx.value or 0)
    gas = int(tx.gas) if tx.gas else None

    tx_hash = ctx.abstract.send(to=tx.to, data=tx.data, value=value, gas=gas)
    ctx.dao.log_tx(
        job.id, name, ctx.abstract.chain_id, tx_hash, None,
        status="sent", raw_request=json.dumps(item.data)[:2000],
    )
    receipt = ctx.abstract.wait_receipt(tx_hash, timeout=300, confirmations=ctx.cfg.execution.tx_confirmations)
    ctx.dao.log_tx(job.id, name, ctx.abstract.chain_id, tx_hash, None,
                   status="confirmed", gas_used=receipt.get("gasUsed"))
    logger.ok(f"{name} подтверждён", wallet=ctx.wallet.address, token=job.symbol, step=name.upper(), tx=tx_hash[:12])

    if name == "approve":
        ctx.dao.update_job(job.id, status="APPROVED")


def _execute_signature_item(ctx: WalletCtx, job: Job, step: Step, item) -> None:
    """kind=signature: подписать EIP-191/EIP-712 и отправить на post.endpoint."""
    data = item.data
    sign_spec = data.get("sign", {})
    post_spec = data.get("post", {})
    kind = (sign_spec.get("signatureKind") or "eip712").lower()

    if kind == "eip191":
        message = sign_spec.get("message", "")
        signable = encode_defunct(hexstr=message) if message.startswith("0x") else encode_defunct(text=message)
        signed = ctx.abstract.account.sign_message(signable)
    else:
        typed = {
            "domain": sign_spec.get("domain", {}),
            "types": sign_spec.get("types", {}),
            "primaryType": sign_spec.get("primaryType"),
            "message": sign_spec.get("value") or sign_spec.get("message") or {},
        }
        signed = ctx.abstract.account.sign_message(encode_typed_data(full_message=typed))

    signature = signed.signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature

    endpoint = post_spec.get("endpoint")
    if not endpoint:
        raise PermanentError("signature-шаг без post.endpoint")
    ctx.relay.post_signature(
        endpoint, signature, body=post_spec.get("body"), method=post_spec.get("method", "POST")
    )
    logger.ok("подпись принята Relay", wallet=ctx.wallet.address, token=job.symbol, step="SIGNATURE")


# ---------------------------------------------------------------- BRIDGE WAIT

def wait_bridge(ctx: WalletCtx, job: Job, base_baseline: int | None) -> int:
    """Ждём исполнения бриджа. Источник истины — публичный RPC Base; Relay-статус — вспомогательный."""
    cfg = ctx.cfg.execution
    expected_out = int(job.amount_out or 0)
    deadline = time.time() + cfg.status_timeout_sec
    last_status = ""

    while time.time() < deadline:
        # 1) Relay intents-статус (дешёвый запрос, даёт причину failure/refund)
        if job.request_id:
            try:
                st = ctx.relay.get_status(job.request_id)
                last_status = str(st.get("status", ""))
                if last_status == "success":
                    ctx.dao.update_job(job.id, status="BRIDGED")
                    logger.ok(
                        "bridge исполнен (Relay: success)",
                        wallet=ctx.wallet.address, token=job.symbol, step="BRIDGE",
                        dst_tx=str(st.get("txHashes") or st.get("destinationChainTxHash") or "?")[:80],
                    )
                    return expected_out
                if last_status in ("failure", "refund"):
                    new = "REFUNDED" if last_status == "refund" else "FAILED"
                    ctx.dao.update_job(job.id, status=new, last_error=json.dumps(st)[:500], error_class="permanent")
                    raise PermanentError(f"Relay status={last_status}: {json.dumps(st)[:300]}")
            except (PermanentError, DustSkip):
                raise
            except Exception as e:  # noqa: BLE001 — статус-сервис недоступен, но RPC решает
                logger.warn(f"status v3 недоступен: {e}", wallet=ctx.wallet.address, step="BRIDGE")

        # 2) Публичный RPC Base: факт зачисления
        if base_baseline is not None and expected_out > 0:
            bal = ctx.base.native_balance()
            min_expected = int(expected_out * 0.9)  # допуск на комиссию/скольжение
            if bal >= base_baseline + min_expected:
                ctx.dao.update_job(job.id, status="BRIDGED", amount_out=str(bal - base_baseline))
                logger.ok(
                    "bridge исполнен (RPC: баланс вырос)",
                    wallet=ctx.wallet.address, token=job.symbol, step="BRIDGE",
                    received_wei=bal - base_baseline,
                )
                return bal - base_baseline

        time.sleep(cfg.status_poll_interval_sec)

    raise RetryableError(f"bridge timeout ({cfg.status_timeout_sec}s), последний статус Relay: {last_status or '?'}")


# ---------------------------------------------------------------- FORWARD

def forward_transfer(ctx: WalletCtx, job: Job) -> None:
    """Финальный native transfer ETH: свой Base-адрес -> target_address (весь баланс - газ)."""
    prev = ctx.dao.get_tx(job.id, "transfer")
    if prev is not None and prev["status"] == "confirmed":
        ctx.dao.update_job(job.id, status="DONE")
        logger.skip("transfer уже выполнен ранее", wallet=ctx.wallet.address, step="TRANSFER")
        return
    if prev is not None and prev["status"] == "sent" and prev["tx_hash"]:
        receipt = ctx.base.wait_receipt(prev["tx_hash"], timeout=300)
        ctx.dao.log_tx(job.id, "transfer", ctx.base.chain_id, prev["tx_hash"], prev["nonce"],
                       status="confirmed", gas_used=receipt.get("gasUsed"))
        ctx.dao.update_job(job.id, status="DONE")
        return

    balance = ctx.base.native_balance()
    reserve = max(
        ctx.cfg.amounts.base_floor,
        ctx.base.forward_gas_cost(ctx.cfg.amounts.gas_estimate_multiplier),
    )
    amount = balance - reserve
    if amount <= 0:
        raise RetryableError(f"на Base нечего пересылать: balance={balance}, резерв={reserve}")

    tx_hash = ctx.base.transfer_native(ctx.wallet.target_address, amount)
    ctx.dao.update_job(job.id, status="TRANSFERRED")
    ctx.dao.log_tx(job.id, "transfer", ctx.base.chain_id, tx_hash, None, status="sent")
    receipt = ctx.base.wait_receipt(tx_hash, timeout=300, confirmations=ctx.cfg.execution.tx_confirmations)
    ctx.dao.log_tx(job.id, "transfer", ctx.base.chain_id, tx_hash, None,
                   status="confirmed", gas_used=receipt.get("gasUsed"))
    ctx.dao.update_job(job.id, status="DONE")
    logger.ok(
        f"переслано {amount / 1e18:.6f} ETH -> {ctx.wallet.target_address}",
        wallet=ctx.wallet.address, token=job.symbol, step="TRANSFER", tx=tx_hash[:12],
    )
