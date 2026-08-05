"""CLI: sync / discover / run / status / retry / init-data / check-protocols / report-protocols.

Запуск без команды — интерактивное меню.
Примеры:
  python -m src.main init-data          # создать шаблоны data/wallets.xlsx и data/proxies.txt
  python -m src.main sync               # XLSX -> SQLite
  python -m src.main run --dry-run      # квоты без отправки транзакций
  python -m src.main run                # боевой прогон
  python -m src.main status             # таблица прогресса (мост + чекер)
  python -m src.main retry              # перезапуск FAILED-джобов
  python -m src.main check-protocols    # чекер протоколов (вход -> AGW -> DeBank -> Excel)
  python -m src.main report-protocols   # перегенерировать Excel-отчёт из БД
"""
from __future__ import annotations

from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from src import logger
from src.config import AppConfig, load_config
from src.core.pipeline import Pipeline
from src.core.protocol_check import ProtocolChecker
from src.data_io import excel
from src.data_io.protocol_report import export_protocol_report
from src.db.dao import Dao

app = typer.Typer(add_completion=False, help="Abstract -> Base (Relay) -> target: кроссчейн-вывод")

REPORT_PROTOCOLS = "reports/protocols_report.xlsx"


def _boot(config_path: str | None = None) -> tuple[AppConfig, Dao]:
    cfg = load_config(config_path)
    logger.setup_file_log(cfg.resolve(cfg.paths.logs_dir))
    dao = Dao(cfg.resolve(cfg.paths.db))
    return cfg, dao


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context):
    """Без команды -> интерактивное меню."""
    if ctx.invoked_subcommand is None:
        interactive_menu()


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
    _print_status(dao)


def _print_status(dao: Dao) -> None:
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
    _print_check_tasks_brief(dao)


def _print_check_tasks_brief(dao: Dao) -> None:
    """Компактная сводка задач чекера протоколов (если чекер запускался)."""
    rows = [r for r in dao.check_tasks_summary() if r["get_agw"] or r["check_protocols"]]
    if not rows:
        return
    agw_done = sum(1 for r in rows if r["get_agw"] == "DONE")
    proto_done = sum(1 for r in rows if r["check_protocols"] == "DONE")
    failed = sum(1 for r in rows if "FAILED" in (r["get_agw"], r["check_protocols"]))
    logger.console.print(
        f"  [bold]Чекер протоколов[/bold]: кошельков {len(rows)}  •  "
        f"AGW получен: [green]{agw_done}[/green]  •  проверено на DeBank: [green]{proto_done}[/green]  •  "
        f"с ошибками: [{'red' if failed else 'dim'}]{failed}[/]"
    )


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


@app.command("bridge-agw")
def bridge_agw(
    wallet: str = typer.Option(None, help="только этот адрес"),
    dry_run: bool = typer.Option(False, "--dry-run", help="показать план без браузера и транзакций"),
    config: str = typer.Option(None),
):
    """Браузерный мост: native ETH AGW(Abstract) -> Base (свой EOA из ключа) -> биржевой адрес из XLSX.

    Если биржевой адрес (target_address) не заполнен — после моста скрипт не падает, а ждёт:
    перечитывает XLSX и отправляет, как только адрес появится.
    """
    from src.core.agw_bridge import AgwBridge

    cfg, dao = _boot(config)
    keys = excel.sync_to_db(cfg.resolve(cfg.paths.wallets_xlsx), dao)
    AgwBridge(cfg, dao, keys).run(only_wallet=wallet, dry_run=dry_run or cfg.mode.dry_run)


@app.command("check-protocols")
def check_protocols(
    wallet: str = typer.Option(None, help="только этот адрес"),
    threads: int = typer.Option(None, "--threads", "-t", help="потоков DeBank-проверок (по умолчанию из config)"),
    login_threads: int = typer.Option(None, "--login-threads", "-l",
                                      help="потоков входа на relay.link (headful, тяжёлые; по умолчанию из config)"),
    headless: bool = typer.Option(True, help="скрытый браузер для DeBank (вход всегда headful)"),
    report: bool = typer.Option(True, help="сохранить Excel-отчёт по завершении"),
    config: str = typer.Option(None),
):
    """Проверить, какие протоколы использует каждый кошелёк (relay.link login -> DeBank). Многопоточно."""
    cfg, dao = _boot(config)
    keys = excel.sync_to_db(cfg.resolve(cfg.paths.wallets_xlsx), dao)
    ProtocolChecker(cfg, dao, keys).run(only_wallet=wallet, headless=headless,
                                        threads=threads, login_threads=login_threads)
    if report:
        export_protocol_report(dao, cfg.resolve(REPORT_PROTOCOLS))


@app.command("report-protocols")
def report_protocols(config: str = typer.Option(None)):
    """Выгрузить отчёт по протоколам из БД в Excel (без повторной проверки)."""
    cfg, dao = _boot(config)
    path = export_protocol_report(dao, cfg.resolve(REPORT_PROTOCOLS))
    logger.console.print(f"[green]OK[/green] {path}")


_MENU = [
    ("1", "Синхронизация XLSX -> БД", "sync"),
    ("2", "Проверка протоколов (DeBank)", "check"),
    ("3", "Мост через браузер: ETH AGW -> Base -> биржа", "bridge_agw"),
    ("4", "Мост Abstract -> Base (EOA-путь, программный)", "run"),
    ("5", "Статус задач", "status"),
    ("6", "Отчёт по протоколам -> Excel", "report"),
    ("7", "Повтор упавших задач", "retry"),
    ("0", "Выход", "quit"),
]


def interactive_menu(config: str | None = None) -> None:
    cfg, dao = _boot(config)
    while True:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="bold cyan", justify="right")
        table.add_column()
        for key, label, _ in _MENU:
            table.add_row(key, label)
        logger.console.print(Panel(table, title="[bold]Abstract Withdraw — меню[/bold]",
                                   border_style="magenta", subtitle="выберите режим"))
        try:
            choice = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            logger.console.print("\n[dim]пока![/dim]")
            return
        if not choice:
            continue
        action = next((a for k, _, a in _MENU if k == choice), None)
        if action == "quit":
            logger.console.print("[dim]пока![/dim]")
            return
        try:
            _dispatch(action, cfg, dao)
        except Exception as e:  # noqa: BLE001 — меню не должно падать
            logger.error(f"ошибка режима: {e}")
        logger.console.print()


def _dispatch(action: str | None, cfg: AppConfig, dao: Dao) -> None:
    if action == "sync":
        excel.sync_to_db(cfg.resolve(cfg.paths.wallets_xlsx), dao)
    elif action == "check":
        keys = excel.sync_to_db(cfg.resolve(cfg.paths.wallets_xlsx), dao)
        default_t = cfg.execution.check_concurrency
        default_l = cfg.execution.login_concurrency
        raw = input(f"  потоков DeBank [{default_t}]: ").strip()
        threads = int(raw) if raw.isdigit() and int(raw) > 0 else default_t
        raw = input(f"  потоков входа (headful, тяжёлые) [{default_l}]: ").strip()
        login_threads = int(raw) if raw.isdigit() and int(raw) > 0 else default_l
        ProtocolChecker(cfg, dao, keys).run(threads=threads, login_threads=login_threads)
        export_protocol_report(dao, cfg.resolve(REPORT_PROTOCOLS))
    elif action == "bridge_agw":
        from src.core.agw_bridge import AgwBridge

        keys = excel.sync_to_db(cfg.resolve(cfg.paths.wallets_xlsx), dao)
        AgwBridge(cfg, dao, keys).run(dry_run=cfg.mode.dry_run)
    elif action == "run":
        keys = excel.sync_to_db(cfg.resolve(cfg.paths.wallets_xlsx), dao)
        Pipeline(cfg, dao, keys).run(dry_run=cfg.mode.dry_run)
    elif action == "status":
        _print_status(dao)
    elif action == "report":
        export_protocol_report(dao, cfg.resolve(REPORT_PROTOCOLS))
    elif action == "retry":
        n = dao.reset_failed_jobs()
        logger.ok(f"сброшено {n} FAILED-джобов")
        if n:
            keys = excel.sync_to_db(cfg.resolve(cfg.paths.wallets_xlsx), dao)
            Pipeline(cfg, dao, keys).run()
    else:
        logger.warn("неизвестный пункт меню")


if __name__ == "__main__":
    app()
