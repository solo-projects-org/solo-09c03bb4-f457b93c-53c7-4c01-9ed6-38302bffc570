"""SQLite storage layer.

The database is the only piece of shared state between the web instances, so
all mutations go through ``BEGIN IMMEDIATE`` transactions.  That serialises
version allocation, run creation and (most importantly) publication across
processes.  Readers use WAL snapshots and never block writers.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS map_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    feature_count   INTEGER NOT NULL,
    repaired_count  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_map_versions_active
    ON map_versions (is_active) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS compartments (
    map_version_id  INTEGER NOT NULL REFERENCES map_versions(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    code            TEXT NOT NULL,
    geom            BLOB NOT NULL,
    minx REAL NOT NULL, miny REAL NOT NULL, maxx REAL NOT NULL, maxy REAL NOT NULL,
    PRIMARY KEY (map_version_id, seq),
    UNIQUE (map_version_id, code)
);

CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    filename        TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    published_run_id INTEGER
);

CREATE TABLE IF NOT EXISTS hotspots (
    batch_id        INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    source_row      INTEGER NOT NULL,
    hotspot_no      TEXT NOT NULL,
    lon             REAL NOT NULL,
    lat             REAL NOT NULL,
    observed_at_utc TEXT NOT NULL,
    raw_json        TEXT NOT NULL,
    PRIMARY KEY (batch_id, seq)
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    map_version_id  INTEGER NOT NULL REFERENCES map_versions(id),
    status          TEXT NOT NULL,           -- queued|running|succeeded|failed|superseded
    error           TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    heartbeat_at    TEXT,
    attempt_token   TEXT,
    published       INTEGER NOT NULL DEFAULT 0,
    matched_count   INTEGER NOT NULL DEFAULT 0,
    conflict_count  INTEGER NOT NULL DEFAULT 0,
    unmatched_count INTEGER NOT NULL DEFAULT 0,
    total_count     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_runs_batch ON runs (batch_id, id);
CREATE INDEX IF NOT EXISTS ix_runs_status ON runs (status);

CREATE TABLE IF NOT EXISTS results (
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    hotspot_seq     INTEGER NOT NULL,
    status          TEXT NOT NULL,
    reason_kind     TEXT NOT NULL,
    reason          TEXT NOT NULL,
    codes_json      TEXT NOT NULL,
    PRIMARY KEY (run_id, hotspot_seq)
);

-- A supply manifest is an immutable snapshot: which run (and therefore which
-- map version) each batch contributed at creation time.  Later publications or
-- uploads only make it stale; they never change its rows.
CREATE TABLE IF NOT EXISTS manifests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    map_version_id  INTEGER NOT NULL REFERENCES map_versions(id),
    batch_count     INTEGER NOT NULL,
    total_count     INTEGER NOT NULL,
    matched_count   INTEGER NOT NULL,
    conflict_count  INTEGER NOT NULL,
    unmatched_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS manifest_items (
    manifest_id     INTEGER NOT NULL REFERENCES manifests(id) ON DELETE CASCADE,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    run_id          INTEGER NOT NULL REFERENCES runs(id),
    map_version_id  INTEGER NOT NULL REFERENCES map_versions(id),
    row_count       INTEGER NOT NULL,
    matched_count   INTEGER NOT NULL,
    conflict_count  INTEGER NOT NULL,
    unmatched_count INTEGER NOT NULL,
    PRIMARY KEY (manifest_id, batch_id)
);

CREATE INDEX IF NOT EXISTS ix_manifest_items_run ON manifest_items (run_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(8):
        conn = connect(db_path)
        try:
            # Two containers may boot at exactly the same time; WAL mode
            # switching can briefly return SQLITE_BUSY, hence the retry.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            return
        except sqlite3.OperationalError as exc:
            last_error = exc
            time.sleep(0.1 * (attempt + 1))
        finally:
            conn.close()
    assert last_error is not None
    raise last_error


@contextmanager
def connection(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A cross-process exclusive transaction.

    ``BEGIN IMMEDIATE`` takes SQLite's RESERVED lock before any read, so two
    app instances cannot interleave a version swap with a publication check.
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A consistent read snapshot (WAL allows concurrent writers)."""

    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("ROLLBACK")


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def dump_codes(codes: list[str]) -> str:
    return json.dumps(codes, ensure_ascii=False)


def load_codes(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
        return [str(item) for item in value] if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []
