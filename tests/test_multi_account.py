"""Account isolation, durable retries, shared enrollment and API behavior.

No live credentials or GPU required; ML is stubbed, HTTP ownership is enforced by
FakeImmich so a request using the wrong key fails instead of passing silently.
"""

from contextlib import contextmanager, nullcontext
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import accounts
import classifier
import immich
import multi_service
from multi_api import create_router
from multi_service import Service, BusyError
from multi_store import Store


class Reply:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status_code = status
        self.text = ""
        self.content = b"preview"

    def json(self):
        return self.payload


class FakeImmich:
    def __init__(self):
        self.assets = {}
        self.persons = {}
        self.faces = {}
        self.writes = []
        self.disabled = set()
        self.thumbnail_failures = set()
        self.fail_face_once = set()
        self.owner_alias = {}
        self.lock = threading.Lock()

    def add_asset(self, account, asset=None):
        asset = asset or str(uuid4())
        self.assets[asset] = {
            "id": asset,
            "ownerId": "owner-" + account,
            "createdAt": "2026-01-01T00:00:00.000Z",
            "fileCreatedAt": "2020-02-03T12:00:00.000Z",
        }
        return asset

    def request(self, method, url, headers=None, json=None, params=None, **kwargs):
        key = (headers or {}).get("x-api-key", "")
        account = key.removeprefix("secret-")
        if account in self.disabled:
            return Reply({}, 401)
        path = url.split("/api/")[-1]
        if path == "users/me":
            return Reply({"id": self.owner_alias.get(account, "owner-" + account)})
        if path == "server/version":
            return Reply({"major": 3, "minor": 3, "patch": 3})
        if path == "search/metadata":
            # Deliberately return all accounts' assets, like partner sharing.
            return Reply(
                {"assets": {"items": list(self.assets.values()), "nextCursor": None}}
            )
        if path.startswith("assets/"):
            aid = path.split("/")[1]
            if aid not in self.assets:
                return Reply({}, 404)
            # Shared assets are readable; the service must explicitly reject them.
            return Reply(self.assets[aid])
        if path == "people" and method == "POST":
            person = str(uuid4())
            self.persons[person] = {"account": account, "name": json["name"]}
            return Reply({"id": person}, 201)
        if path.startswith("people/"):
            person = path.split("/")[1]
            if person not in self.persons:
                return Reply({}, 404)
            assert self.persons[person]["account"] == account, (
                "WRONG OWNER PERSON REQUEST"
            )
            if method == "DELETE":
                del self.persons[person]
                for aid in self.faces:
                    self.faces[aid] = [
                        f for f in self.faces[aid] if f["person"]["id"] != person
                    ]
                return Reply({}, 204)
            if method == "PUT":
                self.persons[person]["name"] = json["name"]
            return Reply(self.persons[person])
        if path == "faces" and method == "GET":
            aid = params["id"]
            assert self.assets[aid]["ownerId"] == "owner-" + account, (
                "WRONG OWNER FACE READ"
            )
            return Reply(self.faces.get(aid, []))
        if path == "faces" and method == "POST":
            aid, person = json["assetId"], json["personId"]
            assert self.assets[aid]["ownerId"] == "owner-" + account, (
                "WRONG OWNER FACE WRITE"
            )
            assert self.persons[person]["account"] == account, "WRONG PERSON MAPPING"
            if aid in self.fail_face_once:
                self.fail_face_once.remove(aid)
                return Reply({}, 500)
            with self.lock:
                self.writes.append((account, aid, person))
                self.faces.setdefault(aid, []).append(
                    {"id": str(uuid4()), "person": {"id": person}}
                )
            return Reply({}, 201)
        if path == "tags":
            return Reply([{"id": "tag-" + account, "name": json["tags"][0]}])
        if path.startswith("tags/"):
            assert path.split("/")[1] == "tag-" + account, "WRONG OWNER TAG"
            return Reply([{"success": True}])
        raise AssertionError((method, path))


@pytest.fixture
def setup_service(tmp_path, monkeypatch):
    fake = FakeImmich()
    monkeypatch.setattr(accounts.requests, "request", fake.request)
    for method in ("get", "post", "put"):
        monkeypatch.setattr(
            immich.requests,
            method,
            lambda url, _method=method, **kw: fake.request(_method.upper(), url, **kw),
        )
    monkeypatch.setattr(immich, "_search_filter", True)
    monkeypatch.setattr(immich, "REVIEW_TAG", "Review")
    service = Service(
        {
            name: accounts.Account(name, "secret-" + name)
            for name in ("user_a", "user_b", "shared")
        },
        tmp_path,
        batch_size=1,
        workers=2,
    )
    pet = service.create_pet("Mochi")
    ref = fake.add_asset("user_a")
    service.add_reference("user_a", ref, pet)
    loads = []

    @contextmanager
    def session():
        loads.append("load")
        yield
        loads.append("unload")

    monkeypatch.setattr(multi_service.inference, "inference_session", session)
    monkeypatch.setattr(multi_service.inference, "model_error_count", lambda: 0)
    monkeypatch.setattr(multi_service.emb, "save_embed_cache", lambda: None)
    monkeypatch.setattr(multi_service.emb, "crop_animals", lambda img: [(None, img)])
    monkeypatch.setattr(
        multi_service.emb, "embed_image", lambda img: np.array([1.0, 2.0])
    )

    def thumbnail(aid):
        assert accounts.current() is not None
        assert fake.assets[aid]["ownerId"] == "owner-" + accounts.current().id
        return (
            None if aid in fake.thumbnail_failures else SimpleNamespace(size=(100, 100))
        )

    monkeypatch.setattr(multi_service.emb, "fetch_thumbnail", thumbnail)
    train_calls = []

    def train(doc):
        train_calls.append(doc)
        return ["Mochi"], None, None

    monkeypatch.setattr(service, "train", train)
    monkeypatch.setattr(multi_service.classifier, "classify", lambda *args: (pet, 0.99))
    yield service, fake, pet, ref, loads, train_calls
    service.shutdown()


def test_three_owners_share_one_training_and_model_session(setup_service):
    service, fake, pet, ref, loads, training = setup_service
    fake.add_asset("user_b")
    fake.add_asset("shared")
    service.scan()
    assert service.status["error"] is None
    assert loads == ["load", "unload"]
    assert len(training) == 1
    assert {a for a, _, _ in fake.writes} == set(service.accounts)
    assert len(service.store.enrollment()["pets"][pet]["refs"]) == 1
    assert len(set(service.store.enrollment()["pets"][pet]["persons"].values())) == 3
    assert all(row["jobs"] == {"done": 1} for row in service.store.summary().values())
    # Inclusive discovery sees the same assets again; durable jobs prevent retagging.
    service.scan()
    assert len(fake.writes) == 3


def test_round_robin_batches_do_not_starve_other_accounts(setup_service):
    service, fake, *_ = setup_service
    for _ in range(4):
        fake.add_asset("user_a")
    fake.add_asset("user_b")
    fake.add_asset("shared")
    service.scan()
    assert [a for a, _, _ in fake.writes[:3]] == ["user_a", "user_b", "shared"]


def test_failed_thumbnail_and_write_remain_retryable(setup_service):
    service, fake, pet, ref, *_ = setup_service
    failing = fake.add_asset("user_b")
    fake.thumbnail_failures.add(ref)
    fake.fail_face_once.add(failing)
    service.scan()
    assert service.store.summary()["user_a"]["jobs"] == {"retry": 1}
    assert service.store.summary()["user_b"]["jobs"] == {"retry": 1}
    fake.thumbnail_failures.clear()
    service.store.db.execute("UPDATE jobs SET retry_at=0")
    service.store.db.commit()
    service.scan()
    assert service.store.summary()["user_a"]["jobs"] == {"done": 1}
    assert service.store.summary()["user_b"]["jobs"] == {"done": 1}


def test_broken_account_does_not_block_other_queues(setup_service):
    service, fake, *_ = setup_service
    fake.add_asset("user_b")
    fake.add_asset("shared")
    fake.disabled.add("user_b")
    service.scan()
    assert "user_b" in service.status["accounts"]
    assert {a for a, _, _ in fake.writes} == {"user_a", "shared"}


def test_missing_reference_stops_instead_of_training_partial_classifier(
    setup_service, monkeypatch
):
    service, fake, *_ = setup_service
    monkeypatch.setattr(
        service,
        "train",
        lambda doc: (_ for _ in ()).throw(RuntimeError("Reference unavailable")),
    )
    service.scan()
    assert service.status["error"] == "Reference unavailable"
    assert not fake.writes
    assert service.store.summary()["user_a"]["jobs"] == {"pending": 1}


def test_shared_asset_rejected_for_enrollment(setup_service):
    service, fake, pet, *_ = setup_service
    asset = fake.add_asset("user_b")
    with pytest.raises(ValueError, match="not owned"):
        service.add_reference("user_a", asset, pet)
    assert len(service.store.enrollment()["pets"][pet]["refs"]) == 1


def test_reset_preserves_enrollment_and_other_accounts(setup_service):
    service, fake, pet, *_ = setup_service
    fake.add_asset("user_b")
    service.scan()
    before = service.store.enrollment()["pets"][pet]
    service.reset_pet(pet, "user_a")
    after = service.store.enrollment()["pets"][pet]
    assert after["refs"] == before["refs"]
    assert "user_a" not in after["persons"]
    assert after["persons"]["user_b"] == before["persons"]["user_b"]
    service.scan(since="1970-01-01T00:00:00.000Z")
    assert (
        service.store.enrollment()["pets"][pet]["persons"]["user_a"]
        != before["persons"]["user_a"]
    )


def test_delete_partial_failure_can_be_retried(setup_service):
    service, fake, pet, *_ = setup_service
    fake.add_asset("user_b")
    service.scan()
    fake.disabled.add("user_b")
    with pytest.raises(RuntimeError):
        service.delete_pet(pet)
    assert "user_a" not in service.store.enrollment()["pets"][pet]["persons"]
    fake.disabled.clear()
    service.delete_pet(pet)
    assert pet not in service.store.enrollment()["pets"]
    assert not fake.persons


def test_local_remove_keeps_immich_persons(setup_service):
    service, fake, pet, *_ = setup_service
    service.scan()
    before = dict(fake.persons)
    service.delete_pet(pet, local_only=True)
    assert fake.persons == before
    assert pet not in service.store.enrollment()["pets"]


def test_rename_reconciles_all_person_names(setup_service):
    service, fake, pet, *_ = setup_service
    service.scan()
    service.update_pet(pet, {"name": "New name"})
    service.scan()
    assert {p["name"] for p in fake.persons.values()} == {"New name"}


def test_lost_person_recreated_without_reenrollment(setup_service):
    service, fake, pet, *_ = setup_service
    service.scan()
    before = service.store.enrollment()["pets"][pet]
    del fake.persons[before["persons"]["shared"]]
    service.scan()
    after = service.store.enrollment()["pets"][pet]
    assert after["refs"] == before["refs"]
    assert after["persons"]["shared"] != before["persons"]["shared"]


def test_contexts_do_not_cross_threads(monkeypatch):
    barrier = threading.Barrier(3)
    monkeypatch.setattr(immich, "IMMICH_API_KEY", "legacy")

    def run(name):
        with accounts.using(
            accounts.Account(name, "secret-" + name, owner_id="owner-" + name)
        ):
            barrier.wait()
            return immich.headers()["x-api-key"], immich.get_owner_id()

    with ThreadPoolExecutor(3) as pool:
        assert list(pool.map(run, ["a", "b", "c"])) == [
            ("secret-a", "owner-a"),
            ("secret-b", "owner-b"),
            ("secret-c", "owner-c"),
        ]
    assert immich.headers()["x-api-key"] == "legacy"


def test_review_tag_cache_is_account_specific(setup_service):
    service, *_ = setup_service
    for name, account in service.accounts.items():
        with accounts.using(account):
            assert immich.resolve_review_tag_id_sync() == "tag-" + name


def test_owner_cannot_change_after_restart(tmp_path):
    store = Store(tmp_path)
    store.bind("a", "owner-a")
    store.close()
    store = Store(tmp_path)
    with pytest.raises(ValueError, match="different stored"):
        store.bind("a", "owner-b")
    with pytest.raises(ValueError, match="same Immich"):
        store.bind("b", "owner-a")
    store.close()


def test_queue_restart_dedup_and_equal_timestamp(tmp_path):
    store = Store(tmp_path)
    store.bind("a", "owner-a")
    store.enqueue("a", [("one", "2020"), ("two", "2020")], cursor="2026")
    store.complete("a", "one", [])
    store.failed("a", "two", "unavailable")
    store.close()
    store = Store(tmp_path)
    store.enqueue(
        "a", [("one", "2020"), ("two", "2020"), ("three", "2020")], cursor="2026"
    )
    assert store.summary()["a"]["jobs"] == {"done": 1, "pending": 1, "retry": 1}
    assert store.cursor("a") == "2026"
    store.close()


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"id": "../a", "api_key": "key"}],
        [{"id": "a"}],
        [{"id": "a", "api_key": "x"}, {"id": "a", "api_key": "y"}],
        [{"id": "a", "api_key": "x"}, {"id": "b", "api_key": "x"}],
    ],
)
def test_invalid_account_config(tmp_path, entries):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(entries))
    with pytest.raises(ValueError):
        accounts.load_accounts(path)


def test_keys_can_come_from_environment_and_are_not_represented(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "never-display-this")
    path = tmp_path / "accounts.json"
    path.write_text('[{"id":"a","api_key_env":"TEST_KEY"}]')
    account = accounts.load_accounts(path)["a"]
    assert account.api_key == "never-display-this"
    assert "never-display-this" not in repr(account)


def test_shared_classifier_fetches_each_reference_with_its_owner(monkeypatch):
    calls = []

    def embed(asset, **kwargs):
        calls.append((accounts.current().id, asset))
        return [np.array([1.0, 0.0]) if asset != "negative" else np.array([0.0, 1.0])]

    monkeypatch.setattr(classifier.emb, "embed_asset_crops", embed)

    @contextmanager
    def context(ref):
        with accounts.using(accounts.Account(ref["account"], "key")):
            yield

    result = classifier.build_classifier(
        ["pet"],
        {
            "pet": [
                {"account": "a", "asset_id": "ref-a"},
                {"account": "b", "asset_id": "ref-b"},
            ]
        },
        [{"account": "c", "asset_id": "negative"}],
        reference_context=context,
        strict=True,
    )
    assert result is not None
    assert calls == [("a", "ref-a"), ("b", "ref-b"), ("c", "negative")]
    assert accounts.current() is None


def test_strict_classifier_rejects_partial_refs(monkeypatch):
    monkeypatch.setattr(
        classifier.emb,
        "embed_asset_crops",
        lambda aid, **kw: [] if aid == "missing" else [np.array([1.0, 0.0])],
    )
    with pytest.raises(RuntimeError, match="not partially"):
        classifier.build_classifier(
            ["pet"], {"pet": [{"asset_id": "ok"}, {"asset_id": "missing"}]}, strict=True
        )


def test_edit_conflict_during_scan(setup_service):
    service, *_ = setup_service
    with service.exclusive():
        with pytest.raises(BusyError):
            service.create_pet("Other pet")


def test_api_validates_owner_and_never_returns_keys(setup_service):
    service, fake, pet, ref, *_ = setup_service
    app = FastAPI()
    app.include_router(create_router(service))
    client = TestClient(app)
    response = client.get("/api/multi/state")
    assert response.status_code == 200 and "secret-" not in response.text
    other = fake.add_asset("user_b")
    response = client.post(
        "/api/multi/references",
        json={"account": "user_a", "asset_id": other, "pet_id": pet},
    )
    assert response.status_code == 400
    assert client.get(f"/api/multi/thumb/user_a/{other}").status_code == 400
    assert client.get(f"/api/multi/thumb/user_a/{ref}").status_code == 200
    assert (
        client.post("/api/multi/scan", json={"account": "missing"}).status_code == 400
    )
    assert (
        client.post("/api/multi/scan", json={"until": "2026-01-01"}).status_code == 422
    )
    assert (
        client.post(
            "/api/multi/references",
            json={
                "account": "user_a",
                "asset_id": ref,
                "pet_id": pet,
                "bbox": [0.8, 0, 0.1, 1],
            },
        ).status_code
        == 422
    )
    with service.exclusive():
        assert (
            client.post("/api/multi/pets", json={"name": "Another pet"}).status_code
            == 409
        )


def test_cancellation_leaves_unstarted_jobs_pending(setup_service, monkeypatch):
    service, fake, *_ = setup_service
    fake.add_asset("user_b")
    fake.add_asset("shared")
    process = service.process

    def one_then_stop(*args):
        process(*args)
        service.cancel.set()

    monkeypatch.setattr(service, "process", one_then_stop)
    service.scan()
    assert len(fake.writes) == 1
    assert service.status["cancelled"]
    assert service.store.summary()["user_b"]["jobs"] == {"pending": 1}
    service.cancel.clear()
    monkeypatch.setattr(service, "process", process)
    service.scan()
    assert len(fake.writes) == 3


def test_owner_verification_fails_closed(monkeypatch):
    monkeypatch.setattr(accounts.requests, "request", lambda *a, **kw: Reply({}, 403))
    with accounts.using(accounts.Account("a", "key")):
        with pytest.raises(RuntimeError, match="403"):
            immich.get_owner_id()


def test_retry_only_attempted_once_in_a_cycle(setup_service):
    service, fake, pet, ref, *_ = setup_service
    fake.thumbnail_failures.add(ref)
    service.scan()
    row = service.store.db.execute(
        "SELECT attempts,status FROM jobs WHERE asset=?", (ref,)
    ).fetchone()
    assert tuple(row) == (1, "retry")


def test_date_range_does_not_tag_outside_pet_lifetime(setup_service):
    service, fake, pet, *_ = setup_service
    service.update_pet(pet, {"since": "2021-01-01"})
    service.scan()
    assert not fake.writes
    assert service.store.summary()["user_a"]["jobs"] == {"done": 1}


def test_main_multi_mode_routes_and_single_worker(tmp_path):
    # Fresh interpreter proves import ordering sets worker count before ML modules.
    import os
    import subprocess
    import sys
    from pathlib import Path

    config = tmp_path / "accounts.json"
    config.write_text('[{"id":"a","api_key":"test-key"}]')
    root = Path(__file__).resolve().parents[1]
    script = """
import conftest
import main
import detector, embedder
from fastapi.testclient import TestClient
assert detector.YOLO_WORKERS == embedder.GPU_WORKERS == 1
with TestClient(main.app) as client:
    assert client.get('/').status_code == 200
    assert 'Family pet tagger' in client.get('/').text
    assert client.get('/api/multi/state').json()['accounts'] == ['a']
    assert client.get('/api/pets').status_code == 404
    assert client.get('/api/status').json()['mode'] == 'multi-account'
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(root / "tests"), str(root / "app")]),
            "IMMICH_ACCOUNTS_FILE": str(config),
            "DATA_DIR": str(tmp_path / "data"),
            "GPU_WORKERS": "9",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_discovery_failure_preserves_pages_without_advancing_watermark(
    setup_service, monkeypatch
):
    service, fake, *_ = setup_service
    account = service.account("user_a")
    old = service.store.cursor(account.id)

    def pages(*args, **kwargs):
        yield list(fake.assets.values())
        raise RuntimeError("second page unavailable")

    monkeypatch.setattr(immich, "_search_metadata_pages", pages)
    with pytest.raises(RuntimeError):
        service.discover(account)
    assert service.store.cursor(account.id) == old
    assert service.store.summary()[account.id]["jobs"] == {"pending": 1}


def test_model_failure_during_training_prevents_writes(setup_service, monkeypatch):
    service, fake, *_ = setup_service
    counts = iter([0, 1])
    monkeypatch.setattr(
        multi_service.inference, "model_error_count", lambda: next(counts)
    )
    service.scan()
    assert service.status["error"] == "Model failure while preparing shared references"
    assert not fake.writes
