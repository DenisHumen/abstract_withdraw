# PLAN.md — Кроссчейн-вывод: Abstract → Base (Relay) → target EVM-адреса

> Статус: **РЕАЛИЗОВАНО (прямой EOA-путь), ожидает боевого теста**. План подтверждён пользователем 10.07.2026.
> Дата: 2026-07-09, обновлено 2026-07-10. Автор: Claude (research + architecture + implementation).
> Текущее состояние кода и проверенные факты — в [CLAUDE.md](CLAUDE.md), пользовательская дока — в [README.md](README.md).

---

## 0. Резюме задачи

Автоматизировать для набора кошельков (EOA, приватные ключи из XLSX):

1. **Discovery** — найти все токены на **Abstract** (native ETH + ERC-20), у которых есть баланс.
2. **Bridge/Swap** — через протокол **relay.link** сбриджить/свопнуть каждый токен в **native ETH на Base**
   (`Abstract 2741 → Base 8453`, `destinationCurrency = ETH`), `recipient = собственный Base-адрес кошелька`.
3. **Forward** — после прихода ETH на свой Base-адрес выполнить **native-transfer** остатка (минус резерв на газ)
   на **целевой EVM-адрес** из XLSX.

Всё состояние — в **SQLite** (single source of truth), XLSX — только вход. Идемпотентность, ретраи, CLI-логи.

> **Принцип «без сторонних сервисов»** (требование пользователя): **никаких платных/ключевых API**
> (Etherscan/abscan/Relay-key и т.п.). Данные берём из **открытых источников**: публичный API самого
> Relay (это и есть протокол, ключ не нужен), **публичные RPC** сетей и ончейн-контракты (`balanceOf`,
> Multicall3). Единственный секрет, который может понадобиться — `ADSPOWER_API_KEY` (только для fallback-ветки).

### Ключевые решения (подтверждены пользователем)
| Параметр | Решение |
|---|---|
| Кошелёк Abstract | Собственный **EVM-приватный ключ (EOA)**. Privy/AGW-ключа нет. Тот же ключ работает и на Abstract, и на Base. |
| Маршрут | **Все токены Abstract → ETH (native) на Base**, если у Relay есть маршрут. `tradeType = EXACT_INPUT`. |
| Пересылка | **Двухшаговая**: bridge → свой Base-адрес, затем **transfer** на целевой адрес. |
| Сумма | **Весь баланс** токена (для native ETH — минус резерв на газ). |
| Прокси | **HTTP-прокси на каждый кошелёк** (`login:passwd@ip:port`). При сбое — авто-ротация на случайную из пула. |
| Стек | **Python**. |

---

## 1. Результаты анализа relay.link (Шаг 1)

### 1.1. Что такое Relay
Relay — сеть кроссчейн-платежей (intents + DEX meta-aggregation), 85+ сетей. Пользователь подписывает
транзакцию/сообщение на origin-сети, off-chain «relayer/solver» исполняет действие на destination-сети.
Есть официальный REST API (`https://api.relay.link`) и TS-SDK `@reservoir0x/relay-sdk`
(**для Python SDK нет → работаем через REST**).

### 1.2. Модель работы через REST (наш основной путь)
Полная OpenAPI-спека: `https://api.relay.link/documentation/json` (импортируем для генерации типов/валидации).

**Основной цикл: Quote → Execute steps → Poll status.**

**A. Получить квоту** — `POST https://api.relay.link/quote/v2`
```jsonc
// request body
{
  "user":                "0x<EOA>",          // кто депонирует и подписывает на origin
  "recipient":           "0x<EOA-на-Base>",  // куда придут средства (наш Base-адрес)
  "originChainId":       2741,               // Abstract
  "destinationChainId":  8453,               // Base
  "originCurrency":      "0x<token>",        // 0x000...000 = native ETH
  "destinationCurrency": "0x0000000000000000000000000000000000000000", // ETH на Base
  "amount":              "<wei, весь баланс>",
  "tradeType":           "EXACT_INPUT",
  "slippageTolerance":   "50"                // bps, опц. (0..10000); для «всё в ETH» задаём из конфига
}
```

**B. Ответ** (упрощённо):
```jsonc
{
  "steps": [
    {
      "id": "approve",                       // присутствует только для ERC-20
      "kind": "transaction",
      "items": [
        { "status": "incomplete",
          "data": { "to": "0x<token>", "data": "0x095ea7b3...", "value": "0", "chainId": 2741 } }
      ]
    },
    {
      "id": "deposit",
      "kind": "transaction",                 // либо "signature" для некоторых маршрутов
      "requestId": "0x<id>",                 // ← ключ для polling статуса
      "items": [
        { "status": "incomplete",
          "data": { "to": "0x<relay>", "data": "0x...", "value": "<wei>", "chainId": 2741,
                    "maxFeePerGas": "...", "maxPriorityFeePerGas": "..." } }
      ]
    }
  ],
  "fees":    { "gas": {...}, "relayer": {...}, "app": {...} },
  "details": { "currencyIn": {...}, "currencyOut": { "amount": "...", "amountFormatted": "..." },
               "timeEstimate": 12, "rate": "..." }
}
```

**C. Исполнить шаги** — по порядку, для каждого шага и его `items[]` (по официальной схеме step-execution):
- `kind == "transaction"` → подписать и отправить `{to, data, value, chainId}` на **origin RPC (Abstract)**;
  затем прогресс шага отслеживается по `item.check`-эндпоинту (у Relay) **и/или** ончейн-квитанции.
- `kind == "signature"` → подписать (`EIP-191` или `EIP-712`, по `signatureKind`) и **отправить подпись POST-ом**
  на `item.data.post.endpoint` с телом из `post` (Relay сам инициирует ончейн-исполнение через solver).
- `approve` (если присутствует) исполняется **до** `deposit`.

**D. Дождаться исполнения** — источник истины: **публичный RPC Base** + статус Relay (тоже публичный):
- Первично: поллим **баланс `recipient` на Base через публичный RPC** (`eth_getBalance`) — фиксируем факт
  прихода ETH (рост ≥ ожидаемого `amount_out − допуск`). Не зависит ни от каких ключей.
- Дополнительно: `GET https://api.relay.link/intents/status/v3?requestId=0x<id>`
  (`waiting → pending → success` | `failure`/`refund`) — даёт dst txHash и причину при неуспехе. Публичный, без ключа.
- Поллинг с интервалом/таймаутом из конфига.

### 1.3. Вспомогательные эндпоинты (все публичные, без ключа)
| Назначение | Эндпоинт |
|---|---|
| Список поддерживаемых сетей | `GET /chains` |
| Полный список токенов сети (наш «токен-юниверс») | `POST /currencies/v1` body `{"chainIds":[2741]}` |
| Список реквестов (история) | `GET /requests/v2?user=0x...` |

`POST /currencies/v1` с телом `{"chainIds":[2741]}` возвращает **весь индексируемый Relay список токенов
Abstract** (без обязательного `term`). Ответ по токену: `chainId, address, symbol, name, decimals, vmType,
metadata{logoURI, verified, isNative}`. Это и есть множество **потенциально маршрутизируемых** токенов —
пересекаем с фактическими ончейн-балансами кошелька (см. §5, discovery). Заголовок `x-api-key`
**не обязателен** (только выше rate-limit).

> **Проверено живым тестом (10.07.2026):** пагинации у `/currencies/v1` нет (`page`/`offset` игнорируются),
> но `limit` принимает большие значения — на Abstract всего ~1031 токен, забирается одним вызовом
> (`limit: 5000`). `verified:true` режет список до ~55 и не включает даже USDC → по умолчанию не используем.
> В списке встречаются невалидные адреса (`0x`) — фильтруются на discovery.

### 1.4. Технические выводы по Abstract (критично)
- **Abstract = chainId 2741**, zkSync ZK-stack, native ETH, RPC `https://api.mainnet.abs.xyz`
  (explorer `abscan.org` — только для ручной проверки человеком, **не как API**).
- **EOA поддерживается штатно**: Abstract принимает **стандартные EIP-1559** транзакции от EOA через
  `eth_sendRawTransaction` (именно так работает MetaMask на Abstract). → **Наш EVM-приватный ключ подписывает
  транзакции Abstract напрямую, без Privy/AGW.**
- Нюансы zkSync-газа: `maxPriorityFeePerGas` рекомендуется `0`; специфичное поле `gasPerPubdata`;
  газ оцениваем через RPC (`eth_estimateGas` / `zks_estimateFee`), не хардкодим.
- **Fallback подписи** (если RPC отклонит стандартную tx / упадёт оценка газа): EIP-712 **type-113**
  через `zksync2` (Python) — `PrivateKeyEthSigner` + `tx712()` + `send_raw_transaction`.
- **Оговорка про AGW**: если фактически средства лежат не на EOA, а в смарт-контракте AGW (баланс EOA = 0,
  а на AGW-адресе есть токены), приватным ключом их **не двинуть** → сработает ветка AdsPower (см. §7).
  Мы это **детектим на pre-flight** (проверка баланса EOA на Abstract) и не гадаем.

### 1.5. Открытые вопросы к Relay (проверяем по OpenAPI на этапе кода, не блокеры)
- Точная форма шага `signature`/`post.endpoint` для маршрутов Abstract.
- Есть ли на некоторых токенах Abstract permit/permit2 вместо `approve`.
- Все нужные эндпоинты (`quote/v2`, `intents/status/v3`, `currencies/v1`, `chains`, `requests/v2`) **публичные,
  ключ не требуется** → в `.env` ключа Relay нет. Рейт-лимиты гасим прокси + пулом RPC (§4).

---

## 2. Архитектура и стек (Шаг 2)

### 2.1. Стек
| Слой | Выбор | Зачем |
|---|---|---|
| Язык | **Python 3.11+** | по требованию пользователя |
| Web3 | **web3.py (v7)** + **eth-account** | Base + Abstract EOA (EIP-1559) |
| Abstract fallback | **zksync2** | EIP-712 type-113, если стандартная tx не проходит |
| HTTP | **httpx** (sync) | Relay публичный REST, AdsPower Local API; per-wallet proxy |
| Прокси | **httpx `proxy=`** + web3 `HTTPProvider(request_kwargs={"proxies": ...})` | единый HTTP-прокси на кошелёк для всех сетевых вызовов + ротация |
| RPC | **пул публичных RPC** на сеть (ChainList) + ротация | без платных нод; обход рейт-лимитов вместе с прокси |
| XLSX | **openpyxl** | чтение входного файла |
| БД | **sqlite3 (stdlib)** + тонкий DAL-слой | single source of truth; без тяжёлого ORM |
| Конфиг | **pydantic-settings** + **PyYAML** + **python-dotenv** | валидация конфигурации и секретов |
| Логи | **rich** (+ опц. `logging` в файл JSONL) | структурированный CLI-вывод, таблицы прогресса |
| CLI | **typer** | команды `sync`, `run`, `status`, `retry`, `discover` |
| Fallback-браузер | **playwright** + AdsPower Local API | если прямой путь недоступен (AGW) |
| Ретраи | **tenacity** | экспоненциальный backoff для транзиентных ошибок |
| Токены Abstract | Relay `POST /currencies/v1` ⋂ **Multicall3 `balanceOf`** по публичному RPC | discovery ERC-20 **без эксплорер-API** |
| Multicall3 | `0xcA11bde05977b3631167028862bE2a173976CA11` (есть на Abstract) | батч `balanceOf` в один RPC-вызов |

### 2.2. Структура проекта
```
abstract_withdrow/
├─ .env                      # секреты (НЕ в git)
├─ .env.example
├─ config.yaml               # параметры запуска (не секреты)
├─ requirements.txt
├─ PLAN.md
├─ data/
│  ├─ wallets.xlsx           # вход
│  ├─ proxies.txt            # пул прокси для ротации (login:passwd@ip:port, по строке)
│  └─ state.db               # SQLite (source of truth)
├─ logs/
│  └─ run-YYYYMMDD.jsonl
└─ src/
   ├─ main.py                # typer CLI: sync/run/status/retry
   ├─ config.py              # pydantic-модели + загрузка .env/yaml
   ├─ logger.py              # rich-логгер, форматы статусов/таймстампов
   ├─ db/
   │  ├─ schema.sql          # DDL
   │  ├─ models.py           # dataclasses/типы строк
   │  └─ dao.py              # CRUD + идемпотентные upsert-ы, транзакции
   ├─ data_io/               # (переименовано из io/ — не конфликтует со stdlib-модулем io)
   │  └─ excel.py            # чтение XLSX → sync в SQLite
   ├─ net/
   │  ├─ proxy.py            # пул прокси, привязка к кошельку, health-check, ротация при сбое
   │  └─ rpc.py              # пул публичных RPC на сеть, health-check, ротация при сбое/лимите
   ├─ chains/
   │  ├─ abstract.py         # клиент Abstract: баланс, send tx (EIP-1559 → zksync2 fallback); через proxy+rpc-пул
   │  ├─ base.py             # клиент Base: баланс, native transfer, balance-watch recipient; через proxy+rpc-пул
   │  ├─ multicall.py        # Multicall3-агрегатор balanceOf/allowance
   │  └─ tokens.py           # discovery: Relay /currencies/v1 ⋂ Multicall3 balanceOf (без эксплореров)
   ├─ relay/
   │  ├─ client.py           # quote/v2, execute steps, status/v3, currencies, chains
   │  └─ types.py            # типы запросов/ответов (pydantic)
   ├─ core/
   │  ├─ pipeline.py         # оркестратор per-wallet/per-token, state machine
   │  ├─ steps.py            # DISCOVER→QUOTE→APPROVE→DEPOSIT→BRIDGE→TRANSFER
   │  └─ errors.py           # типизированные исключения + классификация retryable/permanent
   └─ browser/              # FALLBACK-ветка (реализуем при необходимости)
      ├─ adspower.py         # AdsPower Local API: start/stop профиля → CDP ws endpoint
      └─ relay_ui.py         # Playwright-автоматизация relay.link через профиль
```

### 2.3. Модель «двух кошельков» (Abstract native / EVM)
Один приватный ключ → два клиента:
- **AbstractClient** (origin): RPC Abstract, EIP-1559/zksync-подпись, аппрувы, deposit-tx Relay.
- **BaseClient** (EVM): RPC Base, стандартный EIP-1559, финальный native-transfer.

Это ровно отражает требование «Abstract Native Wallet / EVM Wallet»: адрес один, клиенты и правила газа — разные.

---

## 3. Схема БД (SQLite)

Единица работы (job) = пара **(wallet, token)**. Прогресс отслеживается пошагово.

```sql
-- Кошельки (из XLSX, обогащаются на sync)
CREATE TABLE wallets (
  id              INTEGER PRIMARY KEY,
  address         TEXT NOT NULL UNIQUE,       -- EOA (0x..), выводится из приватного ключа
  pk_ref          TEXT NOT NULL,              -- ссылка на ключ (см. §4.3, ключ в БД НЕ хранится в открытом виде)
  target_address  TEXT,                       -- целевой EVM-адрес; NULL = ждём адрес (задача WAITING_TARGET)
  proxy           TEXT,                       -- текущий HTTP-прокси login:passwd@ip:port (из XLSX или назначен из пула)
  proxy_source    TEXT,                       -- 'xlsx' | 'pool' — откуда взят текущий прокси
  proxy_status    TEXT DEFAULT 'unknown',     -- unknown | ok | dead (обновляется health-check/ротацией)
  adspower_profile TEXT,                      -- id профиля AdsPower (для fallback), опц.
  label           TEXT,
  enabled         INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

-- Снимок обнаруженных токенов на Abstract
CREATE TABLE token_balances (
  id           INTEGER PRIMARY KEY,
  wallet_id    INTEGER NOT NULL REFERENCES wallets(id),
  chain_id     INTEGER NOT NULL,              -- 2741
  token_addr   TEXT NOT NULL,                 -- 0x000..0 = native ETH
  symbol       TEXT,
  decimals     INTEGER,
  raw_balance  TEXT NOT NULL,                 -- wei (строкой, чтобы не терять точность)
  routable     INTEGER,                       -- 1 если есть маршрут в ETH@Base (по /currencies)
  discovered_at TEXT NOT NULL,
  UNIQUE(wallet_id, chain_id, token_addr)
);

-- Джоб на вывод одного токена: state machine
CREATE TABLE jobs (
  id            INTEGER PRIMARY KEY,
  wallet_id     INTEGER NOT NULL REFERENCES wallets(id),
  token_addr    TEXT NOT NULL,                -- что бриджим с Abstract
  amount_in     TEXT,                         -- wei, зафиксировано на этапе QUOTE
  status        TEXT NOT NULL DEFAULT 'PENDING',
    -- PENDING → DISCOVERED → QUOTED → APPROVED → DEPOSITED → BRIDGED → TRANSFERRED → DONE
    -- ожидание: WAITING_TARGET (нет target_address — задача в очереди, ончейн-действий нет)
    -- терминальные/особые: FAILED, SKIPPED (dust/no route), REFUNDED, NEEDS_BROWSER
  request_id    TEXT,                         -- Relay requestId (идемпотентность bridge)
  amount_out    TEXT,                         -- фактически получено ETH@Base (wei)
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  error_class   TEXT,                         -- retryable | permanent | manual
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE(wallet_id, token_addr)               -- один активный джоб на (кошелёк,токен)
);

-- Все ончейн-действия и их хэши (аудит + защита от повторной отправки)
CREATE TABLE tx_log (
  id          INTEGER PRIMARY KEY,
  job_id      INTEGER NOT NULL REFERENCES jobs(id),
  step        TEXT NOT NULL,                  -- approve | deposit | transfer
  chain_id    INTEGER NOT NULL,
  tx_hash     TEXT,
  nonce       INTEGER,
  status      TEXT NOT NULL DEFAULT 'sent',   -- sent | confirmed | reverted
  gas_used    TEXT,
  raw_request TEXT,                           -- сырой {to,data,value} из Relay (JSON)
  created_at  TEXT NOT NULL,
  UNIQUE(job_id, step, tx_hash)
);

-- Курсор синка Excel (детект изменений входа)
CREATE TABLE sync_meta (
  key   TEXT PRIMARY KEY,                     -- 'xlsx_hash', 'last_sync', ...
  value TEXT
);
```

**Идемпотентность на уровне схемы:**
- `UNIQUE(wallet_id, token_addr)` в `jobs` — не создаём дубликаты джоба.
- `request_id` фиксируется при первом успешном quote-deposit → повторный запуск НЕ шлёт новый deposit,
  а поллит статус уже существующего `request_id`.
- `tx_log` хранит `nonce`+`tx_hash` → перед отправкой проверяем, нет ли уже отправленной tx для (job, step).

---

## 4. Конфигурация

### 4.1. `.env` (секреты, не в git)
```dotenv
# --- Ключи API сторонних сервисов НЕ используются (Etherscan/abscan/Relay-key НЕ нужны). ---

# Опц. шифрование приватников в БД (если выберем этот режим; иначе не задаём).
WALLET_ENCRYPTION_KEY=

# Публичные RPC-пулы (через запятую) — берём открытые ноды с ChainList, без ключей.
# Первый в списке — основной, остальные — для ротации при сбое/лимите.
ABSTRACT_RPC_URLS=https://api.mainnet.abs.xyz
BASE_RPC_URLS=https://mainnet.base.org,https://base.publicnode.com,https://base-rpc.publicnode.com

# AdsPower (ЕДИНСТВЕННЫЙ возможный ключ; только для fallback-ветки).
ADSPOWER_API_KEY=
ADSPOWER_BASE_URL=http://local.adspower.net:50325
```
> RPC-эндпоинты — публичные и взаимозаменяемые; список расширяем/правим свободно. Ключей ни к Relay,
> ни к эксплореру нет by design.

### 4.2. `config.yaml` (параметры, не секреты)
```yaml
mode:
  forward_mode: two_step          # bridge → own Base → transfer (подтверждено)
  use_browser_fallback: auto      # auto | never | always
  dry_run: false                  # считать/квотить без отправки транзакций

routing:
  origin_chain_id: 2741
  dest_chain_id: 8453
  dest_currency: "0x0000000000000000000000000000000000000000"   # ETH на Base
  trade_type: EXACT_INPUT
  slippage_bps: 50

amounts:
  bridge_full_balance: true
  # Резерв газа для native-ETH: динамическая оценка (estimateGas × fee × множитель), с полом ниже.
  gas_estimate_multiplier: 1.5
  gas_reserve_abstract_floor_wei: "300000000000000"  # минимальный резерв ETH на газ Abstract (пол)
  gas_reserve_base_floor_wei:     "100000000000000"  # минимальный резерв на финальный transfer Base
  # Порог «пыли» БЕЗ прайс-API: решаем по выходу ETH из квоты Relay (в ней уже посчитан amountOut).
  min_native_out_wei: "200000000000000"          # < 0.0002 ETH на выходе → SKIPPED
  skip_if_out_lte_forward_gas: true              # если выход ≤ газа на forward-transfer — не бриджим

execution:
  concurrency: 3                  # кошельков параллельно
  quote_ttl_sec: 30               # переквотить, если протухло
  status_poll_interval_sec: 2
  status_timeout_sec: 900
  tx_confirmations: 1

rpc:
  rotate_on_failure: true         # при сбое/лимите ноды — следующая из пула ABSTRACT_RPC_URLS/BASE_RPC_URLS
  health_check: true              # быстрый eth_chainId перед боем
  timeout_sec: 20
  max_rotations_per_call: 3

retry:
  max_attempts: 5
  backoff_base_sec: 2
  backoff_max_sec: 60

proxy:
  enabled: true
  scheme: http                    # http (формат login:passwd@ip:port)
  pool_file: data/proxies.txt     # пул для ротации: по одной прокси на строку
  per_wallet_from_xlsx: true      # приоритет: прокси из колонки XLSX; иначе назначаем из пула
  rotate_on_failure: true         # при сбое прокси — взять случайную рабочую из пула
  max_rotations_per_job: 3        # сколько раз менять прокси в рамках одного шага, прежде чем FAILED
  health_check: true              # быстрый пинг прокси (напр. GET https://api.relay.link/chains) перед боем
  health_check_timeout_sec: 8
  sticky: true                    # закрепить назначенную прокси за кошельком (консистентный IP)
  persist_assignment: true        # сохранять назначенную прокси в wallets.proxy (переживает рестарт)

tokens:
  discovery: relay_currencies_plus_onchain  # Relay /currencies/v1 ⋂ Multicall3 balanceOf (без эксплореров)
  include_native_eth: true
  verified_only: false               # true — только verified из Relay-списка (меньше скама, но можно недобрать)
  use_external_search: false         # Relay useExternalSearch — расширить поиск токенов (по желанию)
  allowlist: []                      # доп. токены вручную (если не попали в Relay-список)
  denylist: []                       # исключить (скам/невыводимые)
```

### 4.3. Формат XLSX (`data/wallets.xlsx`)
| Колонка | Обяз. | Описание |
|---|---|---|
| `address` | опц. | EOA-адрес (если пусто — выводим из приватного ключа) |
| `private_key` | да\* | приватный ключ EVM (0x...). \*Для fallback может быть пусто, если только AdsPower |
| `target_address` | да | целевой EVM-адрес для финального transfer |
| `proxy` | опц. | HTTP-прокси кошелька в формате `login:passwd@ip:port`. Если пусто — назначается из пула `data/proxies.txt` |
| `adspower_profile` | опц. | id профиля AdsPower для браузерной ветки |
| `label` | опц. | метка/имя |
| `enabled` | опц. | 1/0 |

**Безопасность ключей:** приватники из XLSX при `sync` не пишутся в `state.db` в открытом виде.
Опции (выбор на ревью): (a) шифровать в БД ключом `WALLET_ENCRYPTION_KEY`, (b) держать XLSX как единственный
источник ключей и читать в память только на время запуска. По умолчанию план — **(b) + опц. шифрование (a)**.

### 4.4. Синхронизация Excel → SQLite (XLSX — источник истины)
- Считаем hash XLSX; если не менялся — можно пропускать ресинк (сейчас синк идёт всегда перед run).
- `upsert` по `address`: добавляем новые, обновляем `target_address`/`label`/`enabled`/`proxy`, **не трогаем** прогресс `jobs`.
- **Удалённые из XLSX кошельки удаляются из БД** вместе с их `jobs`/`token_balances`/`tx_log` (каскад вручную).
  Защита: удаление выполняется только если XLSX успешно распарсен и содержит ≥1 валидный кошелёк
  (пустой/битый файл не обнуляет БД).
- **`target_address` опционален**: пусто → задачи держатся в `WAITING_TARGET` без ончейн-действий;
  как только адрес появится в XLSX и пройдёт `sync` — задачи возобновляются (сбрасываются в `DISCOVERED`).

### 4.5. Прокси: формат, пул и ротация
Цель — избежать рейт-лимитов Relay/RPC и держать **консистентный IP на кошелёк**.

**Формат и источники:**
- Формат прокси: `login:passwd@ip:port` (нормализуем в URL `http://login:passwd@ip:port`).
- Пул для ротации — `data/proxies.txt`, по одной прокси на строку (тот же формат; строки с `#` — комментарии).
- Приоритет назначения: **колонка `proxy` в XLSX** → иначе случайная из пула. Назначение сохраняется в
  `wallets.proxy` (`sticky` + `persist_assignment`), поэтому переживает рестарт и остаётся стабильным.

**Куда применяется** (единый прокси на все сетевые вызовы кошелька):
- `httpx.Client(proxy="http://login:passwd@ip:port")` — публичный Relay REST.
- web3.py: `Web3(HTTPProvider(rpc_url, request_kwargs={"proxies": {"http": p, "https": p}, "timeout": ...}))`
  — RPC Abstract и Base (и `zksync2`, т.к. он поверх web3.py).
- Fallback-ветка: прокси задаётся в самом профиле AdsPower; при программном старте профиля можем
  передавать/сверять прокси через параметры AdsPower Local API.

**Ротация при сбое** (`net/proxy.py`):
1. Проксирующая ошибка (connect/timeout/`ProxyError`/407/5xx от прокси, либо явный rate-limit 429 через прокси)
   классифицируется как **`ProxyError` (retryable)**.
2. Менеджер помечает текущую прокси `dead`, берёт **случайную рабочую** из пула (исключая dead), обновляет
   `wallets.proxy`+`proxy_source='pool'` и **пересоздаёт HTTP/web3-клиенты** кошелька.
3. Повтор шага с новой прокси; до `proxy.max_rotations_per_job` попыток. Если пул исчерпан/все dead →
   джоб `FAILED` с `error_class=retryable` (можно перезапустить позже, когда прокси починятся).
4. Опциональный `health_check` перед боем: быстрый `GET /chains` через прокси; мёртвые сразу исключаются.
5. `dead`-прокси периодически можно «реанимировать» (сбросить статус) на новом запуске — не залипаем навсегда.

**`.env` дополнение:** секретов для прокси не требуется (креды — внутри строки прокси). Файл пула путём
задаётся в `config.yaml` (`proxy.pool_file`).

---

## 5. Основной алгоритм (Шаг 4 — pipeline)

Оркестратор идёт по `enabled`-кошелькам (с ограничением `concurrency`), для каждого — по обнаруженным токенам.

```
для каждого wallet (enabled):
  0. PRE-FLIGHT:
     - PROXY+RPC: взять wallets.proxy (или назначить из пула), опц. health-check;
       собрать клиенты кошелька на этой прокси (Relay публичный API + пул публичных RPC Abstract/Base)
     - abstract.get_native_balance(EOA), base.get_native_balance(EOA)
     - если EOA-баланс на Abstract == 0, но ожидались средства → пометить NEEDS_BROWSER (§7), лог WARN
  1. DISCOVER (без эксплореров):
     - токен-юниверс = Relay POST /currencies/v1 {chainIds:[2741]} (⋃ allowlist, − denylist)
     - Multicall3.balanceOf(EOA, все токены) по публичному RPC + eth_getBalance для native ETH
     - upsert token_balances (routable=1 для всех из Relay-списка);
       создать jobs(status=DISCOVERED) для balance>0
  для каждого token-job:
    2. QUOTE:
       - amount = full balance (native: минус динамический резерв газа Abstract)
       - POST /quote/v2 → сохранить request_id, amount_in, amount_out(ожид.)
       - нет маршрута → SKIPPED; amount_out < min_native_out_wei или ≤ газа forward → SKIPPED (dust)
       - status=QUOTED
    3. APPROVE (только ERC-20, если шаг присутствует и allowance < amount):
       - отправить approve-tx на Abstract; дождаться confirm; tx_log
       - status=APPROVED
    4. DEPOSIT:
       - если для job уже есть request_id со статусом success → пропустить (идемпотентность)
       - отправить deposit-tx (или подписать signature-step) на Abstract; tx_log
       - status=DEPOSITED
    5. BRIDGE (poll, публичные источники):
       - первично: baseRPC.watch_balance(recipient) — рост ETH ≥ amount_out − допуск (source of truth)
       - параллельно: GET /intents/status/v3?requestId=... (dst txHash / причина при failure/refund)
       - success → записать фактический amount_out, status=BRIDGED
       - failure/refund → FAILED/REFUNDED (классифицировать); timeout → RetryableError
    6. TRANSFER (forward_mode=two_step):
       - amount = base_balance − динамический резерв газа Base (floor из конфига)
       - native transfer → target_address; tx_log; дождаться confirm (Base RPC receipt)
       - status=TRANSFERRED → DONE
```

**Что «весь баланс» значит на практике:**
- ERC-20: `amount = balanceOf(token)` целиком.
- native ETH (origin): `balance − max(динамич. оценка газа deposit × multiplier, gas_reserve_abstract_floor_wei)`.
- Финальный transfer (Base, native ETH): `balance − max(динамич. оценка газа transfer × multiplier, gas_reserve_base_floor_wei)`.

---

## 6. Идемпотентность и защита от повторов

| Риск | Защита |
|---|---|
| Повторный запуск после падения | Все статусы в SQLite; pipeline возобновляется с последнего завершённого шага джоба. |
| Двойной deposit в Relay | `request_id` в `jobs`; при наличии — поллим статус, новый deposit не шлём. |
| Двойная ончейн-tx (approve/deposit/transfer) | Перед отправкой проверяем `tx_log` (есть `sent`/`confirmed` для (job,step)); проверяем on-chain nonce/receipt. |
| Двойной финальный transfer | Флаг `TRANSFERRED` + запись `transfer` в `tx_log`; повторно не отправляем. |
| Изменение XLSX между запусками | Hash-детект + upsert без сброса прогресса. |
| Гонки при concurrency | Джобы партиционированы по кошельку; запись статуса — в одной SQLite-транзакции; `BEGIN IMMEDIATE`. |
| Протухшая квота | `quote_ttl_sec`; переквот перед отправкой, если истекло. |

---

## 7. AGW-ветка: Playwright + один ключ (реализуется, `src/browser/`)

**Обновлено 2026-07-12 по факту исследования.** Тест-кошелёк оказался **AGW** (Abstract Global Wallet):
средства в смарт-контракте, он-чейн подписант — встроенный Privy-ключ, а НЕ наш MetaMask-ключ
(см. §1.4, детали в [CLAUDE.md](CLAUDE.md)). Прямой RPC/agw-client с MetaMask-ключом невозможен.
Экспорт Privy-ключа headless невозможен (Privy: только ручное UI-действие). Поэтому — **браузер**.

**Ключевой результат:** вывод возможен **одним MetaMask-ключом БЕЗ расширения и БЕЗ AdsPower** —
через инжектируемый `window.ethereum` в Playwright: relay.link → Privy cross-app вход (SIWE-подпись
нашим ключом) → Privy сам подписывает мост встроенным ключом. Проверено вживую (вход как
AGW `0xF094…a671`). AdsPower НЕ требуется (можно использовать для мульти-профилей, но не обязателен).

Поток (`src/browser/`):
1. `make_injector(pk)` → инжект `window.ethereum` (подпись `personal_sign`/typedData одним ключом;
   **0x-префикс обязателен** — hexbytes v1). Playwright: `launch_persistent_context(headless=False)` +
   `expose_binding('__walletSign', signer)` + `add_init_script(inject_js)`.
2. `login()`: Connect → **Abstract** → Privy popup → «Continue with a wallet» → MetaMask → SIWE(наш ключ)
   → Approve. Возвращает подключённый AGW-адрес. **Проверено.**
3. `bridge_native_eth()`: Buy→**ETH@Base**, `recipient`=наш Base-EOA (обязательно ≠ отправитель!),
   сумма MAX, кнопка Bridge, подтвердить в Privy popup. Селекторы формы — доделать/валидировать.
4. Далее — **программно** (как для EOA): ждём ETH на Base у нашего EOA, `transfer` на `target_address`.

Интеграция в pipeline: для AGW-кошельков «deposit» на Abstract идёт через браузер, discovery — на
AGW-адресе, всё остальное (state/idempotency/Base-transfer) переиспользуется.
> ⚠ Реальный мост двигает средства — запускать только с явного согласия и малой суммой.

---

## 8. Обработка ошибок

**Классификация исключений (`core/errors.py`):**
- `RetryableError` — RPC timeout/5xx, rate-limit (429), «quote expired», временная нехватка ликвидности,
  «tx not yet mined». → tenacity: экспоненциальный backoff (`backoff_base..backoff_max`), до `max_attempts`.
- `ProxyError` (подкласс retryable) — connect/timeout/`ProxyError`/407/5xx от прокси или 429 через прокси.
  → пометить прокси `dead`, взять случайную рабочую из пула, **пересоздать клиенты кошелька**, повторить шаг;
  до `proxy.max_rotations_per_job`. Пул исчерпан → `FAILED` (retryable, перезапуск позже).
- `RpcError` (подкласс retryable) — публичная нода упала/лимитит/рассинхрон. → переключиться на следующий
  RPC из пула (`ABSTRACT_RPC_URLS`/`BASE_RPC_URLS`), повторить; до `rpc.max_rotations_per_call`.
- `PermanentError` — no route, insufficient funds, revert аппрува, невалидный адрес, decimals mismatch.
  → джоб `FAILED`/`SKIPPED`, `error_class=permanent`, без ретраев.
- `ManualError` — нужен человек: `NEEDS_BROWSER` (AGW), подозрение на скам-токен, аномальный slippage.
  → пометка, пропуск, вывод в итоговый отчёт.

**Принципы:**
- **Изоляция кошельков**: падение одного джоба не останавливает остальные (обрабатываем и логируем).
- **Идемпотентная запись**: статус/ошибка пишутся в БД до и после каждого шага.
- **Nonce-менеджмент**: получаем `pending` nonce, при коллизии — refetch и ретрай.
- **Газ**: всегда оценка через RPC + запас; для Abstract `maxPriorityFeePerGas=0`.
- **Circuit breaker**: при серии сетевых ошибок подряд — пауза/стоп с понятным сообщением.
- **Финальный отчёт**: сводка DONE/FAILED/SKIPPED/NEEDS_BROWSER с суммами и хэшами.

---

## 9. Логирование и CLI

**CLI (`typer`):**
- `python -m src.main sync` — XLSX → SQLite.
- `python -m src.main discover [--wallet ADDR]` — только discovery токенов.
- `python -m src.main run [--wallet ADDR] [--dry-run]` — основной пайплайн.
- `python -m src.main status` — таблица прогресса из БД.
- `python -m src.main retry [--only-failed]` — перезапуск упавших джобов.

**Логи (`rich`)**: на каждый шаг — таймстамп, кошелёк(маскир.), токен, действие, статус, tx-hash/requestId,
суммы. Итоговая таблица прогресса. Опционально дублируем в `logs/*.jsonl` для машинного разбора.
Пример строки:
```
[12:41:07] [0x1a2b…9f] [USDC] DEPOSIT sent   tx=0xabc… amount=125.44 USDC → ETH@Base  req=0x77…
[12:41:31] [0x1a2b…9f] [USDC] BRIDGE success out=0.0413 ETH  dstTx=0xdef…
```

---

## 10. Дорожная карта реализации — состояние на 10.07.2026

1. ✅ Каркас проекта, `config.py`, `.env.example`, `requirements.txt`, `schema.sql`, логгер.
2. ✅ `data_io/excel.py` + `db/dao.py` + команда `sync` (идемпотентность проверена E2E-тестом).
3. ✅ `net/proxy.py` + `net/rpc.py` (пулы прокси/RPC, ротация) + `relay/client.py`
   (quote/v2, status/v3, currencies/v1, chains; публичный доступ, ретраи).
4. ✅ `chains/abstract.py` (EIP-1559 prio=0, legacy/zksync2-fallback), `chains/base.py` (transfer +
   balance-watch), `chains/multicall.py`, `chains/tokens.py` (discovery = Relay currencies ⋂ Multicall3).
5. ✅ `core/pipeline.py` + `steps.py` — state machine, идемпотентность, `--dry-run`.
6. ✅ Живые смоук-тесты (chains/currencies/quote/RPC/Multicall) + E2E dry-run на пустом кошельке.
   Найдено и исправлено: невалидные адреса в списке Relay; `NO_SWAP_ROUTES_FOUND` → `SKIPPED`.
7. ⏳ Боевой прогон на 1 кошельке малой суммой → затем масштабирование (`concurrency`).
   **Ожидает заполнения `data/wallets.xlsx` пользователем.**
8. ⬜ (При необходимости) браузерная ветка `browser/adspower.py` + `browser/relay_ui.py`
   (сейчас — заглушка с контрактом реализации в `src/browser/__init__.py`).

---

## 11. Решения, принятые на реализацию (дефолты; правятся в config.yaml)
1. **Приватные ключи**: только XLSX → память процесса на время запуска; в БД не пишутся.
2. **Финальный transfer**: native-ETH на `target_address` в Base.
3. **Discovery**: Relay `POST /currencies/v1` ⋂ Multicall3 `balanceOf`; `verified_only: false`
   (verified-список слишком узкий — нет даже USDC); скам без маршрута отсеивается статусом `SKIPPED`.
4. **Резервы газа**: динамическая оценка через RPC × 1.5 + полы (0.0003 ETH Abstract / 0.0001 ETH Base).
5. **Slippage**: 50 bps по умолчанию (`routing.slippage_bps`).
6. **AdsPower**: пока не реализуем — прямой EOA-путь первичен; заглушка и контракт в `src/browser/`.
7. **Concurrency**: 3 (`execution.concurrency`).
8. **Прокси**: поддержаны оба источника — колонка `proxy` XLSX (приоритет) и пул `data/proxies.txt`;
   схема `http`.

> ✅ План подтверждён, код реализован. Следующий шаг — боевой тест на нескольких кошельках.
