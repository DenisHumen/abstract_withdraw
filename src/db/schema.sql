-- SQLite-схема (single source of truth). См. PLAN.md §3.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS wallets (
  id               INTEGER PRIMARY KEY,
  address          TEXT NOT NULL UNIQUE,      -- EOA (checksum), выводится из приватного ключа
  pk_ref           TEXT NOT NULL DEFAULT 'xlsx', -- откуда берём ключ ('xlsx'); сам ключ в БД НЕ хранится
  target_address   TEXT,                      -- целевой EVM-адрес финального transfer; NULL = ждём адрес (WAITING_TARGET)
  agw_address      TEXT,                      -- Privy/AGW-адрес, полученный при входе на relay.link (для DeBank-чека)
  proxy            TEXT,                      -- текущий HTTP-прокси login:passwd@ip:port
  proxy_source     TEXT,                      -- 'xlsx' | 'pool'
  proxy_status     TEXT DEFAULT 'unknown',    -- unknown | ok | dead
  adspower_profile TEXT,
  label            TEXT,
  enabled          INTEGER NOT NULL DEFAULT 1,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS token_balances (
  id            INTEGER PRIMARY KEY,
  wallet_id     INTEGER NOT NULL REFERENCES wallets(id),
  chain_id      INTEGER NOT NULL,
  token_addr    TEXT NOT NULL,                -- 0x000..0 = native ETH
  symbol        TEXT,
  decimals      INTEGER,
  raw_balance   TEXT NOT NULL,                -- wei строкой
  routable      INTEGER,                      -- 1 = есть в Relay /currencies
  discovered_at TEXT NOT NULL,
  UNIQUE(wallet_id, chain_id, token_addr)
);

CREATE TABLE IF NOT EXISTS jobs (
  id          INTEGER PRIMARY KEY,
  wallet_id   INTEGER NOT NULL REFERENCES wallets(id),
  token_addr  TEXT NOT NULL,
  symbol      TEXT,
  amount_in   TEXT,
  status      TEXT NOT NULL DEFAULT 'PENDING',
    -- PENDING -> DISCOVERED -> QUOTED -> APPROVED -> DEPOSITED -> BRIDGED -> TRANSFERRED -> DONE
    -- ожидание: WAITING_TARGET (нет target_address — задача в очереди, ончейн-действий нет)
    -- терминальные/особые: FAILED, SKIPPED, REFUNDED, NEEDS_BROWSER
  request_id  TEXT,
  amount_out  TEXT,
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  error_class TEXT,                           -- retryable | permanent | manual
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE(wallet_id, token_addr)
);

CREATE TABLE IF NOT EXISTS tx_log (
  id          INTEGER PRIMARY KEY,
  job_id      INTEGER NOT NULL REFERENCES jobs(id),
  step        TEXT NOT NULL,                  -- approve | deposit | transfer
  chain_id    INTEGER NOT NULL,
  tx_hash     TEXT,
  nonce       INTEGER,
  status      TEXT NOT NULL DEFAULT 'sent',   -- sent | confirmed | reverted
  gas_used    TEXT,
  raw_request TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE(job_id, step, tx_hash)
);

CREATE TABLE IF NOT EXISTS sync_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- Каталог протоколов (растёт по мере обнаружения новых на любом кошельке).
-- Дедуп по debank_id (напр. 'abs_zerolend').
CREATE TABLE IF NOT EXISTS protocols (
  id          INTEGER PRIMARY KEY,
  debank_id   TEXT NOT NULL UNIQUE,          -- id из DeBank (abs_zerolend, abs_aborean, ...)
  name        TEXT,                          -- ZeroLend, Aborean Finance, ...
  chain       TEXT,                          -- abs, base, eth, ...
  site_url    TEXT,
  first_seen  TEXT NOT NULL,                 -- когда протокол впервые встретился
  last_seen   TEXT NOT NULL
);

-- Использование протокола конкретным кошельком (снимок).
CREATE TABLE IF NOT EXISTS wallet_protocols (
  id          INTEGER PRIMARY KEY,
  wallet_id   INTEGER NOT NULL REFERENCES wallets(id),
  protocol_id INTEGER NOT NULL REFERENCES protocols(id),
  agw_address TEXT,                          -- на каком адресе найдено (Privy/AGW)
  chain       TEXT,
  net_usd     TEXT,                          -- суммарная стоимость позиции в протоколе (USD, строкой)
  item_types  TEXT,                          -- 'Lending,Liquidity Pool' — типы позиций
  raw         TEXT,                          -- сырой JSON протокола (для деталей)
  checked_at  TEXT NOT NULL,
  UNIQUE(wallet_id, protocol_id)
);

-- Токены кошелька в разрезе чейнов (по DeBank), опц. полезно для отчёта.
CREATE TABLE IF NOT EXISTS wallet_tokens_debank (
  id          INTEGER PRIMARY KEY,
  wallet_id   INTEGER NOT NULL REFERENCES wallets(id),
  chain       TEXT,
  symbol      TEXT,
  amount      TEXT,
  usd_value   TEXT,
  checked_at  TEXT NOT NULL,
  UNIQUE(wallet_id, chain, symbol)
);

CREATE INDEX IF NOT EXISTS idx_wp_wallet ON wallet_protocols(wallet_id);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_txlog_job ON tx_log(job_id, step);
