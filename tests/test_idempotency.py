"""Retried posts, and what makes two of them the same post."""

from __future__ import annotations

from datetime import datetime

import pytest

from binyard.api import create_app
from binyard.bushels import bushels
from binyard.config import load_settings
from binyard.errors import Conflict
from binyard.inventory import Bin, BinKind
from binyard.store import MemoryUnitOfWork, SqliteUnitOfWork
from binyard.store.replies import StoredReply, digest
from binyard.store.sqlite import connect
from test_api import Client, load

DAY = "2025-10-14"


@pytest.fixture
def database(tmp_path):
    return str(tmp_path / "binyard.sqlite3")


@pytest.fixture
def client(database):
    settings = load_settings({"BINYARD_DATABASE_URL": f"sqlite:///{database}"})
    application = create_app(settings)
    work = SqliteUnitOfWork(connect(database, run_migrations=False))
    yard = work.yard.load().with_bin(Bin("N1", "North 1", BinKind.UPRIGHT, bushels("200000")))
    work.yard.save(yard)
    work.commit()
    work.close()
    return Client(application)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, database):
    if request.param == "memory":
        return MemoryUnitOfWork().replies
    unit = SqliteUnitOfWork.open()
    return unit.replies


def reply(key="abc", route="POST /tickets", request_hash="hash", status=201, body="{}"):
    return StoredReply(
        key=key,
        route=route,
        request_hash=request_hash,
        status=status,
        body=body,
        stored_at=datetime(2025, 10, 14, 12, 0),
    )


class TestTheStore:
    def test_an_unknown_key_has_no_reply(self, store):
        assert store.get("nothing") is None
        assert store.replay("nothing", "POST /tickets", "hash") is None

    def test_a_stored_reply_comes_back(self, store):
        store.remember(reply())
        found = store.replay("abc", "POST /tickets", "hash")
        assert found is not None
        assert found.status == 201

    def test_the_same_key_for_a_different_body_is_a_conflict(self, store):
        store.remember(reply())
        with pytest.raises(Conflict):
            store.replay("abc", "POST /tickets", "a different hash")

    def test_the_same_key_on_a_different_route_is_a_conflict(self, store):
        store.remember(reply())
        with pytest.raises(Conflict):
            store.replay("abc", "POST /bins", "hash")

    def test_remembering_twice_keeps_the_later_one(self, store):
        store.remember(reply(body="{}"))
        store.remember(reply(body='{"one": 1}'))
        assert store.get("abc").body == '{"one": 1}'


class TestDigest:
    def test_the_same_request_hashes_the_same(self):
        assert digest("POST /tickets", b"body") == digest("POST /tickets", b"body")

    def test_a_different_body_hashes_differently(self):
        assert digest("POST /tickets", b"one") != digest("POST /tickets", b"two")

    def test_a_different_route_hashes_differently(self):
        assert digest("POST /tickets", b"body") != digest("POST /bins", b"body")


class TestOverHttp:
    def test_without_a_key_a_repeat_is_a_second_load(self, client):
        first = client.post("/tickets", json=load())
        second = client.post("/tickets", json=load())
        assert first.json()["ticket"]["number"] == "WBS-25-000001"
        assert second.json()["ticket"]["number"] == "WBS-25-000002"

    def test_with_a_key_a_retry_replays_the_first_answer(self, client):
        headers = {"idempotency-key": "scale-house-1"}
        first = client.post("/tickets", json=load(), headers=headers)
        second = client.post("/tickets", json=load(), headers=headers)
        assert first.status_code == second.status_code == 201
        assert first.json() == second.json()
        assert second.headers["idempotent-replay"] == "true"
        assert client.get(f"/tickets?day={DAY}").json()["count"] == 1

    def test_a_replay_writes_nothing_to_the_bin(self, client):
        headers = {"idempotency-key": "scale-house-1"}
        client.post("/tickets", json=load(), headers=headers)
        held = client.get("/bins/N1").json()["contents"]["quantity"]
        client.post("/tickets", json=load(), headers=headers)
        assert client.get("/bins/N1").json()["contents"]["quantity"] == held

    def test_the_same_key_for_a_different_load_is_a_conflict(self, client):
        headers = {"idempotency-key": "scale-house-1"}
        client.post("/tickets", json=load(), headers=headers)
        other = load()
        other["weights"] = {"gross": "78000", "tare": "30000"}
        response = client.post("/tickets", json=other, headers=headers)
        assert response.status_code == 409
        assert response.json()["code"] == "conflict"

    def test_different_keys_are_different_loads(self, client):
        client.post("/tickets", json=load(), headers={"idempotency-key": "one"})
        client.post("/tickets", json=load(), headers={"idempotency-key": "two"})
        assert client.get(f"/tickets?day={DAY}").json()["count"] == 2

    def test_a_refused_load_is_not_remembered(self, client):
        headers = {"idempotency-key": "scale-house-1"}
        first = client.post("/tickets", json=load(moisture="27.0"), headers=headers)
        assert first.status_code == 400
        second = client.post("/tickets", json=load(moisture="27.0"), headers=headers)
        assert second.status_code == 400
        assert "idempotent-replay" not in second.headers

    def test_a_key_reused_after_a_refusal_can_take_a_good_load(self, client):
        headers = {"idempotency-key": "scale-house-1"}
        client.post("/tickets", json=load(moisture="27.0"), headers=headers)
        response = client.post("/tickets", json=load(), headers=headers)
        assert response.status_code == 201
