"""Supply manifest (供数清单) tests.

The manifest is the warehouse's frozen hand-off: every batch under one map
version, each pinned to one computation run.  These tests cover the three
promises:

* creation refuses to produce a partial manifest and lists every blocker;
* a frozen manifest keeps its run ids and CSV bytes no matter what is later
  uploaded, replaced or republished;
* concurrent upload/recompute work on two app instances only ever makes a
  manifest stale — it can never be half-old, half-new.
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import db, service
from app.main import create_app


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
                        [[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]
                    ],
                },
            }
        )
    return json.dumps({"type": "FeatureCollection", "features": features}).encode()


def csv_bytes(rows, name="hotspots.csv"):
    lines = ["hotspot_no,lon,lat,observed_at"]
    lines += [",".join(map(str, row)) for row in rows]
    return ("\n".join(lines) + "\n").encode()


def make_client(tmp_path):
    return TestClient(create_app(db_path=str(tmp_path / "forest.db"), start_worker=False))


def drain(app):
    db_path = app.state.settings["db_path"]
    while True:
        claimed = service.claim_next_run(db_path)
        if claimed is None:
            return
        service.execute_run(db_path, claimed[0], attempt_token=claimed[1])


def upload_map(client, polygons, name="m.geojson"):
    response = client.post(
        "/maps",
        files={"file": (name, geojson(polygons), "application/geo+json")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def upload_batch(client, rows, name="hotspots.csv"):
    response = client.post(
        "/batches",
        files={"file": (name, csv_bytes(rows), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].split("/")[2].split("?")[0])


def create_manifest(client):
    response = client.post("/manifests", follow_redirects=False)
    manifest_id = None
    if response.status_code == 303:
        location = response.headers["location"]
        prefix = "/manifests/"
        if location.startswith(prefix):
            manifest_id = int(location.removeprefix(prefix).split("?")[0])
    return response, manifest_id


def manifest_json(client, manifest_id):
    return client.get(f"/api/manifests/{manifest_id}").json()


def item_csv(client, manifest_id, batch_id):
    response = client.get(f"/manifests/{manifest_id}/items/{batch_id}.csv")
    assert response.status_code == 200
    return response.content


def test_creation_refuses_partial_and_lists_every_blocker(tmp_path):
    """Missing and pending batches are enumerated; nothing is written."""

    with make_client(tmp_path) as client:
        app = client.app
        upload_map(client, {"A": (0, 0, 1, 1)})
        upload_batch(client, [("H1", 0.5, 0.5, "2026-01-01T00:00:00Z")], "ready.csv")
        drain(app)
        upload_batch(client, [("H2", 0.5, 0.5, "2026-01-01T00:00:00Z")], "pending.csv")

        response, manifest_id = create_manifest(client)
        assert response.status_code == 409
        assert manifest_id is None
        assert "pending.csv" in response.text
        assert "待算" in response.text
        assert "ready.csv" in response.text  # readiness table still rendered

        with db.connection(app.state.settings["db_path"]) as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM manifests").fetchone()["n"] == 0
            assert conn.execute("SELECT COUNT(*) AS n FROM manifest_items").fetchone()["n"] == 0

        # A batch uploaded before any map exists is "missing" and blocks creation.
        app2_path = tmp_path / "no-map.db"
        db.init_db(str(app2_path))
        service.create_batch(
            str(app2_path),
            csv_bytes([("H1", 0.5, 0.5, "2026-01-01T00:00:00Z")]),
            "no-map.csv",
        )
        with pytest.raises(service.ManifestBlocked) as excinfo:
            service.create_manifest(str(app2_path))
        assert excinfo.value.blockers[0].category == service.MANIFEST_MISSING
        assert excinfo.value.blockers[0].filename == "no-map.csv"

        # With no batches at all there is nothing to freeze.
        empty_path = tmp_path / "empty.db"
        db.init_db(str(empty_path))
        with pytest.raises(service.ServiceError):
            service.create_manifest(str(empty_path))


def test_failed_batch_is_reported_as_failure_not_missing(tmp_path):
    with make_client(tmp_path) as client:
        app = client.app
        upload_map(client, {"A": (0, 0, 1, 1)})
        upload_batch(client, [("H1", 0.5, 0.5, "2026-01-01T00:00:00Z")])
        claimed = service.claim_next_run(app.state.settings["db_path"])
        service._mark_failed(
            app.state.settings["db_path"], claimed[0], "模拟计算故障", claimed[1]
        )

        response, _ = create_manifest(client)
        assert response.status_code == 409
        assert "计算失败" in response.text
        assert "模拟计算故障" not in response.text  # detail is category-level, not a traceback


def test_manifest_freezes_runs_and_survives_republish_and_failure(tmp_path):
    with make_client(tmp_path) as client:
        app = client.app
        upload_map(client, {"A": (0, 0, 1, 1), "B": (1, 0, 2, 1)})
        batch_one = upload_batch(client, [("H1", 0.5, 0.5, "2026-01-01T00:00:00Z")], "one.csv")
        batch_two = upload_batch(client, [("H2", 1.0, 0.5, "2026-01-01T00:00:00Z")], "two.csv")
        drain(app)

        response, manifest_id = create_manifest(client)
        assert response.status_code == 303
        frozen = manifest_json(client, manifest_id)
        assert frozen["stale"] is False
        assert frozen["map_version_id"] == 1
        assert frozen["batch_count"] == 2
        assert {item["batch_id"] for item in frozen["items"]} == {batch_one, batch_two}
        frozen_runs = {item["batch_id"]: item["run_id"] for item in frozen["items"]}
        before = {batch_id: item_csv(client, manifest_id, batch_id) for batch_id in frozen_runs}
        assert "A" in before[batch_one].decode("utf-8-sig")

        # New map + recompute publishes new runs for both batches.
        upload_map(client, {"C": (10, 10, 11, 11)}, "v2.geojson")
        client.post("/recompute-all")
        drain(app)

        stale = manifest_json(client, manifest_id)
        assert stale["stale"] is True
        assert any("当前底图已变为 v2" in reason for reason in stale["stale_reasons"])
        assert any("已发布新的计算记录" in reason for reason in stale["stale_reasons"])
        # Item run ids are unchanged, and so are the CSV bytes.
        assert {item["batch_id"]: item["run_id"] for item in stale["items"]} == frozen_runs
        for batch_id in frozen_runs:
            assert item_csv(client, manifest_id, batch_id) == before[batch_id]
        # The live batch download did move on — proving the manifest is pinned.
        assert client.get(f"/batches/{batch_one}/download.csv").content != before[batch_one]

        # A failed recompute on every batch must not damage the frozen manifest.
        client.post("/recompute-all")
        import app.attribution as attribution_module

        original = attribution_module.Attributor.attribute_one

        def boom(self, hotspot):
            raise RuntimeError("模拟失败重算")

        attribution_module.Attributor.attribute_one = boom
        try:
            drain(app)
        finally:
            attribution_module.Attributor.attribute_one = original

        for batch_id in frozen_runs:
            assert item_csv(client, manifest_id, batch_id) == before[batch_id]
        assert manifest_json(client, manifest_id)["stale"] is True

        page = client.get(f"/manifests/{manifest_id}")
        assert page.status_code == 200
        assert "已过期" in page.text
        assert f"/manifests/{manifest_id}/items/{batch_one}.csv" in page.text


def test_items_from_an_older_map_version_are_still_downloadable(tmp_path):
    """Old manifests of a superseded map version download their pinned runs."""

    with make_client(tmp_path) as client:
        app = client.app
        upload_map(client, {"A": (0, 0, 1, 1)}, "v1.geojson")
        batch_id = upload_batch(client, [("H1", 0.5, 0.5, "2026-01-01T00:00:00Z")])
        drain(app)
        _, first_manifest = create_manifest(client)
        first_bytes = item_csv(client, first_manifest, batch_id)

        upload_map(client, {"B": (10, 10, 11, 11)}, "v2.geojson")
        client.post("/batches/1/recompute")
        drain(app)
        _, second_manifest = create_manifest(client)
        assert second_manifest != first_manifest

        assert item_csv(client, first_manifest, batch_id) == first_bytes
        assert "v1" in first_bytes.decode("utf-8-sig")
        assert "v2" in item_csv(client, second_manifest, batch_id).decode("utf-8-sig")

        listing = client.get("/manifests")
        assert listing.status_code == 200
        assert "已过期" in listing.text
        assert "有效" in listing.text


def test_new_batch_after_creation_marks_manifest_stale(tmp_path):
    with make_client(tmp_path) as client:
        app = client.app
        upload_map(client, {"A": (0, 0, 1, 1)})
        first_batch = upload_batch(client, [("H1", 0.5, 0.5, "2026-01-01T00:00:00Z")], "one.csv")
        drain(app)
        _, manifest_id = create_manifest(client)
        frozen = manifest_json(client, manifest_id)
        assert frozen["stale"] is False
        frozen_csv = item_csv(client, manifest_id, first_batch)

        upload_batch(client, [("H2", 0.5, 0.5, "2026-01-01T00:00:00Z")], "two.csv")
        drain(app)

        after = manifest_json(client, manifest_id)
        assert after["stale"] is True
        assert any("新增批次" in reason for reason in after["stale_reasons"])
        assert after["batch_count"] == 1
        assert item_csv(client, manifest_id, first_batch) == frozen_csv
        # The newly added batch is not addressable through the old manifest.
        assert client.get(f"/manifests/{manifest_id}/items/2.csv").status_code == 404

        # A new manifest now covers both batches.
        response, second = create_manifest(client)
        assert response.status_code == 303
        assert manifest_json(client, second)["batch_count"] == 2
        assert manifest_json(client, second)["stale"] is False


def test_failed_recompute_keeps_current_batch_ready(tmp_path):
    """A failed recompute does not remove the published current-map result."""

    with make_client(tmp_path) as client:
        app = client.app
        upload_map(client, {"A": (0, 0, 1, 1)})
        batch_id = upload_batch(client, [("H1", 0.5, 0.5, "2026-01-01T00:00:00Z")])
        drain(app)

        client.post(f"/batches/{batch_id}/recompute")
        claimed = service.claim_next_run(app.state.settings["db_path"])
        service._mark_failed(
            app.state.settings["db_path"], claimed[0], "模拟失败重算", claimed[1]
        )

        with db.connection(app.state.settings["db_path"]) as conn:
            readiness = service.manifest_readiness(conn)
        assert readiness[0]["category"] == "ready"
        assert "失败" in readiness[0]["detail"]

        response, manifest_id = create_manifest(client)
        assert response.status_code == 303
        frozen = manifest_json(client, manifest_id)
        assert frozen["items"][0]["run_id"] == 1
        assert "A" in item_csv(client, manifest_id, batch_id).decode("utf-8-sig")


def test_failed_recompute_on_new_map_is_reported_as_failure(tmp_path):
    with make_client(tmp_path) as client:
        app = client.app
        upload_map(client, {"A": (0, 0, 1, 1)})
        upload_batch(client, [("H1", 0.5, 0.5, "2026-01-01T00:00:00Z")])
        drain(app)

        upload_map(client, {"B": (10, 10, 11, 11)}, "v2.geojson")
        client.post("/batches/1/recompute")
        claimed = service.claim_next_run(app.state.settings["db_path"])
        service._mark_failed(
            app.state.settings["db_path"], claimed[0], "按新底图重算失败", claimed[1]
        )

        response, _ = create_manifest(client)
        assert response.status_code == 409
        assert "计算失败" in response.text
        assert "v2" in response.text


def test_manifest_creation_raises_when_no_batches(tmp_path):
    with make_client(tmp_path) as client:
        upload_map(client, {"A": (0, 0, 1, 1)})
        response, _ = create_manifest(client)
        assert response.status_code == 303
        assert "err=" in response.headers["location"]


def test_unknown_manifest_and_item_return_404(tmp_path):
    with make_client(tmp_path) as client:
        upload_map(client, {"A": (0, 0, 1, 1)})
        upload_batch(client, [("H1", 0.5, 0.5, "2026-01-01T00:00:00Z")])
        assert client.get("/manifests/999").status_code == 404
        assert client.get("/api/manifests/999").status_code == 404
        assert client.get("/manifests/999/items/1.csv").status_code == 404


def test_concurrent_create_and_publication_never_mixes_state(tmp_path):
    """Two app instances: one freezes manifests while the other republishes.

    Invariant under every interleaving: all items of a manifest come from the
    map version recorded on that manifest, each item's run was really
    published, and the item row count matches that run's stored results.
    """

    db_path = tmp_path / "forest.db"
    client_a = TestClient(create_app(db_path=str(db_path), start_worker=False))
    client_b = TestClient(create_app(db_path=str(db_path), start_worker=False))
    errors: list[BaseException] = []
    created: list[int] = []
    with client_a as app_a_client, client_b as app_b_client:
        upload_map(app_a_client, {"A": (0, 0, 1, 1)}, "v1.geojson")
        for index in range(4):
            upload_batch(
                app_a_client,
                [(f"H{index}", 0.5, 0.5, "2026-01-01T00:00:00Z")],
                f"batch{index}.csv",
            )
        drain(app_a_client.app)

        stop = threading.Event()

        def republisher():
            app_b = app_b_client.app
            maps = [{"A": (0, 0, 1, 1)}, {"B": (0, 0, 2, 2)}, {"C": (0, 0, 3, 3)}]
            turn = 0
            try:
                while not stop.is_set():
                    upload_map(app_b_client, maps[turn % len(maps)], f"swap{turn}.geojson")
                    for batch_id in range(1, 5):
                        try:
                            service.enqueue_recompute(
                                app_b.state.settings["db_path"], batch_id
                            )
                        except service.ServiceError:
                            pass
                    drain(app_b)
                    turn += 1
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def creator():
            try:
                while not stop.is_set():
                    try:
                        created.append(service.create_manifest(str(db_path)))
                    except service.ServiceError:
                        # Not every batch has published on the *current* map at
                        # this instant; refusing is the correct behaviour.
                        pass
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=republisher), threading.Thread(target=creator)]
        for thread in threads:
            thread.start()
        import time

        time.sleep(1.5)
        stop.set()
        for thread in threads:
            thread.join()
        assert not errors, errors
        assert created, "no manifest was created during the race"

        with db.connection(str(db_path)) as conn:
            for manifest_id in created:
                manifest = service.get_manifest(conn, manifest_id)
                assert manifest is not None
                items = manifest["items"]
                assert items
                assert all(
                    int(item["map_version_id"]) == int(manifest["map_version_id"])
                    for item in items
                )
                for item in items:
                    run = conn.execute(
                        "SELECT r.status, r.published, r.map_version_id, "
                        "(SELECT COUNT(*) FROM results x WHERE x.run_id = r.id) AS n "
                        "FROM runs r WHERE r.id = ?",
                        (item["run_id"],),
                    ).fetchone()
                    assert run["status"] == service.STATUS_SUCCEEDED
                    assert run["published"] == 1
                    assert int(run["map_version_id"]) == int(item["map_version_id"])
                    assert int(run["n"]) == int(item["row_count"])
                total = sum(int(item["row_count"]) for item in items)
                assert total == int(manifest["total_count"])
                assert (
                    int(manifest["matched_count"])
                    + int(manifest["conflict_count"])
                    + int(manifest["unmatched_count"])
                    == total
                )

        # Every created manifest is still downloadable after the churn.
        for manifest_id in created:
            manifest = manifest_json(app_a_client, manifest_id)
            for item in manifest["items"]:
                assert client_a.get(
                    f"/manifests/{manifest_id}/items/{item['batch_id']}.csv"
                ).status_code == 200
