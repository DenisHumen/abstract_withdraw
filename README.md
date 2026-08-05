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

# браузерный мост: native ETH AGW(Abstract) -> Base (свой EOA) -> биржевой адрес из XLSX
.\.venv\Scripts\python -m src.main bridge-agw --dry-run          # план без браузера и транзакций
.\.venv\Scripts\python -m src.main bridge-agw                    # боевой запуск

# чекер протоколов: вход на relay.link -> AGW-адрес -> DeBank -> Excel-отчёт
.\.venv\Scripts\python -m src.main check-protocols               # потоков из config
.\.venv\Scripts\python -m src.main check-protocols --threads 4   # DeBank-проверки в 4 потока
.\.venv\Scripts\python -m src.main check-protocols -t 4 -l 3     # + входы на relay.link в 3 потока
.\.venv\Scripts\python -m src.main report-protocols              # только перегенерировать Excel из БД
```

## Браузерный мост: ETH AGW → Base → биржа (`bridge-agw`)

Единственная цель моста — **native ETH** (сканирование прочих токенов отключено,
`tokens.native_only: true`). Пошагово, для каждого кошелька из XLSX:

1. **Вход и мост.** Скрипт входит на relay.link через Privy (существующая авторизация),
   выставляет Buy = ETH в сети **Base**, кликает дропдаун кошелька → `Paste wallet address`
   и вставляет **свой EVM-адрес получателя, сгенерированный из `private_key`** этой строки XLSX.
   Затем MAX → SWAP → Approve в Privy-попапе.
2. **Мониторинг Base.** Скрипт ждёт фактического зачисления ETH на этот EVM-адрес
   (проверка баланса по публичному RPC — источник истины).
3. **Отправка на биржу.** Берёт `target_address` (биржевой депозит-адрес) из XLSX:
   - адрес указан → транзакция подписывается приватным ключом и весь баланс (минус резерв газа)
     уходит на биржу;
   - адреса нет → скрипт **не падает**, а переходит в режим ожидания: перечитывает XLSX каждые
     `execution.target_poll_interval_sec` (по умолчанию 30с) и отправляет, как только адрес
     появится в файле. Ctrl+C безопасен — при следующем запуске кошельки в `WAITING_TARGET`
     продолжат с того же места.

Повторный запуск идемпотентен: выполненные кошельки (DONE) пропускаются, сбриджённые — сразу
переходят к отправке, отправленные-но-не-подтверждённые транзакции дожидаются receipt, а не
шлются заново.

## Чекер протоколов (DeBank)

Определяет, какие DeFi-протоколы использует каждый кошелёк. На каждый кошелёк в БД заводятся
**две задачи** (`check_tasks`, статусы PENDING/RUNNING/DONE/FAILED):

1. **`get_agw`** — вход на сайт моста (relay.link) вашим ключом → получаем и сохраняем
   **AGW/Privy-адрес** (`wallets.agw_address`). Если адрес уже известен (из БД или колонки
   `agw_address` в XLSX) — задача сразу помечается DONE, вход не выполняется;
2. **`check_protocols`** — открываем `debank.com/profile/<agw>`, перехватываем данные портфеля
   и пишем в БД: каталог `protocols` (растёт по мере обнаружения новых) + позиции `wallet_protocols`.

Получили адрес → **сразу же** проверяем его на DeBank. По завершении сохраняется
`reports/protocols_report.xlsx` (листы: protocols / summary / catalog) и печатается сводка по задачам
(на большом числе кошельков — только итоги и проблемные строки). Провал входа помечает обе задачи FAILED.

**Многопоточность** — два независимых параметра:
- `--threads N` / `execution.check_concurrency` — потоки DeBank-проверок (headless, лёгкие);
- `--login-threads N` / `execution.login_concurrency` — потоки **входа** на relay.link (headful
  Chromium, тяжёлые). Кошельки, которым нужен вход, делятся между потоками; каждый поток ведёт свои
  последовательно «вход → сразу DeBank → следующий», старты потоков растянуты по времени.

⚠️ Каждый login-поток — полноценное окно Chromium. Слишком много потоков перегружает машину, и входы
начинают падать (модалка кошельков не грузится за 60с). Начните с 2–3 и смотрите по машине; если
входы массово падают — уменьшите до 1. Повторные прогоны (адреса уже в БД) входа не требуют и
полностью параллельны. Браузер ходит через прокси кошелька; каждая попытка входа — со свежим
профилем (Cloudflare-куки не тянутся между попытками). В Excel-отчёте кошельки визуально разделены.

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
