# CLAUDE.md — контекст проекта для ИИ-агентов

## Что это

Скрипт вывода активов: Abstract (2741) → relay.link → native ETH на Base (8453) → transfer на target-адрес.
Python 3.11+/3.12, стек: web3.py v7, httpx, openpyxl, sqlite3, typer+rich, tenacity.
Полная архитектура: [PLAN.md](PLAN.md). Пользовательская дока: [README.md](README.md).

## Жёсткие требования пользователя (НЕ нарушать)

1. **Никаких платных/ключевых API** (Etherscan, abscan, Relay API-key и т.п.). Только:
   публичный REST `https://api.relay.link`, публичные RPC, ончейн-вызовы (Multicall3).
   Единственный допустимый ключ — `ADSPOWER_API_KEY` (браузерный fallback).
2. **SQLite (`data/state.db`) — single source of truth**; XLSX только вход.
3. **Идемпотентность**: никакой шаг не должен выполняться повторно (см. `tx_log`, `request_id`).
4. **Прокси на кошелёк** формата `login:passwd@ip:port`; при сбое — ротация на случайную из
   `data/proxies.txt`.
5. Приватные ключи — только в памяти, в БД/логи не писать. Прокси-креды в логах маскировать.
6. Держать PLAN.md/README/CLAUDE.md актуальными при изменениях.
7. Отчётность по задачам вести в `reports/worklog.xlsx`
   (колонки: Дата, День недели, Направление, Задача, Часы (оц.)) — дополнять при новых работах.

## Карта кода

```
src/main.py            CLI: init-data | sync | discover | run [--dry-run] | status | retry
src/config.py          .env (EnvSettings) + config.yaml (AppConfig); константы NATIVE_TOKEN, MULTICALL3
src/logger.py          rich + JSONL (logs/run-YYYYMMDD.jsonl)
src/db/                schema.sql, dao.py (thread-local sqlite, upsert-ы), models.py (статусы)
src/data_io/excel.py   чтение/шаблон wallets.xlsx, sync в БД (возвращает {addr: pk} в память)
src/net/proxy.py       ProxyPool: назначение sticky, health-check, ротация, маскирование
src/net/rpc.py         RpcPool: пул публичных нод, ротация при сбое, web3 через прокси
src/relay/client.py    quote/v2, intents/status/v3, currencies/v1, chains; post_signature
src/relay/types.py     pydantic-модели (extra=allow)
src/chains/evm.py      базовый клиент: EIP-1559 подпись/отправка/receipt, ERC20 ABI
src/chains/abstract.py AbstractClient (prio fee=0; fallback zksync2 type-113 — опц. пакет)
src/chains/base.py     BaseClient: transfer_native, watch_balance_increase (источник истины bridge)
src/chains/multicall.py batch balanceOf через Multicall3 (0xcA11...CA11)
src/chains/tokens.py   discovery: Relay currencies ⋂ Multicall3 (фильтр невалидных адресов!)
src/core/steps.py      QUOTE/APPROVE/DEPOSIT/BRIDGE/TRANSFER (идемпотентные)
src/core/pipeline.py   оркестратор: кошельки параллельно, изоляция ошибок, ротация прокси
src/core/errors.py     Retryable/Proxy/Rpc/Permanent/NoRoute/DustSkip/Manual
src/browser/           ЗАГЛУШКА fallback-ветки AdsPower+Playwright (PLAN.md §7)
```

## Проверенные факты (живые тесты 10.07.2026, не переоткрывать)

- Relay `/chains`: 72 сети, Abstract(2741) и Base(8453) присутствуют.
- Relay `POST /currencies/v1` `{chainIds:[2741], limit:N}`: **пагинации нет** (page/offset
  игнорируются), но limit до 5000+ работает; на Abstract всего **~1031 токен**, забираем одним вызовом.
  В списке встречаются **невалидные адреса** (`0x`) — фильтруются `Web3.is_address`.
  `verified:true` режет до ~55 токенов и не включает даже USDC — по умолчанию выключено.
- Native-квота Abstract→Base работает: один шаг `deposit` (kind=transaction),
  `to=0x4cd00e387622c35bddb9b4c962c136462338bc31`, gas ≈ 295k → `DEPOSIT_GAS_HINT=600k` с запасом.
- gasPrice Abstract ≈ 0.045 gwei, Base ≈ 0.006 gwei (динамично, оценивать по RPC).
- Скам-токены раздают фейковые балансы **любому** адресу (пустой кошелёк «держит» ~34 шт).
  Их квоты дают `NO_SWAP_ROUTES_FOUND` → статус `SKIPPED` (не FAILED, чтобы retry их не гонял).
- Etherscan-style recalc.py (skill xlsx) не работает на Windows (AF_UNIX) — формулы в отчёте
  считает сам Excel при открытии.

## Состояние (10.07.2026)

- [x] Полный прямой EOA-путь реализован и проверен dry-run на живых публичных API.
- [x] Шаблоны данных генерируются `init-data`; пользователь заполняет `data/wallets.xlsx`.
- [ ] Реальный боевой тест на нескольких кошельках (ожидает заполнения данных пользователем).
- [ ] Браузерная fallback-ветка AdsPower (только если средства окажутся в AGW-контрактах).
- [ ] Возможное расширение функционала (пользователь упоминал будущие доработки).

## Как тестировать без средств

E2E на случайном пустом кошельке (без транзакций): создать XLSX со сгенерированным ключом,
`Pipeline.run(dry_run=True)` — пройдёт preflight/discover/quote и корректно проставит SKIPPED.
Смоук Relay/RPC/Multicall: см. историю — `/chains`, `/currencies/v1`, quote от богатого адреса
(`0xf70da97812CB96acDF810712Aa562db8dfA3dbEF` — Relay solver).

## Конвенции

- Комментарии/логи/CLI — на русском; код — английские идентификаторы.
- Ошибки классифицировать через `src/core/errors.py`; новые транзиентные случаи → RetryableError.
- Любые новые сетевые вызовы кошелька — строго через его прокси и RpcPool.
- Секреты не коммитить: `.env`, `data/wallets.xlsx`, `data/proxies.txt`, `data/state.db` в .gitignore.
