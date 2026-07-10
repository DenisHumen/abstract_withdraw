"""Оркестратор: кошельки (параллельно) -> токены (последовательно) -> state machine.

Изоляция кошельков: падение одного не останавливает остальные (PLAN.md §8).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.chains.abstract import ABSTRACT_CHAIN_ID, AbstractClient
from src.chains.base import BaseClient
from src.chains.tokens import DiscoveredToken, discover_tokens
from src.config import AppConfig, NATIVE_TOKEN
from src.core import steps as st
from src.core.errors import (
    DustSkip,
    ManualError,
    NoRouteError,
    PermanentError,
    ProxyError,
    RetryableError,
)
from src.db.dao import Dao
from src.db.models import FAILED_STATUSES, TERMINAL_STATUSES, WAITING_TARGET, Wallet
from web3 import Web3
from src.net.proxy import ProxyPool, is_proxy_error
from src.net.rpc import RpcPool
from src.relay.client import RelayClient
from src import logger


class Pipeline:
    def __init__(self, cfg: AppConfig, dao: Dao, keys: dict[str, str]):
        self.cfg = cfg
        self.dao = dao
        self.keys = keys  # address -> private_key (только в памяти)
        self.proxy_pool = ProxyPool(cfg.proxy, cfg.resolve(cfg.proxy.pool_file), dao)
        self.abstract_rpc = RpcPool("abstract", cfg.env.rpc_pool("abstract"), cfg.rpc)
        self.base_rpc = RpcPool("base", cfg.env.rpc_pool("base"), cfg.rpc)

    # ------------------------------------------------ контекст кошелька

    def _build_ctx(self, wallet: Wallet, proxy: str | None) -> st.WalletCtx:
        pk = self.keys.get(wallet.address)
        if not pk:
            raise PermanentError(f"нет приватного ключа для {wallet.address} (проверьте XLSX)")
        w3_abs = self.abstract_rpc.healthy_web3(proxy, expected_chain_id=ABSTRACT_CHAIN_ID)
        w3_base = self.base_rpc.healthy_web3(proxy, expected_chain_id=self.cfg.routing.dest_chain_id)
        return st.WalletCtx(
            cfg=self.cfg,
            dao=self.dao,
            wallet=wallet,
            abstract=AbstractClient(w3_abs, pk),
            base=BaseClient(w3_base, pk),
            relay=RelayClient(proxy=proxy, retry_cfg=self.cfg.retry),
        )

    # ------------------------------------------------ обработка кошелька

    def process_wallet(self, wallet: Wallet, dry_run: bool = False) -> None:
        proxy = None
        try:
            proxy = self.proxy_pool.assign(wallet.id, wallet.proxy, wallet.proxy)
        except ProxyError as e:
            logger.error(f"прокси: {e}", wallet=wallet.address)
            if self.cfg.proxy.enabled and self.proxy_pool.size > 0:
                return
            # без пула работаем напрямую

        rotations = 0
        while True:
            try:
                ctx = self._build_ctx(wallet, proxy)
                self._run_wallet_jobs(ctx, dry_run)
                return
            except ProxyError as e:
                rotations += 1
                if rotations > self.cfg.proxy.max_rotations_per_job or self.proxy_pool.size == 0:
                    logger.error(f"прокси исчерпаны: {e}", wallet=wallet.address)
                    return
                try:
                    proxy = self.proxy_pool.rotate(wallet.id, proxy)
                except ProxyError as e2:
                    logger.error(str(e2), wallet=wallet.address)
                    return
            except ManualError as e:
                logger.warn(f"требуется ручное вмешательство: {e}", wallet=wallet.address)
                return
            except Exception as e:  # noqa: BLE001 — изоляция кошельков
                if is_proxy_error(e) and self.proxy_pool.size > 0 and rotations < self.cfg.proxy.max_rotations_per_job:
                    rotations += 1
                    proxy = self.proxy_pool.rotate(wallet.id, proxy)
                    continue
                logger.error(f"кошелёк остановлен: {e}", wallet=wallet.address)
                return

    def _run_wallet_jobs(self, ctx: st.WalletCtx, dry_run: bool) -> None:
        wallet = ctx.wallet

        # PRE-FLIGHT
        abs_balance = ctx.abstract.native_balance()
        base_balance = ctx.base.native_balance()
        logger.info(
            "pre-flight",
            wallet=wallet.address,
            step="PREFLIGHT",
            abs_eth=f"{abs_balance / 1e18:.6f}",
            base_eth=f"{base_balance / 1e18:.6f}",
        )

        # DISCOVER
        tokens = discover_tokens(
            ctx.abstract.w3, ctx.relay, wallet.address, self.cfg.routing.origin_chain_id, self.cfg.tokens
        )
        if abs_balance == 0 and not tokens:
            logger.warn(
                "на EOA Abstract пусто. Если средства в AGW-кошельке — нужен браузерный fallback (PLAN.md §7)",
                wallet=wallet.address,
            )
            return

        by_addr: dict[str, DiscoveredToken] = {t.address: t for t in tokens}
        for t in tokens:
            self.dao.upsert_balance(
                wallet.id, self.cfg.routing.origin_chain_id, t.address, t.symbol, t.decimals, t.balance, t.routable
            )
            self.dao.ensure_job(wallet.id, t.address, t.symbol)

        # ERC-20 сначала (их выход добавит ETH на Base), native ETH последним
        jobs = self.dao.get_jobs(wallet.id)
        jobs.sort(key=lambda j: (j.token_addr == NATIVE_TOKEN, j.id))

        # Нет адреса назначения -> держим задачи в очереди (WAITING_TARGET), ончейн-действий не делаем.
        # Как только target_address появится в XLSX (при следующем sync) — задачи продолжатся.
        has_target = bool(wallet.target_address) and Web3.is_address(wallet.target_address or "")
        if not has_target:
            waiting = 0
            for job in jobs:
                if job.status in TERMINAL_STATUSES or job.status in FAILED_STATUSES:
                    continue
                if job.status in ("BRIDGED", "TRANSFERRED"):
                    continue  # уже сбриджено на свой Base — ждём target для финального transfer
                if job.status != WAITING_TARGET:
                    self.dao.update_job(job.id, status=WAITING_TARGET, last_error="нет target_address")
                waiting += 1
            logger.warn(
                f"target_address не задан — {waiting} задач(и) в очереди (WAITING_TARGET), ончейн-действий нет",
                wallet=wallet.address, step="QUEUE",
            )
            return

        for job in jobs:
            if job.status in TERMINAL_STATUSES or job.status == "NEEDS_BROWSER":
                continue
            if job.status == WAITING_TARGET:
                # target появился — возвращаем задачу в работу
                self.dao.update_job(job.id, status="DISCOVERED", last_error=None)
            token = by_addr.get(job.token_addr)
            if token is None and job.status in ("PENDING", "DISCOVERED", WAITING_TARGET):
                continue  # баланса больше нет и работа не начата
            self._process_job(ctx, job.id, token, dry_run)

    # ------------------------------------------------ обработка джоба

    def _process_job(self, ctx: st.WalletCtx, job_id: int, token: DiscoveredToken | None, dry_run: bool) -> None:
        job = next((j for j in ctx.dao.get_jobs(ctx.wallet.id) if j.id == job_id), None)
        if job is None:
            return
        symbol = job.symbol or (token.symbol if token else job.token_addr[:10])
        attempts = 0

        while attempts < self.cfg.retry.max_attempts:
            attempts += 1
            try:
                job = ctx.dao.get_job(ctx.wallet.id, job.token_addr)  # refresh
                assert job is not None

                if job.status in ("PENDING", "DISCOVERED", "QUOTED", "APPROVED"):
                    if token is None:
                        raise PermanentError("баланс токена исчез до начала работы")
                    quote = st.make_quote(ctx, job, token)
                    if dry_run:
                        logger.ok(
                            f"[dry-run] {symbol}: квота ок, выход {quote.amount_out_formatted} ETH",
                            wallet=ctx.wallet.address, token=symbol, step="DRY",
                        )
                        return
                    base_baseline = ctx.base.native_balance()
                    job = ctx.dao.get_job(ctx.wallet.id, job.token_addr)
                    assert job is not None
                    st.execute_quote_steps(ctx, job, quote)
                    job = ctx.dao.get_job(ctx.wallet.id, job.token_addr)
                    assert job is not None
                    st.wait_bridge(ctx, job, base_baseline)
                elif job.status == "DEPOSITED":
                    st.wait_bridge(ctx, job, None)

                job = ctx.dao.get_job(ctx.wallet.id, job.token_addr)
                assert job is not None
                if job.status in ("BRIDGED", "TRANSFERRED") and not dry_run:
                    st.forward_transfer(ctx, job)
                return

            except DustSkip as e:
                ctx.dao.update_job(job.id, status="SKIPPED", last_error=str(e))
                logger.skip(f"{symbol}: {e}", wallet=ctx.wallet.address, token=symbol)
                return
            except NoRouteError as e:
                # нет маршрута (в т.ч. скам-токены с фейковым balanceOf) — SKIPPED, не FAILED
                ctx.dao.update_job(job.id, status="SKIPPED", last_error=f"no route: {str(e)[:200]}")
                logger.skip(f"{symbol}: маршрута нет — пропуск", wallet=ctx.wallet.address, token=symbol)
                return
            except PermanentError as e:
                ctx.dao.update_job(job.id, status="FAILED", last_error=str(e)[:500], error_class="permanent")
                logger.error(f"{symbol}: {e}", wallet=ctx.wallet.address, token=symbol)
                return
            except ProxyError:
                raise  # ротацию делает process_wallet
            except RetryableError as e:
                ctx.dao.bump_attempts(job.id)
                if is_proxy_error(e):
                    raise ProxyError(str(e)) from e
                if attempts >= self.cfg.retry.max_attempts:
                    ctx.dao.update_job(job.id, status="FAILED", last_error=str(e)[:500], error_class="retryable")
                    logger.error(f"{symbol}: попытки исчерпаны: {e}", wallet=ctx.wallet.address, token=symbol)
                    return
                backoff = min(
                    self.cfg.retry.backoff_base_sec * (2 ** (attempts - 1)),
                    self.cfg.retry.backoff_max_sec,
                )
                logger.warn(
                    f"{symbol}: ретрай {attempts}/{self.cfg.retry.max_attempts} через {backoff:.0f}s: {e}",
                    wallet=ctx.wallet.address, token=symbol,
                )
                time.sleep(backoff)

    # ------------------------------------------------ запуск

    def run(self, only_wallet: str | None = None, dry_run: bool = False) -> None:
        wallets = self.dao.get_wallets(enabled_only=True)
        if only_wallet:
            wallets = [w for w in wallets if w.address.lower() == only_wallet.lower()]
        wallets = [w for w in wallets if w.address in self.keys]
        if not wallets:
            logger.warn("нет кошельков для обработки (проверьте XLSX/sync)")
            return

        logger.info(f"запуск: {len(wallets)} кошельков, concurrency={self.cfg.execution.concurrency}, dry_run={dry_run}")
        if self.cfg.execution.concurrency <= 1 or len(wallets) == 1:
            for w in wallets:
                self.process_wallet(w, dry_run)
                time.sleep(self.cfg.execution.random_delay())
        else:
            with ThreadPoolExecutor(max_workers=self.cfg.execution.concurrency) as pool:
                futures = {}
                for w in wallets:
                    futures[pool.submit(self.process_wallet, w, dry_run)] = w
                    time.sleep(self.cfg.execution.random_delay())
                for fut in as_completed(futures):
                    w = futures[fut]
                    exc = fut.exception()
                    if exc:
                        logger.error(f"необработанная ошибка: {exc}", wallet=w.address)
        logger.ok("прогон завершён")
