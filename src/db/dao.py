"""DAO поверх SQLite: идемпотентные upsert-ы, thread-local соединения."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from src.db.models import Job, Wallet

_SCHEMA = Path(__file__).parent / "schema.sql"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Dao:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn().executescript(_SCHEMA.read_text(encoding="utf-8"))
        self._conn().commit()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    # ---------- sync_meta ----------

    def get_meta(self, key: str) -> str | None:
        row = self._conn().execute("SELECT value FROM sync_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        c = self._conn()
        c.execute(
            "INSERT INTO sync_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        c.commit()

    # ---------- wallets ----------

    def upsert_wallet(
        self,
        address: str,
        target_address: str | None,
        proxy: str | None,
        adspower_profile: str | None,
        label: str | None,
        enabled: bool,
    ) -> int:
        """Идемпотентный upsert по address. Прогресс jobs не трогаем."""
        c = self._conn()
        now = _now()
        c.execute(
            """
            INSERT INTO wallets(address, target_address, proxy, proxy_source,
                                adspower_profile, label, enabled, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(address) DO UPDATE SET
              target_address=excluded.target_address,
              adspower_profile=excluded.adspower_profile,
              label=excluded.label,
              enabled=excluded.enabled,
              updated_at=excluded.updated_at,
              -- прокси из XLSX имеет приоритет; назначенную из пула не затираем пустотой
              proxy=CASE WHEN excluded.proxy IS NOT NULL THEN excluded.proxy ELSE wallets.proxy END,
              proxy_source=CASE WHEN excluded.proxy IS NOT NULL THEN 'xlsx' ELSE wallets.proxy_source END
            """,
            (
                address,
                target_address,
                proxy,
                "xlsx" if proxy else None,
                adspower_profile,
                label,
                int(enabled),
                now,
                now,
            ),
        )
        c.commit()
        row = c.execute("SELECT id FROM wallets WHERE address=?", (address,)).fetchone()
        return int(row["id"])

    def delete_wallets_not_in(self, addresses: list[str]) -> int:
        """XLSX — источник истины: кошельки, пропавшие из файла, удаляются вместе с их
        задачами/балансами/логами транзакций (каскад вручную — FK-cascade в SQLite off by default)."""
        c = self._conn()
        if not addresses:
            return 0
        placeholders = ",".join("?" for _ in addresses)
        rows = c.execute(
            f"SELECT id FROM wallets WHERE address NOT IN ({placeholders})", addresses
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        c.execute("BEGIN IMMEDIATE")
        for wid in ids:
            c.execute(
                "DELETE FROM tx_log WHERE job_id IN (SELECT id FROM jobs WHERE wallet_id=?)", (wid,)
            )
            c.execute("DELETE FROM jobs WHERE wallet_id=?", (wid,))
            c.execute("DELETE FROM token_balances WHERE wallet_id=?", (wid,))
            c.execute("DELETE FROM wallets WHERE id=?", (wid,))
        c.commit()
        return len(ids)

    def get_wallets(self, enabled_only: bool = True) -> list[Wallet]:
        q = "SELECT * FROM wallets"
        if enabled_only:
            q += " WHERE enabled=1"
        rows = self._conn().execute(q + " ORDER BY id").fetchall()
        return [self._wallet_from(r) for r in rows]

    def get_wallet(self, address: str) -> Wallet | None:
        row = self._conn().execute("SELECT * FROM wallets WHERE address=?", (address,)).fetchone()
        return self._wallet_from(row) if row else None

    @staticmethod
    def _wallet_from(r: sqlite3.Row) -> Wallet:
        return Wallet(
            id=r["id"],
            address=r["address"],
            target_address=r["target_address"],
            proxy=r["proxy"],
            proxy_source=r["proxy_source"],
            proxy_status=r["proxy_status"] or "unknown",
            adspower_profile=r["adspower_profile"],
            label=r["label"],
            enabled=bool(r["enabled"]),
        )

    def set_wallet_proxy(self, wallet_id: int, proxy: str, source: str, status: str = "unknown") -> None:
        c = self._conn()
        c.execute(
            "UPDATE wallets SET proxy=?, proxy_source=?, proxy_status=?, updated_at=? WHERE id=?",
            (proxy, source, status, _now(), wallet_id),
        )
        c.commit()

    def set_wallet_proxy_status(self, wallet_id: int, status: str) -> None:
        c = self._conn()
        c.execute(
            "UPDATE wallets SET proxy_status=?, updated_at=? WHERE id=?", (status, _now(), wallet_id)
        )
        c.commit()

    # ---------- token_balances ----------

    def upsert_balance(
        self,
        wallet_id: int,
        chain_id: int,
        token_addr: str,
        symbol: str | None,
        decimals: int | None,
        raw_balance: int,
        routable: bool,
    ) -> None:
        c = self._conn()
        c.execute(
            """
            INSERT INTO token_balances(wallet_id, chain_id, token_addr, symbol, decimals,
                                       raw_balance, routable, discovered_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(wallet_id, chain_id, token_addr) DO UPDATE SET
              symbol=excluded.symbol, decimals=excluded.decimals,
              raw_balance=excluded.raw_balance, routable=excluded.routable,
              discovered_at=excluded.discovered_at
            """,
            (wallet_id, chain_id, token_addr.lower(), symbol, decimals, str(raw_balance), int(routable), _now()),
        )
        c.commit()

    # ---------- jobs ----------

    def ensure_job(self, wallet_id: int, token_addr: str, symbol: str | None) -> Job:
        """Создать job, если его нет (идемпотентно). Существующий прогресс не трогаем."""
        c = self._conn()
        now = _now()
        c.execute(
            """
            INSERT INTO jobs(wallet_id, token_addr, symbol, status, created_at, updated_at)
            VALUES(?,?,?, 'DISCOVERED', ?, ?)
            ON CONFLICT(wallet_id, token_addr) DO NOTHING
            """,
            (wallet_id, token_addr.lower(), symbol, now, now),
        )
        c.commit()
        return self.get_job(wallet_id, token_addr)  # type: ignore[return-value]

    def get_job(self, wallet_id: int, token_addr: str) -> Job | None:
        row = self._conn().execute(
            "SELECT * FROM jobs WHERE wallet_id=? AND token_addr=?", (wallet_id, token_addr.lower())
        ).fetchone()
        return self._job_from(row) if row else None

    def get_jobs(self, wallet_id: int | None = None, statuses: list[str] | None = None) -> list[Job]:
        q = "SELECT * FROM jobs WHERE 1=1"
        args: list = []
        if wallet_id is not None:
            q += " AND wallet_id=?"
            args.append(wallet_id)
        if statuses:
            q += f" AND status IN ({','.join('?' for _ in statuses)})"
            args.extend(statuses)
        rows = self._conn().execute(q + " ORDER BY id", args).fetchall()
        return [self._job_from(r) for r in rows]

    @staticmethod
    def _job_from(r: sqlite3.Row) -> Job:
        return Job(
            id=r["id"],
            wallet_id=r["wallet_id"],
            token_addr=r["token_addr"],
            symbol=r["symbol"],
            amount_in=r["amount_in"],
            status=r["status"],
            request_id=r["request_id"],
            amount_out=r["amount_out"],
            attempts=r["attempts"],
            last_error=r["last_error"],
        )

    def update_job(self, job_id: int, **fields) -> None:
        """Обновление полей job в одной транзакции."""
        allowed = {"status", "amount_in", "amount_out", "request_id", "last_error", "error_class", "symbol"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"bad job field: {k}")
            sets.append(f"{k}=?")
            args.append(v)
        sets.append("updated_at=?")
        args.append(_now())
        args.append(job_id)
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", args)
        c.commit()

    def bump_attempts(self, job_id: int) -> None:
        c = self._conn()
        c.execute("UPDATE jobs SET attempts=attempts+1, updated_at=? WHERE id=?", (_now(), job_id))
        c.commit()

    def reset_failed_jobs(self, wallet_id: int | None = None) -> int:
        """retry: FAILED -> откат на DISCOVERED (шаги сами продолжат с последнего успешного)."""
        c = self._conn()
        q = "UPDATE jobs SET status='DISCOVERED', last_error=NULL, error_class=NULL, updated_at=? WHERE status='FAILED'"
        args: list = [_now()]
        if wallet_id is not None:
            q += " AND wallet_id=?"
            args.append(wallet_id)
        cur = c.execute(q, args)
        c.commit()
        return cur.rowcount

    # ---------- tx_log ----------

    def log_tx(
        self,
        job_id: int,
        step: str,
        chain_id: int,
        tx_hash: str | None,
        nonce: int | None,
        status: str = "sent",
        raw_request: str | None = None,
        gas_used: int | None = None,
    ) -> None:
        c = self._conn()
        c.execute(
            """
            INSERT INTO tx_log(job_id, step, chain_id, tx_hash, nonce, status, gas_used, raw_request, created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id, step, tx_hash) DO UPDATE SET
              status=excluded.status, gas_used=excluded.gas_used
            """,
            (job_id, step, chain_id, tx_hash, nonce, status, str(gas_used) if gas_used else None, raw_request, _now()),
        )
        c.commit()

    def get_tx(self, job_id: int, step: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM tx_log WHERE job_id=? AND step=? ORDER BY id DESC LIMIT 1", (job_id, step)
        ).fetchone()

    # ---------- отчёт ----------

    def status_summary(self) -> list[sqlite3.Row]:
        return self._conn().execute(
            """
            SELECT w.address, w.label, j.symbol, j.token_addr, j.status,
                   j.amount_in, j.amount_out, j.request_id, j.last_error
            FROM jobs j JOIN wallets w ON w.id = j.wallet_id
            ORDER BY w.id, j.id
            """
        ).fetchall()
