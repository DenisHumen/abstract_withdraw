"""CLI: sync / discover / run / status / retry / init-data.

Примеры:
  python -m src.main init-data          # создать шаблоны data/wallets.xlsx и data/proxies.txt
  python -m src.main sync               # XLSX -> SQLite
  python -m src.main run --dry-run      # квоты без отправки транзакций
  python -m src.main run                # боевой прогон
  python -m src.main status             # таблица прогресса
  python -m src.main retry              # перезапуск FAILED-джобов
"""
from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from src import logger
from src.config import AppConfig, load_config
from src.core.pipeline import Pipeline
from src.data_io import excel
from src.db.dao import Dao

app = typer.Typer(add_completion=False, help="Abstract -> Base (Relay) -> target: кроссчейн-вывод")


def _boot(config_path: str | None = None) -> tuple[AppConfig, Dao]:
    cfg = load_config(config_path)
    logger.setup_file_log(cfg.resolve(cfg.paths.logs_dir))
    dao = Dao(cfg.resolve(cfg.paths.db))
    return cfg, dao


@app.command("init-data")
def init_data():
    """Создать шаблоны data/wallets.xlsx и data/proxies.txt."""
    cfg = load_config()
    xlsx = cfg.resolve(cfg.paths.wallets_xlsx)
    if xlsx.exists():
        logger.warn(f"{xlsx} уже существует — не трогаю")
    else:
        excel.create_template(xlsx)
        logger.ok(f"создан шаблон {xlsx}")

    pool = cfg.resolve(cfg.proxy.pool_file)
    if pool.exists():
        logger.warn(f"{pool} уже существует — не трогаю")
    else:
        pool.parent.mkdir(parents=True, exist_ok=True)
        pool.write_text(
            "# Пул HTTP-прокси для ротации: по одной на строку, формат login:passwd@ip:port\n"
            "# Пример:\n"
            "# user123:pass456@1.2.3.4:8080\n",
            encoding="utf-8",
        )
        logger.ok(f"создан шаблон {pool}")

    env = Path(cfg.resolve(".env"))
    if not env.exists():
        example = cfg.resolve(".env.example")
        env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        logger.ok("создан .env из .env.example")


@app.command()
def sync(config: str = typer.Option(None, help="путь к config.yaml")):
    """Синхронизировать data/wallets.xlsx -> SQLite (ключи в БД не пишутся)."""
    cfg, dao = _boot(config)
    excel.sync_to_db(cfg.resolve(cfg.paths.wallets_xlsx), dao)


@app.command()
def discover(
    wallet: str = typer.Option(None, help="только этот адрес"),
    config: str = typer.Option(None),
):
    """Только discovery токенов (без транзакций)."""
    cfg, dao = _boot(config)
    keys = excel.sync_to_db(cfg.resolve(cfg.paths.wallets_xlsx), dao)
    pipe = Pipeline(cfg, dao, keys)
    wallets = dao.get_wallets()
    if wallet:
        wallets = [w for w in wallets if w.address.lower() == wallet.lower()]
    for w in wallets:
        try:
            proxy = pipe.proxy_pool.assign(w.id, w.proxy, w.proxy) if cfg.proxy.enabled else None
            ctx = pipe._build_ctx(w, proxy)  # noqa: SLF001
            from src.chains.tokens import discover_tokens

            tokens = discover_tokens(ctx.abstract.w3, ctx.relay, w.address, cfg.routing.origin_chain_id, cfg.tokens)
            for t in tokens:
                dao.upsert_balance(w.id, cfg.routing.origin_chain_id, t.address, t.symbol, t.decimals, t.balance, t.routable)
        except Exception as e:  # noqa: BLE001
            logger.error(f"discover: {e}", wallet=w.address)


@app.command()
def run(
    wallet: str = typer.Option(None, help="только этот адрес"),
    dry_run: bool = typer.Option(False, "--dry-run", help="квоты без отправки транзакций"),
    config: str = typer.Option(None),
):
    """Основной пайплайн: DISCOVER -> QUOTE -> APPROVE -> DEPOSIT -> BRIDGE -> TRANSFER."""
    cfg, dao = _boot(config)
    keys = excel.sync_to_db(cfg.resolve(cfg.paths.wallets_xlsx), dao)
    effective_dry = dry_run or cfg.mode.dry_run
    Pipeline(cfg, dao, keys).run(only_wallet=wallet, dry_run=effective_dry)


@app.command()
def status(config: str = typer.Option(None)):
    """Таблица прогресса из SQLite."""
    _, dao = _boot(config)
    rows = dao.status_summary()
    table = Table(title="Прогресс джобов", show_lines=False)
    for col in ("Кошелёк", "Метка", "Токен", "Статус", "In (wei)", "Out (wei)", "Ошибка"):
        table.add_column(col, overflow="fold")
    style = {
        "DONE": "green", "BRIDGED": "cyan", "TRANSFERRED": "cyan",
        "FAILED": "red", "REFUNDED": "red", "NEEDS_BROWSER": "yellow",
        "WAITING_TARGET": "yellow", "SKIPPED": "dim",
    }
    for r in rows:
        table.add_row(
            logger.short_addr(r["address"]),
            r["label"] or "",
            r["symbol"] or r["token_addr"][:10],
            f"[{style.get(r['status'], 'white')}]{r['status']}[/]",
            r["amount_in"] or "",
            r["amount_out"] or "",
            (r["last_error"] or "")[:60],
        )
    logger.console.print(table)
    if not rows:
        logger.info("джобов пока нет: выполните sync и run")


@app.command()
def retry(
    wallet: str = typer.Option(None, help="только этот адрес"),
    config: str = typer.Option(None),
):
    """Сбросить FAILED-джобы и запустить прогон заново."""
    cfg, dao = _boot(config)
    wallet_id = None
    if wallet:
        w = dao.get_wallet(wallet)
        if not w:
            logger.error(f"кошелёк {wallet} не найден в БД")
            raise typer.Exit(1)
        wallet_id = w.id
    n = dao.reset_failed_jobs(wallet_id)
    logger.ok(f"сброшено {n} FAILED-джобов")
    if n:
        keys = excel.sync_to_db(cfg.resolve(cfg.paths.wallets_xlsx), dao)
        Pipeline(cfg, dao, keys).run(only_wallet=wallet)


if __name__ == "__main__":
    app()
