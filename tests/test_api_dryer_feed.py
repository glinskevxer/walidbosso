"""The dryer and feed endpoints."""

from __future__ import annotations

import pytest

from binyard.api import create_app
from binyard.bushels import bushels
from binyard.config import load_settings
from binyard.inventory import Bin, BinKind
from binyard.store.sqlite import SqliteUnitOfWork, connect
from test_api import Client, load

DAY = "2025-10-14"


def feed_line(sequence=1, moisture="0150", crop="CORN"):
    return (
        f"IN{sequence:06d}20251014093015{'HALVORSEN FARMS':<20}{crop:<8}"
        f"0784200003018000{moisture:<4}{'0570':<4}{'TRAVIS':<12}"
    )


@pytest.fixture
def database(tmp_path):
    return str(tmp_path / "binyard.sqlite3")


@pytest.fixture
def client(database):
    settings = load_settings({"BINYARD_DATABASE_URL": f"sqlite:///{database}"})
    application = create_app(settings)
    work = SqliteUnitOfWork(connect(database, run_migrations=False))
    yard = work.yard.load()
    for identifier, name in (("N1", "North 1"), ("WET", "Wet bin"), ("DRY", "Dry bin")):
        yard = yard.with_bin(Bin(identifier, name, BinKind.UPRIGHT, bushels("200000")))
    work.yard.save(yard)
    work.commit()
    work.close()
    return Client(application)


def stock(client, moisture="19.5", into="WET"):
    body = load(moisture=moisture)
    body["bin"] = into
    body["sample"]["moisture"] = moisture
    return client.post("/tickets", json=body).json()


class TestDryer:
    def test_nothing_is_on_the_dryer_to_start_with(self, client):
        assert client.get("/dryer").json()["count"] == 0

    def test_a_run_can_be_started_and_read_back(self, client):
        stock(client)
        response = client.post(
            "/dryer",
            json={
                "identifier": "DR-1",
                "from": "WET",
                "to": "DRY",
                "quantity": "500",
                "at": "2025-10-15T06:00:00",
            },
        )
        assert response.status_code == 201
        assert response.json()["status"] == "drying"
        assert client.get("/dryer").json()["count"] == 1
        assert client.get("/dryer/DR-1").json()["wet_bushels"] == "500.00 bu"

    def test_a_run_off_an_empty_bin_is_refused(self, client):
        response = client.post(
            "/dryer",
            json={"identifier": "DR-1", "from": "WET", "to": "DRY", "quantity": "500"},
        )
        assert response.status_code == 400

    def test_a_duplicate_run_identifier_is_a_conflict(self, client):
        stock(client)
        body = {"identifier": "DR-1", "from": "WET", "to": "DRY", "quantity": "500"}
        client.post("/dryer", json=body)
        assert client.post("/dryer", json=body).status_code == 409

    def test_finishing_reports_the_variance_and_the_fuel(self, client):
        stock(client)
        client.post(
            "/dryer",
            json={
                "identifier": "DR-1",
                "from": "WET",
                "to": "DRY",
                "quantity": "500",
                "at": "2025-10-15T06:00:00",
            },
        )
        response = client.post(
            "/dryer/DR-1/finish",
            json={
                "dry_bushels": "470",
                "dry_moisture": "15.0",
                "at": "2025-10-15T18:00:00",
                "fuel_gallons": "62.5",
                "fuel_price": "1.80",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["posted_loss"] == "30.00 bu"
        assert payload["run"]["fuel_cost"] == "$112.50"
        assert payload["run"]["variance"]["actual_out"] == "470.00 bu"
        assert client.get("/dryer").json()["count"] == 0

    def test_the_dry_bin_ends_up_with_the_new_moisture(self, client):
        stock(client)
        client.post(
            "/dryer",
            json={"identifier": "DR-1", "from": "WET", "to": "DRY", "quantity": "500"},
        )
        client.post(
            "/dryer/DR-1/finish",
            json={"dry_bushels": "470", "dry_moisture": "15.0"},
        )
        quality = client.get("/bins/DRY").json()["contents"]["quality"]
        assert quality["moisture"] == "15%"

    def test_finishing_a_run_that_is_not_there(self, client):
        response = client.post(
            "/dryer/DR-9/finish", json={"dry_bushels": "1", "dry_moisture": "15.0"}
        )
        assert response.status_code == 404

    def test_a_run_can_be_abandoned_with_a_reason(self, client):
        stock(client)
        client.post(
            "/dryer",
            json={"identifier": "DR-1", "from": "WET", "to": "DRY", "quantity": "500"},
        )
        response = client.post(
            "/dryer/DR-1/abandon",
            json={"reason": "burner tripped", "dry_bushels": "120", "dry_moisture": "18.0"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "abandoned"
        assert response.json()["note"] == "burner tripped"

    def test_abandoning_needs_a_reason(self, client):
        stock(client)
        client.post(
            "/dryer",
            json={"identifier": "DR-1", "from": "WET", "to": "DRY", "quantity": "500"},
        )
        response = client.post(
            "/dryer/DR-1/abandon", json={"dry_bushels": "120", "dry_moisture": "18.0"}
        )
        assert response.status_code == 400


class TestFeed:
    def _post(self, client, text, query="?bin=CORN:N1"):
        return client.post(
            f"/feed{query}",
            content=text.encode("ascii"),
            headers={"content-type": "text/plain"},
        )

    def test_a_file_becomes_tickets(self, client):
        text = "\n".join(feed_line(sequence=index) for index in (1, 2, 3))
        response = self._post(client, text)
        assert response.status_code == 201
        payload = response.json()
        assert payload["counts"]["taken"] == 3
        assert payload["parsed"]["accepted"] == 3
        assert client.get(f"/tickets?day={DAY}").json()["count"] == 3

    def test_the_same_file_twice_takes_nothing(self, client):
        text = feed_line()
        self._post(client, text, "?bin=CORN:N1&source=monday.dat")
        again = self._post(client, text, "?bin=CORN:N1&source=monday.dat")
        assert again.status_code == 200
        assert again.json()["counts"]["duplicate"] == 1

    def test_a_dry_run_writes_nothing(self, client):
        response = self._post(client, feed_line(), "?bin=CORN:N1&dry_run=true")
        assert response.status_code == 200
        assert response.json()["dry_run"] is True
        assert client.get(f"/tickets?day={DAY}").json()["count"] == 0

    def test_an_empty_body_is_refused(self, client):
        assert self._post(client, "   ").status_code == 400

    def test_json_is_refused_with_a_reason(self, client):
        response = client.post("/feed?bin=CORN:N1", json={"records": []})
        assert response.status_code == 400
        assert "text/plain" in response.json()["message"]

    def test_no_bin_mapping_is_refused(self, client):
        assert self._post(client, feed_line(), "").status_code == 400

    def test_a_malformed_bin_mapping_is_refused(self, client):
        response = self._post(client, feed_line(), "?bin=CORN")
        assert response.status_code == 400
        assert "CROP:BIN" in response.json()["message"]

    def test_an_unreadable_line_is_reported_not_fatal(self, client):
        response = self._post(client, f"{feed_line()}\nnonsense")
        assert response.status_code == 201
        assert response.json()["rejected_lines"] == 1
        assert response.json()["clean"] is False

    def test_the_history_of_one_file_reads_back(self, client):
        self._post(client, feed_line(), "?bin=CORN:N1&source=monday.dat")
        payload = client.get("/feed/monday.dat").json()
        assert payload["count"] == 1
        assert payload["entries"][0]["outcome"] == "taken"

    def test_a_file_nobody_has_sent_has_no_history(self, client):
        assert client.get("/feed/never.dat").json()["count"] == 0
