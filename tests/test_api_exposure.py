"""The market position, over HTTP and at the command line."""

from __future__ import annotations

import pytest

from binyard.api import create_app
from binyard.bushels import bushels
from binyard.config import load_settings
from binyard.inventory import Bin, BinKind
from binyard.store.sqlite import SqliteUnitOfWork, connect
from test_api import Client, load
from test_cli import invoke

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


def contract_body(**overrides):
    body = {
        "identifier": "C-2025-021",
        "account": "HALVORSEN FARMS",
        "commodity": "corn",
        "quantity": "5000",
        "delivery_from": "2025-10-01",
        "delivery_to": "2025-12-31",
        "price_kind": "basis",
        "basis": "-0.35",
        "futures_month": "CZ25",
    }
    body.update(overrides)
    return body


class TestOverHttp:
    def test_stored_grain_is_not_the_houses_position(self, client):
        client.post("/tickets", json=load())
        corn = client.get(f"/reports/exposure?on={DAY}").json()["exposures"][0]
        assert corn["customer_owned"] == corn["on_hand"]
        assert corn["owned"] == "0.00 bu"
        assert corn["flat_price_position"] == "0.00 bu"

    def test_contract_grain_is_owned_and_long(self, client):
        client.post("/contracts", json=contract_body(price_kind="flat", price="4.52"))
        client.post("/tickets", json=load())
        corn = client.get(f"/reports/exposure?on={DAY}").json()["exposures"][0]
        assert corn["owned"] != "0.00 bu"
        assert corn["long"] is True

    def test_a_forward_sale_makes_the_house_short(self, client):
        client.post("/tickets", json=load())
        payload = client.get(f"/reports/exposure?on={DAY}&sold=corn:5000").json()
        assert payload["short_in"] == ["CORN"]
        assert payload["exposures"][0]["short"] is True

    def test_an_unfixed_basis_contract_shows_as_basis_exposure(self, client):
        client.post("/contracts", json=contract_body())
        client.post("/tickets", json=load())
        corn = client.get(f"/reports/exposure?on={DAY}").json()["exposures"][0]
        assert corn["basis_unfixed"] != "0.00 bu"

    def test_fixing_the_futures_clears_the_basis_exposure(self, client):
        client.post("/contracts", json=contract_body())
        client.post("/tickets", json=load())
        client.post("/contracts/C-2025-021/futures", json={"price": "4.70"})
        corn = client.get(f"/reports/exposure?on={DAY}").json()["exposures"][0]
        assert corn["basis_unfixed"] == "0.00 bu"

    def test_a_malformed_sale_is_refused(self, client):
        response = client.get(f"/reports/exposure?on={DAY}&sold=corn")
        assert response.status_code == 400
        assert "CROP:BUSHELS" in response.json()["message"]

    def test_a_crop_the_house_does_not_handle(self, client):
        assert client.get(f"/reports/exposure?on={DAY}&sold=rice:100").status_code == 400

    def test_it_renders_as_text_when_asked(self, client):
        client.post("/tickets", json=load())
        response = client.get(f"/reports/exposure?on={DAY}", headers={"accept": "text/plain"})
        assert "Market position" in response.text

    def test_the_index_lists_it(self, client):
        names = {route["name"] for route in client.get("/").json()["routes"]}
        assert "exposure" in names


class TestAtTheCommandLine:
    @pytest.fixture
    def stocked(self, tmp_path):
        database = "sqlite:///" + str(tmp_path / "binyard.sqlite3")
        invoke(database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000")
        invoke(
            database,
            "--on",
            DAY,
            "receive",
            "HALVORSEN FARMS",
            "--crop",
            "corn",
            "--bin",
            "N1",
            "--gross",
            "78420",
            "--tare",
            "30180",
            "--moisture",
            "15.0",
            "--test-weight",
            "57",
        )
        return database

    def test_a_flat_book_exits_zero(self, stocked):
        code, out, _ = invoke(stocked, "--on", DAY, "report", "exposure")
        assert code == 0
        assert "Market position" in out

    def test_a_short_book_exits_non_zero_and_warns(self, stocked):
        code, _, err = invoke(stocked, "--on", DAY, "report", "exposure", "--sold", "corn=5000")
        assert code == 1
        assert "CORN is short" in err

    def test_a_malformed_sale_is_refused(self, stocked):
        code, _, err = invoke(stocked, "--on", DAY, "report", "exposure", "--sold", "corn")
        assert code == 2
        assert "CROP=BUSHELS" in err

    def test_it_writes_to_a_file_like_the_others(self, stocked, tmp_path):
        target = tmp_path / "exposure.csv"
        code, out, _ = invoke(
            stocked,
            "--on",
            DAY,
            "--format",
            "csv",
            "report",
            "--out",
            str(target),
            "exposure",
        )
        assert code == 0
        assert "wrote 1 rows" in out
        assert "CORN" in target.read_text()
