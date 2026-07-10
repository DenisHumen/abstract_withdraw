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
```

## Формат data/wallets.xlsx

| Колонка | Обяз. | Описание |
|---|---|---|
| `address` | нет | адрес EOA; если задан — сверяется с ключом |
| `private_key` | да | приватный ключ EVM-кошелька (0x...) |
| `target_address` | да | куда пересылать ETH на Base |
| `proxy` | нет | `login:passwd@ip:port`; если пусто — из `data/proxies.txt` |
| `adspower_profile` | нет | id профиля AdsPower (будущая fallback-ветка) |
| `label` | нет | метка |
| `enabled` | нет | 1/0 (по умолчанию 1) |

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
особые: `FAILED`, `SKIPPED`, `REFUNDED`, `NEEDS_BROWSER`.

## Конфигурация

- `.env` — RPC-пулы (`ABSTRACT_RPC_URLS`, `BASE_RPC_URLS`, через запятую), ключ AdsPower.
- `config.yaml` — slippage, резервы газа, пороги пыли, ретраи, прокси, конкурентность.
  Комментарии в самом файле.

## Безопасность

- `.gitignore` исключает `.env`, `data/wallets.xlsx`, `data/proxies.txt`, `data/state.db`, `logs/`.
- Прокси-креды маскируются в логах (`***@ip:port`).
- Перед боевым запуском: прогон `--dry-run`, затем тест на 1 кошельке с малой суммой.
