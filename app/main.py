"""FastAPI application: uploads, run status, conflict review, CSV download."""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, db, service
from .parsing import InputError
from .worker import Worker

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("forest.web")


def _settings() -> dict[str, Any]:
    return {
        "db_path": os.environ.get("FOREST_DB_PATH", str(BASE_DIR.parent / "data" / "forest.db")),
        "upload_max_mb": int(os.environ.get("FOREST_UPLOAD_MAX_MB", "32")),
        "run_worker": os.environ.get("FOREST_RUN_WORKER", "1") not in {"0", "false", "no"},
        "stale_seconds": int(os.environ.get("FOREST_STALE_RUN_SECONDS", "120")),
        "per_page": int(os.environ.get("FOREST_PREVIEW_ROWS", "200")),
    }


def create_app(db_path: str | None = None, start_worker: bool | None = None) -> FastAPI:
    settings = _settings()
    settings["db_path"] = db_path or settings["db_path"]
    if start_worker is not None:
        settings["run_worker"] = start_worker

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db(app.state.settings["db_path"])
        worker = None
        if app.state.settings["run_worker"]:
            worker = Worker(
                app.state.settings["db_path"],
                stale_seconds=app.state.settings["stale_seconds"],
            )
            worker.start()
        app.state.worker = worker
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()

    app = FastAPI(title="林火卫星热点林班归属", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.worker = None
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.filters["status_label"] = lambda value: service.STATUS_LABELS.get(value, value)
    templates.env.filters["reason_label"] = lambda value: service.REASON_LABELS.get(value, value)
    templates.env.filters["run_status_label"] = lambda value: service.RUN_STATUS_LABELS.get(value, value)

    def conn() -> sqlite3.Connection:
        return db.connect(app.state.settings["db_path"])

    def kick_worker() -> None:
        worker = app.state.worker
        if worker is not None:
            worker.kick()

    def render(request: Request, template: str, context: dict[str, Any], status_code: int = 200):
        return templates.TemplateResponse(
            request=request, name=template, context=context, status_code=status_code
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, msg: str | None = None, err: str | None = None):
        with conn() as c:
            active_map = service.get_active_map(c)
            batches = service.list_batches(c)
            maps = service.list_maps(c, limit=10)
        return render(
            request,
            "index.html",
            {
                "active_map": active_map,
                "maps": maps,
                "batches": batches,
                "msg": msg,
                "err": err,
                "max_mb": app.state.settings["upload_max_mb"],
            },
        )

    @app.post("/maps")
    def upload_map(request: Request, file: UploadFile = File(...)):
        try:
            data = _read_upload(file, app.state.settings["upload_max_mb"], "林班 GeoJSON")
            info, stale_batches = service.publish_map(
                app.state.settings["db_path"], data, _safe_filename(file.filename, "compartments.geojson")
            )
        except InputError as exc:
            return _redirect("/", err=str(exc))
        note = f"林班底图已发布为 v{info.id}（{info.feature_count} 个林班）"
        if info.repaired_count:
            note += f"，其中 {info.repaired_count} 个无效几何已自动修复"
        if stale_batches:
            note += f"。有 {len(stale_batches)} 个批次的已发布结果基于旧底图，请重算"
        return _redirect("/", msg=note)

    @app.post("/recompute-all")
    def recompute_all(request: Request):
        with conn() as c:
            active = service.get_active_map(c)
            if active is None:
                return _redirect("/", err="尚未上传林班底图")
            batches = [row["id"] for row in c.execute("SELECT id FROM batches ORDER BY id").fetchall()]
        queued = 0
        errors = []
        for batch_id in batches:
            try:
                service.enqueue_recompute(app.state.settings["db_path"], int(batch_id))
                queued += 1
            except service.ServiceError as exc:
                errors.append(str(exc))
        kick_worker()
        note = f"已为 {queued} 个批次按 v{active.id} 重新排队计算"
        if errors:
            return _redirect("/", msg=note, err="；".join(errors))
        return _redirect("/", msg=note)

    @app.post("/batches")
    def upload_batch(request: Request, file: UploadFile = File(...)):
        try:
            data = _read_upload(file, app.state.settings["upload_max_mb"], "热点 CSV")
            batch_id, run_id = service.create_batch(
                app.state.settings["db_path"], data, _safe_filename(file.filename, "hotspots.csv")
            )
        except InputError as exc:
            return _redirect("/", err=str(exc))
        if run_id is None:
            return _redirect("/", msg=f"热点批次 #{batch_id} 已保存；上传林班底图后即可计算")
        kick_worker()
        return _redirect(f"/batches/{batch_id}", msg=f"热点批次 #{batch_id} 已上传，计算已排队")

    @app.post("/batches/{batch_id}/recompute")
    def recompute(request: Request, batch_id: int):
        try:
            run_id = service.enqueue_recompute(app.state.settings["db_path"], batch_id)
        except service.ServiceError as exc:
            return _redirect(f"/batches/{batch_id}", err=str(exc))
        kick_worker()
        return _redirect(
            f"/batches/{batch_id}", msg=f"已按当前底图排队重算（计算记录 #{run_id}）"
        )

    @app.get("/batches/{batch_id}", response_class=HTMLResponse)
    def batch_detail(
        request: Request,
        batch_id: int,
        msg: str | None = None,
        err: str | None = None,
        only: str | None = None,
    ):
        with conn() as c:
            batch = service.get_batch(c, batch_id)
            if batch is None:
                raise HTTPException(status_code=404, detail="热点批次不存在")
            active_map = service.get_active_map(c)
            published = service.get_published_run(c, batch)
            runs = service.list_runs(c, batch_id)
            counts = service.result_counts(c, published["id"]) if published else []
            preview = (
                service.preview_results(
                    c,
                    published["id"],
                    limit=app.state.settings["per_page"],
                    only_problems=only == "problems",
                )
                if published
                else []
            )
            pending = service.is_run_active(c, batch_id)
        return render(
            request,
            "batch.html",
            {
                "batch": batch,
                "active_map": active_map,
                "published": published,
                "runs": runs,
                "counts": counts,
                "preview": preview,
                "only_problems": only == "problems",
                "pending": pending,
                "msg": msg,
                "err": err,
                "per_page": app.state.settings["per_page"],
            },
        )

    @app.get("/batches/{batch_id}/download.csv")
    def download(batch_id: int):
        with conn() as c:
            batch = service.get_batch(c, batch_id)
            if batch is None:
                raise HTTPException(status_code=404, detail="热点批次不存在")
            published = service.get_published_run(c, batch)
        if published is None:
            raise HTTPException(status_code=409, detail="该批次还没有可下载的计算结果")
        name = f"hotspot_attribution_batch{batch_id}_run{published['id']}.csv"
        headers = {
            "Content-Disposition": f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}",
            "Cache-Control": "no-store",
        }
        return StreamingResponse(
            service.iter_result_csv(app.state.settings["db_path"], batch_id),
            media_type="text/csv; charset=utf-8",
            headers=headers,
        )

    @app.get("/api/batches/{batch_id}")
    def batch_status(batch_id: int):
        """Small JSON view, useful for polling and for smoke tests."""

        with conn() as c:
            batch = service.get_batch(c, batch_id)
            if batch is None:
                raise HTTPException(status_code=404, detail="热点批次不存在")
            published = service.get_published_run(c, batch)
            runs = service.list_runs(c, batch_id, limit=10)
        return {
            "batch_id": batch_id,
            "published_run_id": batch["published_run_id"],
            "published_map_version": published["map_version_id"] if published else None,
            "published_total": published["total_count"] if published else None,
            "matched": published["matched_count"] if published else None,
            "conflict": published["conflict_count"] if published else None,
            "unmatched": published["unmatched_count"] if published else None,
            "pending": any(
                run["status"] in (service.STATUS_QUEUED, service.STATUS_RUNNING) for run in runs
            ),
            "runs": [
                {
                    "id": run["id"],
                    "status": run["status"],
                    "map_version_id": run["map_version_id"],
                    "matched": run["matched_count"],
                    "conflict": run["conflict_count"],
                    "unmatched": run["unmatched_count"],
                    "error": run["error"],
                }
                for run in runs
            ],
        }

    @app.get("/healthz")
    def healthz():
        with conn() as c:
            active = service.get_active_map(c)
        return {"status": "ok", "version": __version__, "active_map_version": active.id if active else None}

    # ------------------------------------------------------------------
    # Supply manifests (供数清单)
    # ------------------------------------------------------------------

    @app.get("/manifests", response_class=HTMLResponse)
    def manifests_index(request: Request, msg: str | None = None, err: str | None = None):
        with conn() as c:
            # One WAL snapshot: readiness and the stale flags of every listed
            # manifest are evaluated against the same committed state.
            with db.read_tx(c):
                active_map = service.get_active_map(c)
                readiness = service.manifest_readiness(c, active_map)
                manifests = service.list_manifests(c)
        blockers = [item for item in readiness if item["category"] != "ready"]
        return render(
            request,
            "manifests.html",
            {
                "active_map": active_map,
                "readiness": readiness,
                "blockers": blockers,
                "manifests": manifests,
                "msg": msg,
                "err": err,
            },
        )

    @app.post("/manifests")
    def manifests_create(request: Request):
        try:
            manifest_id = service.create_manifest(app.state.settings["db_path"])
        except service.ManifestBlocked as exc:
            with conn() as c:
                with db.read_tx(c):
                    active_map = service.get_active_map(c)
                    readiness = service.manifest_readiness(c, active_map)
                    manifests = service.list_manifests(c)
            return render(
                request,
                "manifests.html",
                {
                    "active_map": active_map,
                    "readiness": readiness,
                    "blockers": exc.blockers,
                    "blocked_error": str(exc),
                    "manifests": manifests,
                    "msg": None,
                    "err": None,
                },
                status_code=409,
            )
        except service.ServiceError as exc:
            return _redirect("/manifests", err=str(exc))
        return _redirect(f"/manifests/{manifest_id}", msg=f"供数清单 #{manifest_id} 已冻结")

    @app.get("/manifests/{manifest_id}", response_class=HTMLResponse)
    def manifest_detail(
        request: Request,
        manifest_id: int,
        msg: str | None = None,
        err: str | None = None,
    ):
        with conn() as c:
            with db.read_tx(c):
                manifest = service.get_manifest(c, manifest_id)
                if manifest is None:
                    raise HTTPException(status_code=404, detail="供数清单不存在")
                active_map = service.get_active_map(c)
                # A manifest may cover batches which were later deleted; the
                # batch page would 404, so only advertise links that exist.
                existing_batches = {
                    int(row["id"])
                    for row in c.execute(
                        "SELECT id FROM batches WHERE id IN ({})".format(
                            ",".join("?" for _ in manifest["items"])
                        ),
                        tuple(int(item["batch_id"]) for item in manifest["items"]),
                    ).fetchall()
                } if manifest["items"] else set()
        return render(
            request,
            "manifest.html",
            {
                "manifest": manifest,
                "active_map": active_map,
                "existing_batches": existing_batches,
                "msg": msg,
                "err": err,
            },
        )

    @app.get("/manifests/{manifest_id}/items/{batch_id}.csv")
    def manifest_item_download(manifest_id: int, batch_id: int):
        with conn() as c:
            item = service.get_manifest_item_export(c, manifest_id, batch_id)
        if item is None:
            raise HTTPException(status_code=404, detail="供数清单中不存在该批次")
        name = f"manifest{manifest_id}_batch{batch_id}_run{item['run_id']}.csv"
        headers = {
            "Content-Disposition": f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}",
            "Cache-Control": "no-store",
        }
        return StreamingResponse(
            service.iter_manifest_item_csv(
                app.state.settings["db_path"], manifest_id, batch_id
            ),
            media_type="text/csv; charset=utf-8",
            headers=headers,
        )

    @app.get("/api/manifests/{manifest_id}")
    def manifest_status(manifest_id: int):
        """JSON view of a frozen manifest, including live staleness."""

        with conn() as c:
            with db.read_tx(c):
                manifest = service.get_manifest(c, manifest_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail="供数清单不存在")
        return {
            "manifest_id": manifest["id"],
            "created_at": manifest["created_at"],
            "map_version_id": manifest["map_version_id"],
            "map_filename": manifest["map_filename"],
            "map_sha256": manifest["map_sha256"],
            "batch_count": manifest["batch_count"],
            "total": manifest["total_count"],
            "matched": manifest["matched_count"],
            "conflict": manifest["conflict_count"],
            "unmatched": manifest["unmatched_count"],
            "stale": manifest["stale"],
            "stale_reasons": manifest["stale_reasons"],
            "items": [
                {
                    "batch_id": item["batch_id"],
                    "filename": item["batch_filename"],
                    "run_id": item["run_id"],
                    "map_version_id": item["map_version_id"],
                    "row_count": item["row_count"],
                    "matched": item["matched_count"],
                    "conflict": item["conflict_count"],
                    "unmatched": item["unmatched_count"],
                }
                for item in manifest["items"]
            ],
        }

    return app


def _read_upload(file: UploadFile, max_mb: int, what: str) -> bytes:
    data = file.file.read()
    if not data:
        raise InputError(f"{what} 为空")
    if len(data) > max_mb * 1024 * 1024:
        raise InputError(f"{what} 超过 {max_mb} MB 限制")
    return data


def _safe_filename(name: str | None, fallback: str) -> str:
    text = Path(name or "").name.strip()
    return text or fallback


def _redirect(path: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    from urllib.parse import urlencode

    params = {key: value for key, value in (("msg", msg), ("err", err)) if value}
    url = path + (("?" + urlencode(params)) if params else "")
    return RedirectResponse(url, status_code=303)


app = create_app()
