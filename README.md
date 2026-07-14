# Abstract → Base (Relay) → target: кроссчейн-вывод

Автоматизация вывода активов: все токены с кошельков в сети **Abstract** свопятся/бриджатся
через **relay.link** в **native ETH на Base**, затем пересылаются на целевые EVM-адреса.

Архитектура и детальный план — в [PLAN.md](PLAN.md). Контекст для ИИ-агентов — в [CLAUDE.md](CLAUDE.md).

## Принципы

- **Никаких платных/ключевых API** (Etherscan/abscan/Relay-key не нужны): публичный REST Relay,
  публичные RPC, Multicall3. Единственный возможный ключ — AdsPower (fallback-ветка, пока не активна).
- **SQLite — единственный источник истины** (`data/state.db`); XLSX — только вход.
- **Идемпотентность**: повторный запуск продолжает с последнего успешного шага, транзакции не дублируются.
- **HTTP-прокси на кошелёк** (`login:passwd@ip:port`) с авто-ротацией из пула при сбое.
- Приватные ключи живут **только в памяти процесса** (читаются из XLSX на время запуска, в БД не пишутся).

## Быстрый старт

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# 1) создать шаблоны data/wallets.xlsx, data/proxies.txt, .env
.\.venv\Scripts\python -m src.main init-data

# 2) заполнить data/wallets.xlsx (private_key, target_address, опц. proxy)
#    и при необходимости data/proxies.txt (пул прокси для ротации)

# 3) проверить маршруты и суммы без отправки транзакций
.\.venv\Scripts\python -m src.main run --dry-run

# 4) боевой запуск
.\.venv\Scripts\python -m src.main run

# прогресс / перезапуск упавших
.\.venv\Scripts\python -m src.main status
.\.venv\Scripts\python -m src.main retry

# интерактивное меню (запуск без команды) — удобное переключение режимов
.\.venv\Scripts\python -m src.main

# чекер протоколов: вход на relay.link -> AGW-адрес -> DeBank -> Excel-отчёт
.\.venv\Scripts\python -m src.main check-protocols               # потоков из config
.\.venv\Scripts\python -m src.main check-protocols --threads 4   # 4 кошелька параллельно (фаза DeBank)
.\.venv\Scripts\python -m src.main report-protocols              # только перегенерировать Excel из БД
```

## Чекер протоколов (DeBank)

Определяет, какие DeFi-протоколы использует каждый кошелёк:
1. вход на сайт моста (relay.link) вашим ключом → получаем и сохраняем **AGW/Privy-адрес** (`wallets.agw_address`);
2. открываем `debank.com/profile/<agw>` и перехватываем данные портфеля;
3. пишем в БД: каталог протоколов `protocols` (растёт по мере обнаружения новых) + позиции `wallet_protocols`;
4. по завершении сохраняем `reports/protocols_report.xlsx` (листы: protocols / summary / catalog).

Браузер использует прокси кошелька; Privy-сессия кэшируется в `data/.browser/<addr>` (повторный вход мгновенный).

**Многопоточность** (`--threads N` или `execution.check_concurrency`): проверка идёт в два этапа —
(1) вход на relay.link и получение AGW-адресов выполняется **последовательно** (тяжёлый Privy-SPA не
терпит параллельных браузеров), причём кошельки с уже сохранённым адресом этот этап пропускают;
(2) сама проверка на DeBank (публичные страницы, логин не нужен) идёт **параллельно** в N потоков.
Итог: первый прогон делает входы по очереди + DeBank параллельно; повторные прогоны (адреса в БД) —
полностью параллельны и быстры. В Excel-отчёте кошельки визуально разделены (заливка групп + рамка).

## Формат data/wallets.xlsx

| Колонка | Обяз. | Описание |
|---|---|---|
| `address` | нет | адрес EOA; если задан — сверяется с ключом |
| `private_key` | да | приватный ключ EVM-кошелька (0x...) |
| `target_address` | нет | куда пересылать ETH на Base. **Если пусто — задача встаёт в очередь (`WAITING_TARGET`) и ждёт**: как только адрес появится в XLSX, средства уйдут туда |
| `agw_address` | нет | Privy/AGW-адрес кошелька. **Если задан — чекер пропускает вход на relay.link** и сразу идёт на DeBank (быстро, параллельно). Если пусто — адрес определяется входом (медленно) и кэшируется в БД |
| `proxy` | нет | `login:passwd@ip:port`; если пусто — из `data/proxies.txt` |
| `adspower_profile` | нет | id профиля AdsPower (будущая fallback-ветка) |
| `label` | нет | метка |
| `enabled` | нет | 1/0 (по умолчанию 1) |

### XLSX — источник истины (двусторонняя синхронизация)

При каждом `sync`/`run` база данных приводится в соответствие с Excel:
- **новая строка в XLSX** → кошелёк добавляется в БД;
- **строка убрана из XLSX** → кошелёк **удаляется** из БД вместе со своими задачами, балансами и логами;
- **`target_address` заполнен позже** → задачи из очереди `WAITING_TARGET` автоматически продолжаются.
- Защита: если XLSX пуст или не читается, удаление не выполняется (БД не обнуляется).

### Поведение без `target_address`

Кошелёк без адреса назначения **не совершает ончейн-действий**: токены обнаруживаются, задачи создаются
и держатся в статусе `WAITING_TARGET`. Реальный бридж/своп/перевод произойдёт только после того, как
адрес появится в XLSX (следующий `run` подхватит его).

## Как это работает

```
XLSX -> sync -> SQLite
для каждого кошелька (параллельно, через свой прокси):
  PREFLIGHT  балансы EOA на Abstract/Base (публичный RPC)
  DISCOVER   токен-юниверс Relay /currencies/v1 (chainId=2741) ⋂ Multicall3.balanceOf
  на каждый токен (ERC-20 сначала, native ETH последним):
    QUOTE    POST /quote/v2 (recipient = свой адрес на Base); пыль/нет маршрута -> SKIPPED
    APPROVE  если Relay вернул шаг approve (ERC-20)
    DEPOSIT  подпись и отправка deposit-tx на Abstract (EIP-1559, prio=0)
    BRIDGE   ожидание: рост баланса на Base (RPC, источник истины) + intents/status/v3
    TRANSFER native transfer: весь баланс Base - резерв газа -> target_address
```

Статусы джоба: `PENDING → DISCOVERED → QUOTED → APPROVED → DEPOSITED → BRIDGED → TRANSFERRED → DONE`,
ожидание: `WAITING_TARGET` (нет `target_address`), особые: `FAILED`, `SKIPPED`, `REFUNDED`, `NEEDS_BROWSER`.

## Конфигурация

- `.env` — RPC-пулы (`ABSTRACT_RPC_URLS`, `BASE_RPC_URLS`, через запятую), ключ AdsPower.
- `config.yaml` — slippage, резервы газа, пороги пыли, ретраи, прокси, конкурентность.
  Комментарии в самом файле.

## Безопасность

- `.gitignore` исключает `.env`, `data/wallets.xlsx`, `data/proxies.txt`, `data/state.db`, `logs/`.
- Прокси-креды маскируются в логах (`***@ip:port`).
- Перед боевым запуском: прогон `--dry-run`, затем тест на 1 кошельке с малой суммой.
