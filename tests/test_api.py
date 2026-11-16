import io
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import box

from app import db, service
from app.main import create_app
from app.parsing import parse_compartment_geojson, parse_hotspots_csv
from app.worker import Worker

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def make_client(tmp_path, start_worker=False):
    app = create_app(db_path=str(tmp_path / "forest.db"), start_worker=start_worker)
    return TestClient(app), app


def geojson(polygons):
    features = []
    for code, (minx, miny, maxx, maxy) in polygons.items():
        features.append(
            {
                "type": "Feature",
                "properties": {"code": code},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [minx, miny],
                            [maxx, miny],
                            [maxx, maxy],
                            [minx, maxy],
                            [minx, miny],
                        ]
                    ],
                },
            }
        )
    import json

    return json.dumps({"type": "FeatureCollection", "features": features}).encode()


def csv_bytes(rows):
    lines = ["hotspot_no,lon,lat,observed_at"]
    lines += [",".join(map(str, row)) for row in rows]
    return ("\n".join(lines) + "\n").encode()


def drain(app):
    """Run queued work deterministically in the test process."""

    while True:
        claimed = service.claim_next_run(app.state.settings["db_path"])
        if claimed is None:
            return
        run_id, attempt_token = claimed
        service.execute_run(app.state.settings["db_path"], run_id, attempt_token=attempt_token)


def test_full_upload_compute_download(tmp_path):
    client, app = make_client(tmp_path)
    with client:
        r = client.post(
            "/maps",
            files={"file": ("m.geojson", geojson({"A": (0, 0, 1, 1), "B": (1, 0, 2, 1)}), "application/geo+json")},
            follow_redirects=False,
        )
        assert r.status_code == 303
        r = client.post(
            "/batches",
            files={
                "file": (
                    "h.csv",
                    csv_bytes(
                        [
                            ("H1", 0.5, 0.5, "2026-01-01T00:00:00Z"),
                            ("H2", 1, 0.5, "2026-01-01T00:01:00Z"),
                            ("H3", 9, 9, "2026-01-01T00:02:00Z"),
                        ]
                    ),
                    "text/csv",
                )
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        drain(app)

        detail = client.get("/batches/1")
        assert detail.status_code == 200
        assert "共边" in detail.text
        assert "A" in detail.text and "B" in detail.text

        dl = client.get("/batches/1/download.csv")
        assert dl.status_code == 200
        assert "attachment" in dl.headers["content-disposition"]
        text = dl.content.decode("utf-8-sig")
        lines = text.splitlines()
        assert len(lines) == 4
        body = [line.split(",") for line in lines[1:]]
        assert body[0][6] == "已赋码" and body[0][9] == "A"
        assert body[1][6] == "归属冲突" and body[1][10] == "A;B"
        assert body[2][6] == "未匹配" and body[2][9] == ""

        status = client.get("/api/batches/1").json()
        assert status["published_total"] == 3
        assert status["matched"] == 1 and status["conflict"] == 1 and status["unmatched"] == 1


def test_recompute_after_map_change_uses_new_version(tmp_path):
    client, app = make_client(tmp_path)
    with client:
        client.post("/maps", files={"file": ("v1.geojson", geojson({"A": (0, 0, 1, 1)}), "application/geo+json")})
        client.post(
            "/batches",
            files={"file": ("h.csv", csv_bytes([("H1", 0.5, 0.5, "2026-01-01T00:00:00Z")]), "text/csv")},
        )
        drain(app)
        assert client.get("/api/batches/1").json()["published_map_version"] == 1

        # New map no longer covers the hotspot.
        client.post("/maps", files={"file": ("v2.geojson", geojson({"B": (10, 10, 11, 11)}), "application/geo+json")})
        client.post("/batches/1/recompute")
        drain(app)

        payload = client.get("/api/batches/1").json()
        assert payload["published_map_version"] == 2
        assert payload["unmatched"] == 1
        csv_text = client.get("/batches/1/download.csv").content.decode("utf-8-sig")
        assert "v2" in csv_text


def test_failed_run_keeps_previous_download(tmp_path, monkeypatch):
    client, app = make_client(tmp_path)
    with client:
        client.post("/maps", files={"file": ("v1.geojson", geojson({"A": (0, 0, 1, 1)}), "application/geo+json")})
        client.post(
            "/batches",
            files={"file": ("h.csv", csv_bytes([("H1", 0.5, 0.5, "2026-01-01T00:00:00Z")]), "text/csv")},
        )
        drain(app)
        before = client.get("/batches/1/download.csv").content

        client.post("/batches/1/recompute")

        import app.attribution as attribution_module

        original = attribution_module.Attributor.attribute_one

        def boom(self, hotspot):
            raise RuntimeError("模拟计算故障")

        monkeypatch.setattr(attribution_module.Attributor, "attribute_one", boom)
        drain(app)
        monkeypatch.setattr(attribution_module.Attributor, "attribute_one", original)

        payload = client.get("/api/batches/1").json()
        assert payload["runs"][0]["status"] == "failed"
        assert payload["published_total"] == 1
        after = client.get("/batches/1/download.csv")
        assert after.status_code == 200
        assert after.content == before
        assert "模拟计算故障" in client.get("/api/batches/1").json()["runs"][0]["error"]


def test_late_old_run_cannot_overwrite_newer(tmp_path):
    """Older run finishing after a newer published run must not be published."""

    db_path = str(tmp_path / "forest.db")
    db.init_db(db_path)
    with db.connection(db_path) as c:
        with db.write_tx(c):
            c.execute(
                "INSERT INTO map_versions (filename,sha256,feature_count,created_at,is_active) VALUES ('m',?,1,'now',1)",
                ("0" * 64,),
            )
            c.execute(
                "INSERT INTO batches (name,filename,sha256,row_count,created_at) VALUES ('b','f',?,1,'now')",
                ("0" * 64,),
            )
            c.execute(
                "INSERT INTO hotspots (batch_id,seq,source_row,hotspot_no,lon,lat,observed_at_utc,raw_json) VALUES (1,0,2,'H',0.5,0.5,'t','{}')"
            )
            old = service._insert_run(c, 1, 1)
            new = service._insert_run(c, 1, 1)

    # Both runs are claimed (old by a slow instance), but the newer one
    # finishes first and publishes.
    old_claim = service.claim_next_run(db_path)
    assert old_claim is not None and old_claim[0] == 1
    new_claim = service.claim_next_run(db_path)
    assert new_claim is not None and new_claim[0] == 2
    assert service.execute_run(db_path, new_claim[0], attempt_token=new_claim[1]) is True
    # The old run finishes late and must not overwrite the newer publication.
    assert service.execute_run(db_path, old_claim[0], attempt_token=old_claim[1]) is False
    with db.connection(db_path) as c:
        batch = c.execute("SELECT published_run_id FROM batches WHERE id=1").fetchone()
        old_row = c.execute("SELECT status FROM runs WHERE id=?", (old,)).fetchone()
    assert batch["published_run_id"] == new
    assert old_row["status"] == "superseded"


def test_run_against_replaced_map_is_superseded_and_requeued(tmp_path):
    db_path = str(tmp_path / "forest.db")
    db.init_db(db_path)
    client, app = make_client(tmp_path)
    with client:
        client.post("/maps", files={"file": ("v1.geojson", geojson({"A": (0, 0, 1, 1)}), "application/geo+json")})
        client.post(
            "/batches",
            files={"file": ("h.csv", csv_bytes([("H1", 0.5, 0.5, "2026-01-01T00:00:00Z")]), "text/csv")},
        )
        # Do NOT drain: swap the map while run #1 is still queued.
        client.post("/maps", files={"file": ("v2.geojson", geojson({"B": (10, 10, 11, 11)}), "application/geo+json")})
        drain(app)

        payload = client.get("/api/batches/1").json()
        statuses = {run["id"]: run["status"] for run in payload["runs"]}
        assert statuses[1] == "superseded"
        assert payload["published_map_version"] == 2
        assert payload["published_total"] == 1
        assert payload["unmatched"] == 1


def test_two_instances_share_database(tmp_path):
    """Two app instances on one DB: one claims each run, publication is single."""

    db_path = str(tmp_path / "forest.db")
    client_a, app_a = make_client(tmp_path)
    client_b, app_b = make_client(tmp_path)
    with client_a, client_b:
        client_a.post("/maps", files={"file": ("v1.geojson", geojson({"A": (0, 0, 1, 1)}), "application/geo+json")})
        client_b.post(
            "/batches",
            files={"file": ("h.csv", csv_bytes([(f"H{i}", 0.5, 0.5, "2026-01-01T00:00:00Z") for i in range(50)]), "text/csv")},
        )
        # Two workers race; exactly one must claim the single queued run.
        results = []

        def claim_and_run(app):
            claimed = service.claim_next_run(db_path)
            if claimed is not None:
                run_id, token = claimed
                results.append(service.execute_run(db_path, run_id, attempt_token=token))

        threads = [
            threading.Thread(target=claim_and_run, args=(app_a,)),
            threading.Thread(target=claim_and_run, args=(app_b,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results) == [True]
        payload = client_b.get("/api/batches/1").json()
        assert payload["published_total"] == 50
        assert payload["runs"][0]["status"] == "succeeded"


def test_worker_thread_processes_queue(tmp_path):
    db_path = str(tmp_path / "forest.db")
    client, app = make_client(tmp_path)
    with client:
        client.post("/maps", files={"file": ("v1.geojson", geojson({"A": (0, 0, 1, 1)}), "application/geo+json")})
        client.post(
            "/batches",
            files={"file": ("h.csv", csv_bytes([("H1", 0.2, 0.2, "2026-01-01T00:00:00Z")]), "text/csv")},
        )
        worker = Worker(db_path, poll_interval=0.02)
        worker.start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                payload = client.get("/api/batches/1").json()
                if payload["published_total"] == 1 and not payload["pending"]:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("worker did not publish in time")
        finally:
            worker.stop()
            worker.join(timeout=5)
        assert payload["matched"] == 1


def test_samples_produce_shared_boundary_conflicts(tmp_path):
    client, app = make_client(tmp_path)
    with client:
        client.post(
            "/maps",
            files={"file": ("v1.geojson", (SAMPLES / "compartments_v1.geojson").read_bytes(), "application/geo+json")},
        )
        client.post(
            "/batches",
            files={"file": ("hotspots.csv", (SAMPLES / "hotspots.csv").read_bytes(), "text/csv")},
        )
        drain(app)
        payload = client.get("/api/batches/1").json()
        assert payload["published_total"] == 15
        assert payload["conflict"] >= 5
        csv_text = client.get("/batches/1/download.csv").content.decode("utf-8-sig")
        assert "共边" in csv_text
        assert "重叠区" in csv_text
        # Every source row appears exactly once.
        assert csv_text.count("H-00") == 15


def test_upload_validation_errors_are_user_facing(tmp_path):
    client, app = make_client(tmp_path)
    with client:
        r = client.post(
            "/maps",
            files={"file": ("bad.geojson", b'{"type":"FeatureCollection","features":[]}', "application/geo+json")},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "err=" in r.headers["location"]
        r = client.post(
            "/batches",
            files={"file": ("bad.csv", b"hotspot_no,lon,lat\nH1,0,0\n", "text/csv")},
            follow_redirects=False,
        )
        assert "err=" in r.headers["location"]
        # No half-written map version survives a rejected upload.
        with db.connection(str(tmp_path / "forest.db")) as c:
            assert service.get_active_map(c) is None


def _seed_batch(db_path):
    db.init_db(db_path)
    with db.connection(db_path) as c:
        with db.write_tx(c):
            c.execute(
                "INSERT INTO map_versions (filename,sha256,feature_count,created_at,is_active) VALUES ('m',?,1,'now',1)",
                ("0" * 64,),
            )
            c.execute(
                "INSERT INTO batches (name,filename,sha256,row_count,created_at) VALUES ('b','f',?,1,'now')",
                ("0" * 64,),
            )
            c.execute(
                "INSERT INTO hotspots (batch_id,seq,source_row,hotspot_no,lon,lat,observed_at_utc,raw_json) VALUES (1,0,2,'H',0.5,0.5,'t','{}')"
            )


def test_stale_attempt_cannot_fail_or_wipe_reclaimed_run(tmp_path):
    """A stalled worker recovered by another instance must be inert."""

    db_path = str(tmp_path / "forest.db")
    _seed_batch(db_path)
    with db.connection(db_path) as c:
        with db.write_tx(c):
            service._insert_run(c, 1, 1)

    stale_run, stale_token = service.claim_next_run(db_path)
    # Another instance decides the run is stale and requeues it.
    assert service.recover_stale_runs(db_path, 0) == 1
    fresh_run, fresh_token = service.claim_next_run(db_path)
    assert (fresh_run, fresh_token != stale_token) == (stale_run, True)

    # Stale worker wakes up and reports failure: must not affect the run.
    assert service._mark_failed(db_path, stale_run, "stale boom", stale_token) is False
    assert service.execute_run(db_path, fresh_run, attempt_token=fresh_token) is True
    with db.connection(db_path) as c:
        run = c.execute("SELECT status, error FROM runs WHERE id=?", (fresh_run,)).fetchone()
    assert run["status"] == "succeeded"
    assert run["error"] is None


def test_map_swap_during_computation_invalidates_publication(tmp_path):
    """Publication reads the active version inside the write transaction."""

    client, app = make_client(tmp_path)
    db_path = app.state.settings["db_path"]
    with client:
        client.post("/maps", files={"file": ("v1.geojson", geojson({"A": (0, 0, 1, 1)}), "application/geo+json")})
        client.post(
            "/batches",
            files={"file": ("h.csv", csv_bytes([("H1", 0.5, 0.5, "2026-01-01T00:00:00Z")]), "text/csv")},
        )
        claimed = service.claim_next_run(db_path)
        run_id, token = claimed
        # Map is replaced between claim and publish.
        service.publish_map(
            db_path, geojson({"B": (10, 10, 11, 11)}), "v2.geojson"
        )
        assert service.execute_run(db_path, run_id, attempt_token=token) is False

        with db.connection(db_path) as c:
            run = service.get_run(c, run_id)
            batch = service.get_batch(c, 1)
            following = c.execute(
                "SELECT id, map_version_id, status FROM runs WHERE batch_id=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert run["status"] == "superseded"
        assert run["published"] == 0
        assert batch["published_run_id"] is None
        # A retry against the new active map was queued automatically.
        assert following["map_version_id"] == 2
        assert following["status"] == "queued"
