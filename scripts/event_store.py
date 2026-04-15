"""
event_store.py — Persistent EAS event history in SQLite

Stores each decoded EAS message in a local SQLite database.
The file grows ~2 KB per event; with RWT tests every 3 hours, a year takes
≈ 6 MB — completely manageable.

Usage:
    store = EventStore('alerts_history.db')
    store.save(result_dict)                       # from decoder_loop
    events = store.query(from_dt, to_dt)          # from web API
    total  = store.count()
"""

import sqlite3
import json
import datetime
import threading
import logging

log = logging.getLogger(__name__)

# Maximum rows returned by query (protection against very broad ranges)
QUERY_LIMIT = 2000


class EventStore:
    """
    Thread-safe SQLite store for EAS messages.

    Indexed columns: received_at, EEE, COUNTRY.
    The rest of the message fields are serialized in extra_json
    (transmitter, TTTT, JJJHHMM, etc.).
    """

    # Schema v1
    _DDL = """
    CREATE TABLE IF NOT EXISTS events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at  TEXT NOT NULL,          -- ISO 8601, local time
        EEE          TEXT,
        ORG          TEXT,
        COUNTRY      TEXT,
        event_name   TEXT,
        organization TEXT,
        LLLLLLLL     TEXT,
        PSSCCC       TEXT,                   -- JSON list
        start_time   TEXT,
        end_time     TEXT,
        length       TEXT,
        seconds      INTEGER,
        raw_message  TEXT,
        extra_json   TEXT                    -- transmitter, TTTT, JJJHHMM, ...
    );
    CREATE INDEX IF NOT EXISTS idx_received_at ON events (received_at);
    CREATE INDEX IF NOT EXISTS idx_eee         ON events (EEE);
    CREATE INDEX IF NOT EXISTS idx_country     ON events (COUNTRY);
    """

    # Fields that go into dedicated columns (not in extra_json)
    _DEDICATED = frozenset({
        'EEE', 'ORG', 'COUNTRY', 'event', 'organization',
        'LLLLLLLL', 'PSSCCC_list', 'start', 'end', 'length',
        'seconds', 'MESSAGE', 'received_at',
    })

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock   = threading.Lock()
        self._init_db()
        log.info(f'EventStore started: {db_path}  ({self.count()} events)')

    # ------------------------------------------------------------------
    # Internal lifecycle
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.executescript(self._DDL)
            except Exception as e:
                log.error(f'EventStore._init_db: {e}')

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save(self, msg: dict) -> None:
        """
        Persist a decoded EAS message.
        Thread-safe, call from decoder_loop.

        Parameters
        ----------
        msg : dict
            The dict returned by _decode_frame(), enriched with
            'received_at' (ISO 8601) before calling this method.
        """
        extra = {k: v for k, v in msg.items() if k not in self._DEDICATED}

        row = (
            msg.get('received_at') or datetime.datetime.now().isoformat(),
            msg.get('EEE', ''),
            msg.get('ORG', ''),
            msg.get('COUNTRY', ''),
            msg.get('event', ''),
            msg.get('organization', ''),
            msg.get('LLLLLLLL', ''),
            json.dumps(msg.get('PSSCCC_list', []), ensure_ascii=False),
            msg.get('start', ''),
            msg.get('end', ''),
            msg.get('length', ''),
            int(msg.get('seconds') or 0),
            msg.get('MESSAGE', ''),
            json.dumps(extra, default=str, ensure_ascii=False),
        )

        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        """INSERT INTO events
                           (received_at, EEE, ORG, COUNTRY, event_name,
                            organization, LLLLLLLL, PSSCCC, start_time,
                            end_time, length, seconds, raw_message, extra_json)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        row,
                    )
            except Exception as e:
                log.error(f'EventStore.save: {e}')

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def query(self,
              from_dt: datetime.datetime,
              to_dt:   datetime.datetime) -> list[dict]:
        """
        Returns up to QUERY_LIMIT events in the range [from_dt, to_dt],
        sorted newest first.
        Thread-safe.
        """
        with self._lock:
            try:
                with self._connect() as conn:
                    cur = conn.execute(
                        """SELECT id, received_at, EEE, ORG, COUNTRY,
                                  event_name, organization, LLLLLLLL,
                                  PSSCCC, start_time, end_time, length,
                                  seconds, raw_message, extra_json
                           FROM events
                           WHERE received_at >= ? AND received_at <= ?
                           ORDER BY received_at DESC
                           LIMIT ?""",
                        (from_dt.isoformat(), to_dt.isoformat(), QUERY_LIMIT),
                    )
                    return [self._row_to_dict(row) for row in cur.fetchall()]
            except Exception as e:
                log.error(f'EventStore.query: {e}')
                return []

    def count(self) -> int:
        """Total stored events."""
        with self._lock:
            try:
                with self._connect() as conn:
                    return conn.execute(
                        'SELECT COUNT(*) FROM events'
                    ).fetchone()[0]
            except Exception:
                return 0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def delete_by_timestamp(self, received_at: str) -> bool:
        """
        Deletes an event by its received_at timestamp.
        Returns True if a row was deleted (or False if it didn't exist or error).
        """
        with self._lock:
            try:
                with self._connect() as conn:
                    cur = conn.execute(
                        "DELETE FROM events WHERE received_at = ?",
                        (received_at,)
                    )
                    return cur.rowcount > 0
            except Exception as e:
                log.error(f'EventStore.delete_by_timestamp: {e}')
                return False

    def delete_many(self, timestamps: list[str]) -> int:
        """
        Deletes multiple events in a single SQL statement.
        Returns the number of rows actually deleted.
        """
        if not timestamps:
            return 0
        placeholders = ','.join('?' * len(timestamps))
        with self._lock:
            try:
                with self._connect() as conn:
                    cur = conn.execute(
                        f"DELETE FROM events WHERE received_at IN ({placeholders})",
                        timestamps,
                    )
                    return cur.rowcount
            except Exception as e:
                log.error(f'EventStore.delete_many: {e}')
                return 0

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        # Deserialize PSSCCC_list
        try:
            d['PSSCCC_list'] = json.loads(d.pop('PSSCCC', '[]') or '[]')
        except Exception:
            d['PSSCCC_list'] = []
        # Merge extra_json back to root dict
        try:
            extra = json.loads(d.pop('extra_json', '{}') or '{}')
            d.update(extra)
        except Exception:
            d.pop('extra_json', None)
        # Rename columns to original pipeline keys
        d['event']   = d.pop('event_name',  '')
        d['start']   = d.pop('start_time',  '')
        d['end']     = d.pop('end_time',    '')
        d['MESSAGE'] = d.pop('raw_message', '')
        return d
