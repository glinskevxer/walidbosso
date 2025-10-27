"""The HTTP surface, driven synchronously.

There is no async test plugin in the dev set on purpose. One small wrapper that
runs a single request through :func:`asyncio.run` is less machinery than a
plugin and it keeps every test a plain function, which matters because most of
the assertions here are about status codes and bodies rather than concurrency.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import httpx
import pytest

from binyard.api import create_app
from binyard.bushels import bushels
from binyard.config import load_settings
from binyard.inventory import Bin, BinKind
from binyard.store.sqlite import SqliteUnitOfWork, connect

DAY = "2025-10-14"


class Client:
    """One request at a time, through the ASGI app, synchronously."""

    def __init__(self, app, headers: dict[str, str] | None = None) -> None:
        self._app = app
        self._headers = headers or {}

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        headers = {**self._headers, **kwargs.pop("headers", {})}

        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://binyard.test"
            ) as client:
                return await client.request(method, url, headers=headers, **kwargs)

        return asyncio.run(go())

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)


@pytest.fixture
def database(tmp_path):
    return str(tmp_path / "binyard.sqlite3")


@pytest.fixture
def settings(database):
    return load_settings({"BINYARD_DATABASE_URL": f"sqlite:///{database}"})


@pytest.fixture
def app(settings, database):
    application = create_app(settings)
    work = SqliteUnitOfWork(connect(database, run_migrations=False))
    yard = work.yard.load()
    yard = yard.with_bin(Bin("N1", "North 1", BinKind.UPRIGHT, bushels("120000")))
    yard = yard.with_bin(Bin("N2", "North 2", BinKind.UPRIGHT, bushels("60000")))
    work.yard.save(yard)
    work.commit()
    work.close()
    return application


@pytest.fixture
def client(app):
    return Client(app)


def load(moisture="15.0", **overrides):
    body = {
        "account": "HALVORSEN FARMS",
        "commodity": "corn",
        "bin": "N1",
        "weighed_on": DAY,
        "weights": {"gross": "78420", "tare": "30180"},
        "sample": {"moisture": moisture, "test_weight": "57.0"},
    }
    body.update(overrides)
    return body


class TestHealth:
    def test_health_answers_without_the_database(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_ready_reports_the_location(self, client, settings):
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json()["location"] == str(settings.location)
        assert response.json()["pending_migrations"] == []

    def test_metrics_are_the_text_format(self, client):
        client.get("/health")
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "http_requests_total" in response.text

    def test_every_response_carries_a_request_id(self, client):
        assert client.get("/health").headers["x-request-id"]

    def test_a_request_id_the_caller_sent_is_kept(self, client):
        response = client.get("/health", headers={"x-request-id": "abc123"})
        assert response.headers["x-request-id"] == "abc123"


class TestReceiving:
    def test_a_load_comes_back_graded_and_applied(self, client):
        response = client.post("/tickets", json=load())
        assert response.status_code == 201
        payload = response.json()
        assert payload["ticket"]["number"] == "WBS-25-000001"
        assert payload["ticket"]["status"] == "applied"
        assert payload["ticket"]["grade"]["label"] == "US No. 1"
        assert payload["obligation"]["kind"] == "open_storage"

    def test_a_wet_load_carries_its_discounts(self, client):
        payload = client.post("/tickets", json=load(moisture="18.5")).json()
        sources = [line["source"] for line in payload["discounts"]["lines"]]
        assert "moisture" in sources

    def test_a_load_too_wet_to_take_is_a_four_hundred(self, client):
        response = client.post("/tickets", json=load(moisture="27.0"))
        assert response.status_code == 400
        assert response.json()["code"] == "ticket_error"

    def test_a_missing_field_names_itself(self, client):
        body = load()
        del body["account"]
        response = client.post("/tickets", json=body)
        assert response.status_code == 400
        assert response.json()["detail"]["field"] == "account"

    def test_a_nested_missing_field_names_its_path(self, client):
        body = load()
        del body["sample"]["moisture"]
        response = client.post("/tickets", json=body)
        assert response.json()["detail"]["field"] == "sample.moisture"

    def test_an_unknown_crop_lists_the_ones_handled(self, client):
        response = client.post("/tickets", json=load(commodity="rice"))
        assert response.status_code == 400
        assert "CORN" in response.json()["detail"]["handled"]

    def test_a_bin_that_is_not_there_is_a_four_oh_four(self, client):
        response = client.post("/tickets", json=load(bin="GHOST"))
        assert response.status_code == 404

    def test_a_body_that_is_not_an_object(self, client):
        response = client.post("/tickets", json=[1, 2, 3])
        assert response.status_code == 400
        assert response.json()["detail"]["field"] == "body"

    def test_the_error_body_carries_the_request_id(self, client):
        response = client.post("/tickets", json=load(moisture="27.0"))
        assert response.json()["request_id"] == response.headers["x-request-id"]


class TestTicketReads:
    def test_a_ticket_can_be_fetched_by_number(self, client):
        client.post("/tickets", json=load())
        response = client.get("/tickets/WBS-25-000001")
        assert response.status_code == 200
        assert response.json()["account"] == "HALVORSEN FARMS"

    def test_a_ticket_that_is_not_there(self, client):
        response = client.get("/tickets/WBS-25-000999")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    def test_a_number_that_is_not_a_ticket_number(self, client):
        response = client.get("/tickets/nonsense")
        assert response.status_code == 400

    def test_listing_by_day(self, client):
        client.post("/tickets", json=load())
        assert client.get(f"/tickets?day={DAY}").json()["count"] == 1
        assert client.get("/tickets?day=2025-10-15").json()["count"] == 0

    def test_listing_by_account(self, client):
        client.post("/tickets", json=load())
        assert client.get("/tickets?account=HALVORSEN FARMS").json()["count"] == 1
        assert client.get("/tickets?account=OTHER FARMS").json()["count"] == 0

    def test_asking_both_ways_at_once_is_refused(self, client):
        response = client.get(f"/tickets?day={DAY}&account=HALVORSEN FARMS")
        assert response.status_code == 400

    def test_a_malformed_day_is_refused(self, client):
        assert client.get("/tickets?day=14-10-2025").status_code == 400


class TestBins:
    def test_the_bins_are_listed(self, client):
        payload = client.get("/bins").json()
        assert [item["identifier"] for item in payload["bins"]] == ["N1", "N2"]

    def test_a_bin_can_be_registered(self, client):
        response = client.post(
            "/bins",
            json={"identifier": "S1", "name": "South 1", "capacity": "40000"},
        )
        assert response.status_code == 201
        assert response.json()["capacity"] == "40000.00 bu"

    def test_a_pile_needs_its_kind_to_carry_an_allowance(self, client):
        response = client.post(
            "/bins",
            json={
                "identifier": "P1",
                "name": "Pile",
                "capacity": "40000",
                "pile_allowance": "0.5",
            },
        )
        assert response.status_code == 400

    def test_a_bin_reads_back_as_of_a_moment(self, client):
        client.post("/tickets", json=load())
        after = client.get("/bins/N1?when=2030-01-01T00:00:00").json()
        before = client.get("/bins/N1?when=2020-01-01T00:00:00").json()
        assert after["contents"]["quantity"] != "0.00 bu"
        assert before["contents"]["quantity"] == "0.00 bu"

    def test_a_malformed_when_is_refused(self, client):
        assert client.get("/bins/N1?when=yesterday").status_code == 400

    def test_a_transfer_moves_grain_between_two_bins(self, client):
        client.post("/tickets", json=load())
        response = client.post(
            "/bins/N1/transfer",
            json={"to": "N2", "quantity": "100", "reference": "TR-1"},
        )
        assert response.status_code == 200
        assert response.json()["to"]["contents"]["quantity"] == "100.00 bu"

    def test_a_transfer_of_more_than_is_there(self, client):
        client.post("/tickets", json=load())
        response = client.post(
            "/bins/N1/transfer",
            json={"to": "N2", "quantity": "99999", "reference": "TR-1"},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "insufficient_inventory"

    def test_an_adjustment_is_posted_with_its_reason(self, client):
        client.post("/tickets", json=load())
        response = client.post(
            "/bins/N1/adjust",
            json={"quantity": "-40", "reason": "measure up", "reference": "MU-25"},
        )
        assert response.status_code == 200


class TestBlends:
    def test_a_feasible_blend_comes_back_with_a_draw_list(self, client):
        client.post("/tickets", json=load())
        response = client.post(
            "/blends",
            json={
                "commodity": "corn",
                "quantity": "500",
                "grade": 2,
                "minimum_draw": "0",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["draws"][0]["bin"] == "N1"
        assert payload["quantity"] == "500.00 bu"
        assert payload["spec"]["minimum_grade"] == 2

    def test_an_infeasible_blend_is_a_four_two_two(self, client):
        client.post("/tickets", json=load(moisture="18.5"))
        response = client.post(
            "/blends",
            json={
                "commodity": "corn",
                "quantity": "500",
                "limits": {"moisture": "15.0"},
                "minimum_draw": "0",
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "infeasible_blend"

    def test_a_blend_with_no_limits_at_all(self, client):
        client.post("/tickets", json=load())
        response = client.post("/blends", json={"commodity": "corn", "quantity": "500"})
        assert response.status_code == 400

    def test_an_unknown_factor_lists_the_ones_there_are(self, client):
        client.post("/tickets", json=load())
        response = client.post(
            "/blends",
            json={"commodity": "corn", "quantity": "500", "limits": {"colour": "1"}},
        )
        assert response.status_code == 400
        assert "moisture" in response.json()["detail"]["known"]

    def test_explicit_costs_are_not_offered_over_http(self, client):
        client.post("/tickets", json=load())
        response = client.post(
            "/blends",
            json={
                "commodity": "corn",
                "quantity": "500",
                "grade": 2,
                "objective": "explicit_cost",
            },
        )
        assert response.status_code == 400

    def test_planning_a_blend_writes_nothing(self, client):
        client.post("/tickets", json=load())
        before = client.get("/bins/N1").json()["contents"]["quantity"]
        client.post(
            "/blends",
            json={"commodity": "corn", "quantity": "500", "grade": 2, "minimum_draw": "0"},
        )
        assert client.get("/bins/N1").json()["contents"]["quantity"] == before


class TestReports:
    def test_the_position_is_json_by_default(self, client):
        client.post("/tickets", json=load())
        payload = client.get(f"/reports/position?on={DAY}").json()
        assert payload["positions"][0]["commodity"] == "CORN"

    def test_the_examiner_statement_can_be_csv(self, client):
        client.post("/tickets", json=load())
        response = client.get(
            f"/reports/examiner?on={DAY}", headers={"accept": "text/csv"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert response.text.splitlines()[0].startswith("Crop,")

    def test_the_examiner_statement_can_be_plain_text(self, client):
        client.post("/tickets", json=load())
        response = client.get(
            f"/reports/examiner?on={DAY}", headers={"accept": "text/plain"}
        )
        assert "Warehouse examiner statement" in response.text

    def test_the_day_sheet_carries_all_four_tables(self, client):
        client.post("/tickets", json=load())
        payload = client.get(f"/reports/day?on={DAY}").json()
        assert set(payload["tables"]) == {"tickets", "bins", "obligations", "position"}
        assert json.loads(payload["tables"]["tickets"])["rows"]

    def test_one_table_of_the_day_sheet_can_be_asked_for(self, client):
        client.post("/tickets", json=load())
        payload = client.get(f"/reports/day?on={DAY}&only=bins").json()
        assert payload["title"] == "Bins"

    def test_an_unknown_table_lists_the_ones_there_are(self, client):
        response = client.get(f"/reports/day?on={DAY}&only=nonsense")
        assert response.status_code == 400
        assert "tickets" in response.json()["detail"]["known"]

    def test_a_malformed_date_is_refused(self, client):
        assert client.get("/reports/position?on=14-10-2025").status_code == 400

    def test_the_review_queue_is_empty_on_a_clean_day(self, client):
        client.post("/tickets", json=load())
        payload = client.get("/reports/review").json()
        assert payload["rows"] == []

    def test_a_declared_tare_lands_in_the_review_queue(self, client):
        body = load()
        body["weights"]["tare_source"] = "declared"
        client.post("/tickets", json=body)
        payload = client.get("/reports/review").json()
        assert payload["rows"][0]["number"] == "WBS-25-000001"


class TestSecurity:
    @pytest.fixture
    def keyed(self, database):
        settings = load_settings(
            {
                "BINYARD_DATABASE_URL": f"sqlite:///{database}",
                "BINYARD_API_KEY": "a-shared-secret",
            }
        )
        return create_app(settings)

    def test_the_probes_stay_open(self, keyed):
        client = Client(keyed)
        for path in ("/health", "/ready", "/metrics"):
            assert client.get(path).status_code == 200

    def test_a_request_with_no_key_is_refused(self, keyed):
        response = Client(keyed).get("/bins")
        assert response.status_code == 401
        assert response.json()["code"] == "unauthorised"

    def test_a_wrong_key_is_refused(self, keyed):
        client = Client(keyed, {"x-api-key": "not-it"})
        assert client.get("/bins").status_code == 401

    def test_the_right_key_gets_through(self, keyed):
        client = Client(keyed, {"x-api-key": "a-shared-secret"})
        assert client.get("/bins").status_code == 200

    def test_with_no_key_configured_everything_is_open(self, client):
        assert client.get("/bins").status_code == 200

    def test_a_body_over_the_ceiling_is_refused(self, database):
        settings = load_settings(
            {
                "BINYARD_DATABASE_URL": f"sqlite:///{database}",
                "BINYARD_MAX_REQUEST_BYTES": "2048",
            }
        )
        client = Client(create_app(settings))
        response = client.post("/tickets", json={"padding": "x" * 4096})
        assert response.status_code == 413
        assert response.json()["code"] == "body_too_large"

    def test_a_body_under_the_ceiling_is_read(self, database):
        settings = load_settings(
            {
                "BINYARD_DATABASE_URL": f"sqlite:///{database}",
                "BINYARD_MAX_REQUEST_BYTES": "65536",
            }
        )
        client = Client(create_app(settings))
        assert client.post("/tickets", json={}).status_code == 400

    def test_cors_headers_appear_when_origins_are_configured(self, database):
        settings = load_settings(
            {
                "BINYARD_DATABASE_URL": f"sqlite:///{database}",
                "BINYARD_ALLOWED_ORIGINS": "https://desk.example",
            }
        )
        client = Client(create_app(settings))
        response = client.get("/bins", headers={"origin": "https://desk.example"})
        assert response.headers["access-control-allow-origin"] == "https://desk.example"

    def test_no_cors_headers_without_configuration(self, client):
        response = client.get("/bins", headers={"origin": "https://desk.example"})
        assert "access-control-allow-origin" not in response.headers


def test_the_whole_day_reconciles_over_http(client):
    for _ in range(4):
        assert client.post("/tickets", json=load()).status_code == 201
    position = client.get(f"/reports/position?on={DAY}").json()["positions"][0]
    assert position["on_hand"] == position["open_storage"]
    assert position["house_position"] == "0.00 bu"
    assert position["short"] is False
    assert client.get(f"/tickets?day={DAY}").json()["count"] == 4
