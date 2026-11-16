"""Application services: map versions, batches, runs, publication, export.

Consistency model
-----------------
* Every run is pinned to one ``map_versions`` row and reads that immutable
  snapshot.  Compartment geometry is never updated in place.
* Exactly one map version is active at a time (partial unique index).
* A run may publish only if, inside a ``BEGIN IMMEDIATE`` transaction:
    - it is still allowed to publish (not failed/cancelled),
    - its map version is still the active one,
    - no newer run has already published for the same batch.
  A late-finishing older computation therefore cannot overwrite a newer one,
  and a computation against a replaced map can never be published.
* Failed or superseded runs never touch ``batches.published_run_id``, so the
  previous downloadable detail survives.
* Downloads resolve ``published_run_id`` once and then read only that run's
  result rows, so a concurrent republication cannot produce a mixed file.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import db
from .attribution import (
    CONFLICT,
    MATCHED,
    REASON_LABELS,
    STATUS_LABELS,
    UNMATCHED,
    Attribution,
    Attributor,
    CompartmentGeometry,
    HotspotPoint,
)
from .parsing import (
    Compartment,
    InputError,
    Hotspot,
    geometry_to_wkb,
    parse_compartment_geojson,
    parse_hotspots_csv,
    sha256_bytes,
    wkb_to_geometry,
)

log = logging.getLogger("forest.service")

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_SUPERSEDED = "superseded"

# Internal outcomes of a computation attempt.
PUBLISHED = "published"
SUPERSEDED_OLD_MAP = "superseded_old_map"
SUPERSEDED_NEWER = "superseded_newer"
ABORTED = "aborted"

RUN_STATUS_LABELS = {
    STATUS_QUEUED: "排队中",
    STATUS_RUNNING: "计算中",
    STATUS_SUCCEEDED: "已完成",
    STATUS_FAILED: "失败",
    STATUS_SUPERSEDED: "已作废",
}

RESULT_COLUMNS = [
    "来源文件",
    "来源行号",
    "热点号",
    "经度",
    "纬度",
    "UTC时刻",
    "归属状态",
    "原因类型",
    "原因说明",
    "林班号",
    "候选林班号",
    "林班底图版本",
    "计算记录号",
]


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class MapInfo:
    id: int
    filename: str
    sha256: str
    feature_count: int
    repaired_count: int
    created_at: str
    is_active: bool


def _row_to_map(row: sqlite3.Row) -> MapInfo:
    return MapInfo(
        id=row["id"],
        filename=row["filename"],
        sha256=row["sha256"],
        feature_count=row["feature_count"],
        repaired_count=row["repaired_count"],
        created_at=row["created_at"],
        is_active=bool(row["is_active"]),
    )


def get_active_map(conn: sqlite3.Connection) -> MapInfo | None:
    row = conn.execute("SELECT * FROM map_versions WHERE is_active = 1").fetchone()
    return _row_to_map(row) if row else None


def get_map(conn: sqlite3.Connection, map_id: int) -> MapInfo | None:
    row = conn.execute("SELECT * FROM map_versions WHERE id = ?", (map_id,)).fetchone()
    return _row_to_map(row) if row else None


def list_maps(conn: sqlite3.Connection, limit: int = 20) -> list[MapInfo]:
    rows = conn.execute(
        "SELECT * FROM map_versions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_map(row) for row in rows]


def publish_map(db_path: str, data: bytes, filename: str) -> tuple[MapInfo, list[int]]:
    """Parse and atomically activate a new compartment map version.

    Returns the new version and ids of batches whose previously published
    result was produced against an older version (they can be recomputed).
    """

    compartments, repaired = parse_compartment_geojson(data, filename)
    digest = sha256_bytes(data)
    created_at = db.utc_now()
    with db.connection(db_path) as conn:
        with db.write_tx(conn):
            conn.execute("UPDATE map_versions SET is_active = 0 WHERE is_active = 1")
            cur = conn.execute(
                """
                INSERT INTO map_versions
                    (filename, sha256, feature_count, repaired_count, created_at, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (filename, digest, len(compartments), repaired, created_at),
            )
            version_id = int(cur.lastrowid)
            conn.executemany(
                """
                INSERT INTO compartments
                    (map_version_id, seq, code, geom, minx, miny, maxx, maxy)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        version_id,
                        item.seq,
                        item.code,
                        geometry_to_wkb(item.geometry),
                        item.minx,
                        item.miny,
                        item.maxx,
                        item.maxy,
                    )
                    for item in compartments
                ],
            )
            stale_rows = conn.execute(
                """
                SELECT DISTINCT b.id
                FROM batches b
                JOIN runs r ON r.id = b.published_run_id
                WHERE r.map_version_id != ?
                ORDER BY b.id
                """,
                (version_id,),
            ).fetchall()
            info = _row_to_map(
                conn.execute("SELECT * FROM map_versions WHERE id = ?", (version_id,)).fetchone()
            )
    return info, [int(row["id"]) for row in stale_rows]


def _insert_hotspots(conn: sqlite3.Connection, batch_id: int, hotspots: Sequence[Hotspot]) -> None:
    conn.executemany(
        """
        INSERT INTO hotspots (batch_id, seq, source_row, hotspot_no, lon, lat, observed_at_utc, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                batch_id,
                item.seq,
                item.source_row,
                item.hotspot_no,
                item.lon,
                item.lat,
                item.observed_at_utc,
                json.dumps(item.raw, ensure_ascii=False),
            )
            for item in hotspots
        ],
    )


def create_batch(db_path: str, data: bytes, filename: str) -> tuple[int, int | None]:
    """Store a hotspot batch and (if a map exists) enqueue its first run."""

    hotspots = parse_hotspots_csv(data, filename)
    digest = sha256_bytes(data)
    created_at = db.utc_now()
    with db.connection(db_path) as conn:
        with db.write_tx(conn):
            cur = conn.execute(
                """
                INSERT INTO batches (name, filename, sha256, row_count, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (Path(filename).stem or "热点批次", filename, digest, len(hotspots), created_at),
            )
            batch_id = int(cur.lastrowid)
            _insert_hotspots(conn, batch_id, hotspots)
            active = get_active_map(conn)
            run_id = None
            if active is not None:
                run_id = _insert_run(conn, batch_id, active.id)
    return batch_id, run_id


def enqueue_recompute(db_path: str, batch_id: int) -> int:
    """Queue a run against the currently active map version.

    If the newest run for this batch already uses the active version and is
    still queued/running, it is reused rather than duplicating the work.
    """

    with db.connection(db_path) as conn:
        with db.write_tx(conn):
            batch = conn.execute("SELECT id FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if batch is None:
                raise ServiceError("热点批次不存在")
            active = get_active_map(conn)
            if active is None:
                raise ServiceError("尚未上传林班底图，无法计算")
            latest = conn.execute(
                "SELECT id, status, map_version_id FROM runs WHERE batch_id = ? ORDER BY id DESC LIMIT 1",
                (batch_id,),
            ).fetchone()
            if (
                latest is not None
                and latest["map_version_id"] == active.id
                and latest["status"] in (STATUS_QUEUED, STATUS_RUNNING)
            ):
                return int(latest["id"])
            return _insert_run(conn, batch_id, active.id)


def _insert_run(conn: sqlite3.Connection, batch_id: int, map_version_id: int) -> int:
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM hotspots WHERE batch_id = ?", (batch_id,)
    ).fetchone()["n"]
    cur = conn.execute(
        """
        INSERT INTO runs (batch_id, map_version_id, status, created_at, total_count)
        VALUES (?, ?, ?, ?, ?)
        """,
        (batch_id, map_version_id, STATUS_QUEUED, db.utc_now(), total),
    )
    return int(cur.lastrowid)


def claim_next_run(db_path: str) -> tuple[int, str] | None:
    """Atomically take the oldest queued run; safe across app instances.

    Returns ``(run_id, attempt_token)``.  The token identifies this specific
    execution attempt: if a stalled worker is recovered and the run re-claimed
    elsewhere, the stale worker's late writes are rejected instead of
    corrupting the new attempt or marking it failed.
    """

    with db.connection(db_path) as conn:
        with db.write_tx(conn):
            row = conn.execute(
                "SELECT id FROM runs WHERE status = ? ORDER BY id LIMIT 1", (STATUS_QUEUED,)
            ).fetchone()
            if row is None:
                return None
            run_id = int(row["id"])
            now = db.utc_now()
            token = uuid.uuid4().hex
            updated = conn.execute(
                """
                UPDATE runs SET status = ?, started_at = ?, heartbeat_at = ?, attempt_token = ?
                WHERE id = ? AND status = ?
                """,
                (STATUS_RUNNING, now, now, token, run_id, STATUS_QUEUED),
            ).rowcount
            if updated != 1:
                return None
            return run_id, token


def _load_run_inputs(
    db_path: str, run_id: int
) -> tuple[sqlite3.Row, list[HotspotPoint], list[CompartmentGeometry], MapInfo]:
    """Read one consistent snapshot of hotspots + the pinned map version."""

    with db.connection(db_path) as conn:
        with db.read_tx(conn):
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise ServiceError("计算记录不存在")
            batch = conn.execute(
                "SELECT * FROM batches WHERE id = ?", (run["batch_id"],)
            ).fetchone()
            map_info = get_map(conn, int(run["map_version_id"]))
            if map_info is None:
                raise ServiceError("计算所用的林班底图版本不存在")
            hotspot_rows = conn.execute(
                """
                SELECT seq, source_row, hotspot_no, lon, lat, observed_at_utc
                FROM hotspots WHERE batch_id = ? ORDER BY seq
                """,
                (run["batch_id"],),
            ).fetchall()
            compartments = [
                CompartmentGeometry(
                    seq=int(row["seq"]),
                    code=row["code"],
                    geometry=wkb_to_geometry(row["geom"]),
                )
                for row in conn.execute(
                    "SELECT seq, code, geom FROM compartments WHERE map_version_id = ? ORDER BY seq",
                    (map_info.id,),
                )
            ]
            hotspots = [HotspotPoint(*row) for row in hotspot_rows]
    return run, hotspots, compartments, map_info


def execute_run(db_path: str, run_id: int, attempt_token: str | None = None, chunk_size: int = 2000) -> bool:
    """Compute and publish a claimed run. Returns True if published.

    ``attempt_token`` is the token returned by :func:`claim_next_run`.  When it
    is supplied, every write is conditional on the run still belonging to this
    attempt, so a worker that was stalled and then recovered cannot delete or
    overwrite the results of the replacement attempt.  Passing ``None`` is only
    for tests and for callers that hold the run exclusively.
    """

    try:
        run, hotspots, compartments, map_info = _load_run_inputs(db_path, run_id)
        attributor = Attributor(compartments)
        counts, total, outcome = _compute_rows(
            db_path, run_id, attempt_token, hotspots, attributor, chunk_size
        )
        if outcome == PUBLISHED:
            return True
        _discard_results(db_path, run_id, attempt_token)
        if outcome == SUPERSEDED_OLD_MAP:
            if _mark_superseded(db_path, run_id, attempt_token):
                _requeue_against_active_map(db_path, int(run["batch_id"]))
        elif outcome == SUPERSEDED_NEWER:
            _mark_superseded(db_path, run_id, attempt_token)
        return False
    except Exception as exc:  # noqa: BLE001 - failure must be recorded, not raised
        log.exception("run %s failed", run_id)
        if _mark_failed(db_path, run_id, str(exc) or exc.__class__.__name__, attempt_token):
            _discard_results(db_path, run_id, attempt_token)
        return False


def _token_clause(attempt_token: str | None) -> tuple[str, tuple[Any, ...]]:
    """SQL fragment pinning a mutation to one attempt.

    ``None`` means the caller holds the run exclusively (tests, maintenance)
    and no attempt check is applied.
    """

    if attempt_token is None:
        return "", ()
    return " AND attempt_token = ?", (attempt_token,)


def _owns_attempt(
    conn: sqlite3.Connection, run_id: int, attempt_token: str | None, *, require_running: bool
) -> bool:
    row = conn.execute(
        "SELECT status, attempt_token FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return False
    if require_running and row["status"] != STATUS_RUNNING:
        return False
    if attempt_token is not None and row["attempt_token"] != attempt_token:
        return False
    return True


def _compute_rows(
    db_path: str,
    run_id: int,
    attempt_token: str | None,
    hotspots: Sequence[HotspotPoint],
    attributor: Attributor,
    chunk_size: int,
) -> tuple[dict[str, int], int, str]:
    counts = {MATCHED: 0, CONFLICT: 0, UNMATCHED: 0}
    total = len(hotspots)
    last_heartbeat = time.monotonic()
    for start in range(0, total, chunk_size):
        chunk = hotspots[start : start + chunk_size]
        rows: list[tuple[Any, ...]] = []
        for hotspot in chunk:
            attribution = attributor.attribute_one(hotspot)
            counts[attribution.status] += 1
            rows.append(
                (
                    run_id,
                    hotspot.seq,
                    attribution.status,
                    attribution.reason_kind,
                    attribution.reason,
                    db.dump_codes(list(attribution.codes)),
                )
            )
        with db.connection(db_path) as conn:
            with db.write_tx(conn):
                if not _owns_attempt(conn, run_id, attempt_token, require_running=True):
                    return counts, total, ABORTED
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO results
                        (run_id, hotspot_seq, status, reason_kind, reason, codes_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                if time.monotonic() - last_heartbeat > 5:
                    clause, params = _token_clause(attempt_token)
                    conn.execute(
                        f"UPDATE runs SET heartbeat_at = ? WHERE id = ?{clause}",
                        (db.utc_now(), run_id, *params),
                    )
                    last_heartbeat = time.monotonic()
    return counts, total, _publish_run(db_path, run_id, attempt_token, counts, total)


def _publish_run(
    db_path: str,
    run_id: int,
    attempt_token: str | None,
    counts: dict[str, int],
    total: int,
) -> str:
    with db.connection(db_path) as conn:
        with db.write_tx(conn):
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None or not _owns_attempt(
                conn, run_id, attempt_token, require_running=True
            ):
                return ABORTED
            active = get_active_map(conn)
            if active is None or active.id != run["map_version_id"]:
                # Map was replaced while this run was computing. Publishing it
                # would break "one published detail = one map version".
                return SUPERSEDED_OLD_MAP
            batch = conn.execute(
                "SELECT published_run_id FROM batches WHERE id = ?", (run["batch_id"],)
            ).fetchone()
            if batch is not None and batch["published_run_id"] is not None:
                previous = conn.execute(
                    "SELECT id FROM runs WHERE id = ?", (batch["published_run_id"],)
                ).fetchone()
                if previous is not None and int(previous["id"]) > run_id:
                    # A newer computation already published; late old result loses.
                    return SUPERSEDED_NEWER
            now = db.utc_now()
            clause, params = _token_clause(attempt_token)
            success = conn.execute(
                f"""
                UPDATE runs SET status = ?, published = 1, finished_at = ?, error = NULL,
                    matched_count = ?, conflict_count = ?, unmatched_count = ?, total_count = ?
                WHERE id = ? AND status = ? AND published = 0{clause}
                """,
                (
                    STATUS_SUCCEEDED,
                    now,
                    counts[MATCHED],
                    counts[CONFLICT],
                    counts[UNMATCHED],
                    total,
                    run_id,
                    STATUS_RUNNING,
                    *params,
                ),
            ).rowcount
            if success != 1:
                return ABORTED
            conn.execute(
                "UPDATE batches SET published_run_id = ? WHERE id = ?", (run_id, run["batch_id"])
            )
            return PUBLISHED


def _requeue_against_active_map(db_path: str, batch_id: int) -> int | None:
    """After a map swap invalidated a computation, retry against the new map.

    Skips if a run for this batch is already queued/running on the active
    version, so two instances racing to requeue create only one run.
    """

    with db.connection(db_path) as conn:
        with db.write_tx(conn):
            if conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is None:
                return None
            active = get_active_map(conn)
            if active is None:
                return None
            existing = conn.execute(
                """
                SELECT 1 FROM runs
                WHERE batch_id = ? AND map_version_id = ? AND status IN (?, ?)
                LIMIT 1
                """,
                (batch_id, active.id, STATUS_QUEUED, STATUS_RUNNING),
            ).fetchone()
            if existing is not None:
                return None
            return _insert_run(conn, batch_id, active.id)


def _mark_superseded(db_path: str, run_id: int, attempt_token: str | None = None) -> bool:
    with db.connection(db_path) as conn:
        with db.write_tx(conn):
            if not _owns_attempt(conn, run_id, attempt_token, require_running=True):
                return False
            clause, params = _token_clause(attempt_token)
            conn.execute(
                f"""
                UPDATE runs SET status = ?, finished_at = ?,
                    error = COALESCE(error, '林班底图已更换或已有更新计算发布，本次结果未发布')
                WHERE id = ? AND status = ?{clause}
                """,
                (STATUS_SUPERSEDED, db.utc_now(), run_id, STATUS_RUNNING, *params),
            )
            return True


def _mark_failed(
    db_path: str, run_id: int, message: str, attempt_token: str | None = None
) -> bool:
    with db.connection(db_path) as conn:
        with db.write_tx(conn):
            if not _owns_attempt(conn, run_id, attempt_token, require_running=False):
                return False
            # Only the attempt that owns the run may mark it failed; a stale
            # worker must not turn a re-claimed run into a failure.
            clause, params = _token_clause(attempt_token)
            updated = conn.execute(
                f"""
                UPDATE runs SET status = ?, finished_at = ?, error = ?
                WHERE id = ? AND status IN (?, ?){clause}
                """,
                (
                    STATUS_FAILED,
                    db.utc_now(),
                    message[:2000],
                    run_id,
                    STATUS_QUEUED,
                    STATUS_RUNNING,
                    *params,
                ),
            ).rowcount
            return updated == 1


def _discard_results(db_path: str, run_id: int, attempt_token: str | None = None) -> None:
    """Delete partial results for a run that did not publish.

    With a token, the delete only happens if this attempt still owns the run:
    a stale worker must not wipe rows written by the replacement attempt.
    """

    with db.connection(db_path) as conn:
        with db.write_tx(conn):
            if not _owns_attempt(conn, run_id, attempt_token, require_running=False):
                return
            conn.execute("DELETE FROM results WHERE run_id = ?", (run_id,))


def recover_stale_runs(db_path: str, stale_seconds: int) -> int:
    """Requeue runs abandoned by a crashed instance.

    Only runs whose heartbeat is older than ``stale_seconds`` are touched, so a
    healthy long-running computation on another instance is never stolen.
    """

    cutoff = datetime.now(timezone.utc).timestamp() - stale_seconds
    recovered = 0
    with db.connection(db_path) as conn:
        with db.write_tx(conn):
            rows = conn.execute(
                "SELECT id, started_at, heartbeat_at FROM runs WHERE status = ?", (STATUS_RUNNING,)
            ).fetchall()
            for row in rows:
                stamp = row["heartbeat_at"] or row["started_at"]
                try:
                    when = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                except (AttributeError, ValueError):
                    when = 0
                if when < cutoff:
                    conn.execute(
                        """
                        UPDATE runs SET status = ?, started_at = NULL, heartbeat_at = NULL,
                            error = '上一次计算进程中断，已重新排队'
                        WHERE id = ?
                        """,
                        (STATUS_QUEUED, row["id"]),
                    )
                    conn.execute("DELETE FROM results WHERE run_id = ?", (row["id"],))
                    recovered += 1
    return recovered


# --------------------------------------------------------------------------
# Queries for the web UI
# --------------------------------------------------------------------------


def list_batches(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT b.*,
               pr.id AS pub_run_id, pr.map_version_id AS pub_map_id, pr.status AS pub_status,
               pr.total_count AS pub_total, pr.matched_count AS pub_matched,
               pr.conflict_count AS pub_conflict, pr.unmatched_count AS pub_unmatched,
               pm.filename AS pub_map_filename,
               lr.id AS latest_run_id, lr.status AS latest_status,
               lr.map_version_id AS latest_map_id
        FROM batches b
        LEFT JOIN runs pr ON pr.id = b.published_run_id
        LEFT JOIN map_versions pm ON pm.id = pr.map_version_id
        LEFT JOIN runs lr ON lr.id = (
            SELECT id FROM runs WHERE batch_id = b.id ORDER BY id DESC LIMIT 1
        )
        ORDER BY b.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    return dict(row) if row else None


def list_runs(conn: sqlite3.Connection, batch_id: int, limit: int = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.*, m.filename AS map_filename, m.sha256 AS map_sha256
        FROM runs r
        LEFT JOIN map_versions m ON m.id = r.map_version_id
        WHERE r.batch_id = ?
        ORDER BY r.id DESC
        LIMIT ?
        """,
        (batch_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT r.*, m.filename AS map_filename, m.sha256 AS map_sha256
        FROM runs r LEFT JOIN map_versions m ON m.id = r.map_version_id
        WHERE r.id = ?
        """,
        (run_id,),
    ).fetchone()
    return dict(row) if row else None


def result_counts(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT status, reason_kind, COUNT(*) AS n
        FROM results WHERE run_id = ?
        GROUP BY status, reason_kind
        ORDER BY status, n DESC
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "status": row["status"],
            "status_label": STATUS_LABELS.get(row["status"], row["status"]),
            "reason_kind": row["reason_kind"],
            "reason_label": REASON_LABELS.get(row["reason_kind"], row["reason_kind"]),
            "count": row["n"],
        }
        for row in rows
    ]


def preview_results(
    conn: sqlite3.Connection, run_id: int, limit: int = 200, only_problems: bool = False
) -> list[dict[str, Any]]:
    clause = "AND r.status != 'matched'" if only_problems else ""
    rows = conn.execute(
        f"""
        SELECT h.source_row, h.hotspot_no, h.lon, h.lat, h.observed_at_utc,
               r.status, r.reason_kind, r.reason, r.codes_json
        FROM results r JOIN hotspots h
          ON h.batch_id = (SELECT batch_id FROM runs WHERE id = ?) AND h.seq = r.hotspot_seq
        WHERE r.run_id = ? {clause}
        ORDER BY h.seq
        LIMIT ?
        """,
        (run_id, run_id, limit),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        codes = db.load_codes(item.pop("codes_json"))
        item["code"] = codes[0] if item["status"] == MATCHED and codes else ""
        item["codes"] = codes
        item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
        item["reason_label"] = REASON_LABELS.get(item["reason_kind"], item["reason_kind"])
        out.append(item)
    return out


def get_published_run(conn: sqlite3.Connection, batch: dict[str, Any]) -> dict[str, Any] | None:
    run_id = batch.get("published_run_id")
    return get_run(conn, int(run_id)) if run_id is not None else None


def is_run_active(conn: sqlite3.Connection, batch_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM runs WHERE batch_id = ? AND status IN (?, ?) LIMIT 1",
        (batch_id, STATUS_QUEUED, STATUS_RUNNING),
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def iter_result_csv(db_path: str, batch_id: int, chunk: int = 1000) -> Iterator[str]:
    """Yield the published detail as CSV, pinned to one published run."""

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")

    def drain() -> str:
        value = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return value

    with db.connection(db_path) as conn:
        with db.read_tx(conn):
            batch = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if batch is None:
                raise ServiceError("热点批次不存在")
            run_id = batch["published_run_id"]
            if run_id is None:
                raise ServiceError("该批次还没有可下载的计算结果")
            run = conn.execute(
                """
                SELECT r.*, m.filename AS map_filename, m.id AS map_id
                FROM runs r JOIN map_versions m ON m.id = r.map_version_id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if run is None:
                raise ServiceError("已发布的计算结果不存在")
            writer.writerow(RESULT_COLUMNS)
            yield "﻿" + drain()
            cursor = conn.execute(
                """
                SELECT h.source_row, h.hotspot_no, h.lon, h.lat, h.observed_at_utc,
                       r.status, r.reason_kind, r.reason, r.codes_json,
                       b.filename AS batch_filename, b.id AS batch_id,
                       m.filename AS map_filename, m.id AS map_id, rr.id AS run_id
                FROM results r
                JOIN runs rr ON rr.id = r.run_id
                JOIN hotspots h ON h.batch_id = rr.batch_id AND h.seq = r.hotspot_seq
                JOIN batches b ON b.id = rr.batch_id
                JOIN map_versions m ON m.id = rr.map_version_id
                WHERE r.run_id = ?
                ORDER BY h.seq
                """,
                (run_id,),
            )
            while True:
                rows = cursor.fetchmany(chunk)
                if not rows:
                    break
                for row in rows:
                    codes = db.load_codes(row["codes_json"])
                    matched = row["status"] == MATCHED and codes
                    writer.writerow(
                        [
                            row["batch_filename"],
                            row["source_row"],
                            row["hotspot_no"],
                            f"{row['lon']:.8f}".rstrip("0").rstrip("."),
                            f"{row['lat']:.8f}".rstrip("0").rstrip("."),
                            row["observed_at_utc"],
                            STATUS_LABELS.get(row["status"], row["status"]),
                            REASON_LABELS.get(row["reason_kind"], row["reason_kind"]),
                            row["reason"],
                            codes[0] if matched else "",
                            ";".join(codes) if not matched else "",
                            f"v{row['map_id']} · {row['map_filename']}",
                            f"run-{row['run_id']}",
                        ]
                    )
                yield drain()
            if buffer.tell():
                yield drain()
