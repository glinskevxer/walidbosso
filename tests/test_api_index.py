"""The self description at the root, and the events the services log."""

from __future__ import annotations

import io
import json
import logging

import pytest

from binyard import __version__
from binyard.api import create_app
from binyard.bushels import bushels
from binyard.config import load_settings
from binyard.inventory import Bin, BinKind
from binyard.observability import clear, configure
from binyard.store.sqlite import SqliteUnitOfWork, connect
from test_api import Client, load


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


class TestIndex:
    def test_the_root_describes_the_build(self, client):
        payload = client.get("/").json()
        assert payload["service"] == "binyard"
        assert payload["version"] == __version__
        assert payload["location"] == "WBS"
        assert "CORN" in payload["commodities"]

    def test_every_route_appears_with_a_method(self, client):
        payload = client.get("/").json()
        assert payload["count"] == len(payload["routes"])
        assert payload["count"] > 25
        for route in payload["routes"]:
            assert route["method"] in {"GET", "POST"}
            assert route["path"].startswith("/")

    def test_the_index_does_not_list_itself(self, client):
        paths = {route["path"] for route in client.get("/").json()["routes"]}
        assert "/" not in paths

    def test_the_important_routes_carry_a_purpose(self, client):
        purposes = {
            route["name"]: route["purpose"] for route in client.get("/").json()["routes"]
        }
        assert "weigh, grade" in purposes["create_ticket"]
        assert purposes["examiner"]

    def test_the_probes_are_marked_open(self, client):
        routes = {route["path"]: route["open"] for route in client.get("/").json()["routes"]}
        assert routes["/health"] is True
        assert routes["/tickets"] is False

    def test_it_says_when_a_key_is_needed(self, database):
        settings = load_settings(
            {"BINYARD_DATABASE_URL": f"sqlite:///{database}", "BINYARD_API_KEY": "s3cret"}
        )
        keyed = Client(create_app(settings), {"x-api-key": "s3cret"})
        payload = keyed.get("/").json()
        assert payload["authentication"]["required"] is True
        assert payload["authentication"]["header"] == "x-api-key"

    def test_it_never_prints_the_key(self, database):
        settings = load_settings(
            {"BINYARD_DATABASE_URL": f"sqlite:///{database}", "BINYARD_API_KEY": "s3cret"}
        )
        keyed = Client(create_app(settings), {"x-api-key": "s3cret"})
        assert "s3cret" not in keyed.get("/").text

    def test_the_index_needs_a_key_when_one_is_configured(self, database):
        settings = load_settings(
            {"BINYARD_DATABASE_URL": f"sqlite:///{database}", "BINYARD_API_KEY": "s3cret"}
        )
        assert Client(create_app(settings)).get("/").status_code == 401


class TestServiceLogs:
    @pytest.fixture
    def logged(self):
        buffer = io.StringIO()
        configure("INFO", json_output=True, stream=buffer)
        yield buffer
        clear()
        logging.getLogger().handlers.clear()

    def events(self, buffer) -> list[dict]:
        return [json.loads(line) for line in buffer.getvalue().splitlines()]

    def test_taking_a_load_logs_it_with_the_ticket_bound(self, client, logged):
        client.post("/tickets", json=load())
        received = [item for item in self.events(logged) if item["event"] == "load received"]
        assert received
        assert received[0]["ticket"] == "WBS-25-000001"
        assert received[0]["commodity"] == "CORN"
        assert received[0]["net"].endswith("bu")

    def test_a_load_turned_away_says_why(self, client, logged):
        client.post("/tickets", json=load(moisture="27.0"))
        turned = [
            item
            for item in self.events(logged)
            if item["event"] == "load turned away at the pit"
        ]
        assert turned
        assert turned[0]["moisture"] == "27%"

    def test_every_line_carries_the_request_id(self, client, logged):
        response = client.post("/tickets", json=load())
        request_id = response.headers["x-request-id"]
        lines = [item for item in self.events(logged) if "request_id" in item]
        assert lines
        assert all(item["request_id"] == request_id for item in lines)

    def test_the_counters_move_when_a_load_is_taken(self, client):
        before = client.get("/metrics").text
        client.post("/tickets", json=load())
        after = client.get("/metrics").text
        assert "loads_received_total" in after
        assert after != before

    def test_a_refused_load_is_counted_separately(self, client):
        client.post("/tickets", json=load(moisture="27.0"))
        metrics = client.get("/metrics").text
        assert 'loads_refused_total{commodity="CORN",reason="wet"}' in metrics
