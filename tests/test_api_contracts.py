"""The obligation and contract endpoints, over the same synchronous client."""

from __future__ import annotations

import pytest

from binyard.api import create_app
from binyard.bushels import bushels
from binyard.config import load_settings
from binyard.inventory import Bin, BinKind
from binyard.store.sqlite import SqliteUnitOfWork, connect
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
    yard = work.yard.load().with_bin(Bin("N1", "North 1", BinKind.UPRIGHT, bushels("120000")))
    work.yard.save(yard)
    work.commit()
    work.close()
    return Client(application)


def contract_body(**overrides):
    body = {
        "identifier": "C-2025-014",
        "account": "HALVORSEN FARMS",
        "commodity": "corn",
        "quantity": "5000",
        "delivery_from": "2025-10-01",
        "delivery_to": "2025-12-31",
        "price_kind": "flat",
        "price": "4.5225",
    }
    body.update(overrides)
    return body


class TestContracts:
    def test_a_flat_contract_can_be_written(self, client):
        response = client.post("/contracts", json=contract_body())
        assert response.status_code == 201
        payload = response.json()
        assert payload["price"] == "$4.5225"
        assert payload["fill_state"] == "empty"

    def test_a_flat_contract_without_a_price_is_refused(self, client):
        response = client.post("/contracts", json=contract_body(price=None))
        assert response.status_code == 400

    def test_a_duplicate_identifier_is_a_conflict(self, client):
        client.post("/contracts", json=contract_body())
        assert client.post("/contracts", json=contract_body()).status_code == 409

    def test_a_basis_contract_starts_unpriced_and_can_be_fixed(self, client):
        client.post(
            "/contracts",
            json=contract_body(
                identifier="C-2025-021",
                price_kind="basis",
                price=None,
                basis="-0.35",
                futures_month="CZ25",
            ),
        )
        assert client.get("/contracts/C-2025-021").json()["price"] is None
        fixed = client.post("/contracts/C-2025-021/futures", json={"price": "4.70"})
        assert fixed.status_code == 200
        assert fixed.json()["price"] == "$4.35"

    def test_fixing_futures_on_a_flat_contract_is_refused(self, client):
        client.post("/contracts", json=contract_body())
        response = client.post("/contracts/C-2025-014/futures", json={"price": "4.70"})
        assert response.status_code == 400

    def test_listing_needs_all_three_narrowing_terms(self, client):
        assert client.get("/contracts").status_code == 400
        assert client.get("/contracts?account=HALVORSEN FARMS").status_code == 400

    def test_listing_finds_the_open_ones(self, client):
        client.post("/contracts", json=contract_body())
        payload = client.get(
            f"/contracts?account=HALVORSEN FARMS&commodity=CORN&on={DAY}"
        ).json()
        assert payload["count"] == 1

    def test_a_malformed_date_on_the_listing(self, client):
        assert (
            client.get(
                "/contracts?account=HALVORSEN FARMS&commodity=CORN&on=14-10-2025"
            ).status_code
            == 400
        )

    def test_a_contract_that_is_not_there(self, client):
        assert client.get("/contracts/C-NOPE").status_code == 404

    def test_a_delivery_lands_against_the_contract(self, client):
        client.post("/contracts", json=contract_body())
        client.post("/tickets", json=load())
        payload = client.get("/contracts/C-2025-014").json()
        assert payload["delivered"] != "0.00 bu"
        assert payload["fill_state"] == "short"


class TestObligations:
    def test_an_obligation_appears_when_grain_is_stored(self, client):
        client.post("/tickets", json=load())
        payload = client.get("/obligations").json()
        assert payload["count"] == 1
        assert payload["obligations"][0]["kind"] == "open_storage"

    def test_there_is_no_way_to_open_one_directly(self, client):
        assert client.post("/obligations", json={}).status_code == 405

    def test_listing_by_account_needs_a_commodity(self, client):
        client.post("/tickets", json=load())
        assert client.get("/obligations?account=HALVORSEN FARMS").status_code == 400
        assert (
            client.get("/obligations?account=HALVORSEN FARMS&commodity=CORN").json()["count"]
            == 1
        )

    def test_an_obligation_that_is_not_there(self, client):
        assert client.get("/obligations/OP-NOPE").status_code == 404

    def test_open_storage_cannot_be_priced(self, client):
        client.post("/tickets", json=load())
        identifier = client.get("/obligations").json()["obligations"][0]["identifier"]
        response = client.post(
            f"/obligations/{identifier}/price", json={"price": "4.52", "on": DAY}
        )
        assert response.status_code == 409

    def test_a_deferred_price_obligation_can_be_priced(self, client):
        body = load()
        body["storage_kind"] = "deferred_price"
        client.post("/tickets", json=body)
        identifier = client.get("/obligations").json()["obligations"][0]["identifier"]
        response = client.post(
            f"/obligations/{identifier}/price", json={"price": "4.5225", "on": DAY}
        )
        assert response.status_code == 200
        assert response.json()["priced_at"] == "$4.5225"
        assert response.json()["value"] != "$0.00"

    def test_cancelling_takes_it_off_the_open_list(self, client):
        client.post("/tickets", json=load())
        identifier = client.get("/obligations").json()["obligations"][0]["identifier"]
        assert client.post(f"/obligations/{identifier}/cancel", json={}).status_code == 200
        assert client.get("/obligations").json()["count"] == 0

    def test_cancelling_twice_is_refused(self, client):
        client.post("/tickets", json=load())
        identifier = client.get("/obligations").json()["obligations"][0]["identifier"]
        client.post(f"/obligations/{identifier}/cancel", json={})
        assert client.post(f"/obligations/{identifier}/cancel", json={}).status_code == 409

    def test_naming_a_replacement_that_is_not_there_is_a_four_oh_four(self, client):
        client.post("/tickets", json=load())
        identifier = client.get("/obligations").json()["obligations"][0]["identifier"]
        response = client.post(
            f"/obligations/{identifier}/cancel", json={"superseded_by": "WR-NOPE"}
        )
        assert response.status_code == 404

    def test_a_receipt_can_name_a_replacement_that_exists(self, client):
        client.post("/tickets", json=load())
        second = load()
        second["weights"] = {"gross": "78000", "tare": "30000"}
        client.post("/tickets", json=second)
        listing = client.get("/obligations").json()["obligations"]
        first, other = listing[0]["identifier"], listing[1]["identifier"]
        payload = client.post(
            f"/obligations/{first}/cancel", json={"superseded_by": other}
        ).json()
        assert payload["status"] == "cancelled"

    def test_storage_accrues_to_a_date(self, client):
        client.post("/tickets", json=load())
        identifier = client.get("/obligations").json()["obligations"][0]["identifier"]
        payload = client.get(f"/obligations/{identifier}/storage?through=2026-03-01").json()
        assert payload["total"] != "$0.00"
        assert payload["segments"]

    def test_nothing_accrues_inside_the_free_period(self, client):
        client.post("/tickets", json=load())
        identifier = client.get("/obligations").json()["obligations"][0]["identifier"]
        payload = client.get(f"/obligations/{identifier}/storage?through={DAY}").json()
        assert payload["total"] == "$0.00"

    def test_the_accrual_convention_can_be_chosen(self, client):
        client.post("/tickets", json=load())
        identifier = client.get("/obligations").json()["obligations"][0]["identifier"]
        monthly = client.get(f"/obligations/{identifier}/storage?through=2026-01-15").json()
        daily = client.get(
            f"/obligations/{identifier}/storage?through=2026-01-15&accrual=per_day"
        ).json()
        assert monthly["total"] != daily["total"]

    def test_an_unknown_accrual_lists_the_ones_there_are(self, client):
        client.post("/tickets", json=load())
        identifier = client.get("/obligations").json()["obligations"][0]["identifier"]
        response = client.get(f"/obligations/{identifier}/storage?accrual=weekly")
        assert response.status_code == 400
        assert "per_day" in response.json()["detail"]["allowed"]

    def test_a_malformed_through_date(self, client):
        client.post("/tickets", json=load())
        identifier = client.get("/obligations").json()["obligations"][0]["identifier"]
        assert (
            client.get(f"/obligations/{identifier}/storage?through=1-3-2026").status_code == 400
        )
