"""Cross-process concurrency tests: real Uvicorn servers, one database.

These exercise the guarantee that matters in deployment: two web instances
uploading/recomputing at the same time must never publish a detail file that
mixes results from two different compartment versions.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Server:
    def __init__(self, db_path: Path, port: int, cwd: Path):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        env = dict(os.environ)
        env.update(
            FOREST_DB_PATH=str(db_path),
            FOREST_RUN_WORKER="1",
            FOREST_STALE_RUN_SECONDS="2",
            PYTHONPATH=str(ROOT),
        )
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def wait_ready(self, timeout: float = 25.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if httpx.get(self.base + "/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError("server did not start")

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.proc.kill()


def upload_map(client: httpx.Client, name: str, payload: bytes) -> None:
    response = client.post(
        f"{client.base_url}/maps",
        files={"file": (name, payload, "application/geo+json")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def upload_hotspots(client: httpx.Client, payload: bytes) -> None:
    response = client.post(
        f"{client.base_url}/batches",
        files={"file": ("hotspots.csv", payload, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def wait_published(client: httpx.Client, batch_id: int, timeout: float = 25.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"{client.base_url}/api/batches/{batch_id}").json()
        if payload["published_total"] is not None and not payload["pending"]:
            return payload
        time.sleep(0.05)
    raise AssertionError("batch never published")


@pytest.mark.slow
def test_two_servers_publish_single_consistent_version(tmp_path):
    db_path = tmp_path / "forest.db"
    port_a, port_b = free_port(), free_port()
    server_a = Server(db_path, port_a, tmp_path)
    server_b = Server(db_path, port_b, tmp_path)
    server_a.wait_ready()
    server_b.wait_ready()
    try:
        with httpx.Client(base_url=server_a.base, timeout=30) as client_a, httpx.Client(
            base_url=server_b.base, timeout=30
        ) as client_b:
            upload_map(client_a, "v1.geojson", (SAMPLES / "compartments_v1.geojson").read_bytes())
            upload_hotspots(client_b, (SAMPLES / "hotspots.csv").read_bytes())
            first = wait_published(client_a, 1)
            assert first["published_map_version"] == 1

            # Instance A swaps the map while instance B triggers a recompute.
            upload_map(client_a, "v2.geojson", (SAMPLES / "compartments_v2.geojson").read_bytes())
            response = client_b.post(
                f"{client_b.base_url}/batches/1/recompute", follow_redirects=False
            )
            assert response.status_code == 303

            final = wait_published(client_b, 1)
            assert final["published_map_version"] == 2
            assert final["published_total"] == 15

            # The published CSV must be internally consistent: it is produced
            # exclusively from the run pinned to the active v2 map.
            csv_bytes = client_a.get(f"{client_a.base_url}/batches/1/download.csv").content
            text = csv_bytes.decode("utf-8-sig")
            rows = text.splitlines()
            assert len(rows) == 16
            assert all("v2" in line for line in rows[1:])
            assert "v1" not in text
            assert "A-01;B-02" not in text  # v1-only shared edge between A and B

            # Every hotspot row present exactly once.
            assert text.count("H-00") == 15

            # No computation against the retired v1 map may remain pending, and
            # no v1 run may be the published one.
            payload = client_a.get(f"{client_a.base_url}/api/batches/1").json()
            active_version = client_a.get(f"{client_a.base_url}/healthz").json()[
                "active_map_version"
            ]
            assert payload["published_map_version"] == active_version == 2
            for run in payload["runs"]:
                if run["map_version_id"] == 1:
                    assert run["status"] in {"succeeded", "superseded"}
            published_runs = [
                run for run in payload["runs"] if run["id"] == payload["published_run_id"]
            ]
            assert published_runs[0]["map_version_id"] == active_version
    finally:
        server_a.stop()
        server_b.stop()


@pytest.mark.slow
def test_two_servers_freeze_consistent_manifest_while_churning(tmp_path):
    """Manifest creation on one instance races map swaps on the other.

    Every manifest that is successfully created must be internally consistent:
    all items pinned to the manifest's map version and to runs that really
    published; later churn only marks it stale, and every item CSV stays
    downloadable and byte-stable.
    """

    db_path = tmp_path / "forest.db"
    port_a, port_b = free_port(), free_port()
    server_a = Server(db_path, port_a, tmp_path)
    server_b = Server(db_path, port_b, tmp_path)
    server_a.wait_ready()
    server_b.wait_ready()
    try:
        with httpx.Client(base_url=server_a.base, timeout=30) as client_a, httpx.Client(
            base_url=server_b.base, timeout=30
        ) as client_b:
            upload_map(client_a, "v1.geojson", (SAMPLES / "compartments_v1.geojson").read_bytes())
            upload_hotspots(client_a, (SAMPLES / "hotspots.csv").read_bytes())
            wait_published(client_a, 1)

            first = client_a.post(f"{client_a.base_url}/manifests", follow_redirects=False)
            assert first.status_code == 303, first.text
            manifest_id = int(first.headers["location"].split("/")[2].split("?")[0])
            frozen = client_a.get(f"{client_a.base_url}/api/manifests/{manifest_id}").json()
            assert frozen["stale"] is False
            before = {
                item["batch_id"]: client_a.get(
                    f"{client_a.base_url}/manifests/{manifest_id}/items/{item['batch_id']}.csv"
                ).content
                for item in frozen["items"]
            }

            import threading

            errors: list[BaseException] = []
            stop = threading.Event()

            def churn():
                v2 = (SAMPLES / "compartments_v2.geojson").read_bytes()
                v1 = (SAMPLES / "compartments_v1.geojson").read_bytes()
                try:
                    turn = 0
                    while not stop.is_set():
                        upload_map(client_b, f"v{turn}.geojson", v2 if turn % 2 == 0 else v1)
                        client_b.post(
                            f"{client_b.base_url}/batches/1/recompute", follow_redirects=False
                        )
                        turn += 1
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            thread = threading.Thread(target=churn)
            thread.start()
            try:
                time.sleep(2.0)
            finally:
                stop.set()
                thread.join()
            assert not errors, errors

            after = client_a.get(f"{client_a.base_url}/api/manifests/{manifest_id}").json()
            assert after["stale"] is True
            assert {item["run_id"] for item in after["items"]} == {
                item["run_id"] for item in frozen["items"]
            }
            for item in after["items"]:
                download = client_a.get(
                    f"{client_a.base_url}/manifests/{manifest_id}/items/{item['batch_id']}.csv"
                )
                assert download.status_code == 200
                assert download.content == before[item["batch_id"]]
                assert all(
                    "v1" in line
                    for line in download.content.decode("utf-8-sig").splitlines()[1:]
                )

            # The frozen map version is recorded on the manifest and each item.
            assert all(item["map_version_id"] == 1 for item in after["items"])
            assert after["map_version_id"] == 1
    finally:
        server_a.stop()
        server_b.stop()


@pytest.mark.slow
def test_concurrent_map_swaps_leave_exactly_one_active(tmp_path):
    db_path = tmp_path / "forest.db"
    port_a, port_b = free_port(), free_port()
    server_a = Server(db_path, port_a, tmp_path)
    server_b = Server(db_path, port_b, tmp_path)
    server_a.wait_ready()
    server_b.wait_ready()
    try:
        with httpx.Client(base_url=server_a.base, timeout=30) as client_a, httpx.Client(
            base_url=server_b.base, timeout=30
        ) as client_b:
            upload_hotspots(client_a, (SAMPLES / "hotspots.csv").read_bytes())
            v1 = (SAMPLES / "compartments_v1.geojson").read_bytes()
            v2 = (SAMPLES / "compartments_v2.geojson").read_bytes()

            import threading

            errors: list[BaseException] = []

            def swap(client, payload, name):
                try:
                    upload_map(client, name, payload)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [
                threading.Thread(target=swap, args=(client_a, v1, "a.geojson")),
                threading.Thread(target=swap, args=(client_b, v2, "b.geojson")),
                threading.Thread(target=swap, args=(client_a, v2, "c.geojson")),
                threading.Thread(target=swap, args=(client_b, v1, "d.geojson")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert not errors

            health = client_a.get(f"{client_a.base_url}/healthz").json()
            assert health["active_map_version"] is not None
            from app import db, service

            with db.connection(str(db_path)) as conn:
                active_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM map_versions WHERE is_active = 1"
                ).fetchone()["n"]
                versions = conn.execute("SELECT COUNT(*) AS n FROM map_versions").fetchone()["n"]
            assert active_count == 1
            assert versions == 4

            # The active version is the one the health endpoint reports, and a
            # recompute publishes against it with no mixed output.
            client_b.post(f"{client_b.base_url}/batches/1/recompute", follow_redirects=False)
            final = wait_published(client_b, 1)
            assert final["published_map_version"] == health["active_map_version"]
    finally:
        server_a.stop()
        server_b.stop()
