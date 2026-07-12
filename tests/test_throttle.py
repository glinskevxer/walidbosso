"""The write ceiling, on the bucket and through the app."""

from __future__ import annotations

import pytest

from binyard.api import create_app
from binyard.api.throttle import Bucket, TokenBuckets
from binyard.bushels import bushels
from binyard.config import load_settings
from binyard.errors import ConfigurationError
from binyard.inventory import Bin, BinKind
from binyard.store.sqlite import SqliteUnitOfWork, connect
from test_api import Client, load


@pytest.fixture
def database(tmp_path):
    return str(tmp_path / "binyard.sqlite3")


def app_for(database, **overrides):
    environment = {"BINYARD_DATABASE_URL": f"sqlite:///{database}"}
    environment.update(overrides)
    settings = load_settings(environment)
    application = create_app(settings)
    work = SqliteUnitOfWork(connect(database, run_migrations=False))
    yard = work.yard.load().with_bin(Bin("N1", "North 1", BinKind.UPRIGHT, bushels("900000")))
    work.yard.save(yard)
    work.commit()
    work.close()
    return application


class TestTheBucket:
    def test_a_fresh_caller_starts_full(self):
        buckets = TokenBuckets(per_minute=60, burst=5)
        assert buckets.allow("one", now=0.0)[0]

    def test_the_burst_is_the_ceiling(self):
        buckets = TokenBuckets(per_minute=60, burst=3)
        assert [buckets.allow("one", now=0.0)[0] for _ in range(4)] == [
            True,
            True,
            True,
            False,
        ]

    def test_tokens_come_back_as_time_passes(self):
        buckets = TokenBuckets(per_minute=60, burst=2)
        buckets.allow("one", now=0.0)
        buckets.allow("one", now=0.0)
        assert not buckets.allow("one", now=0.0)[0]
        assert buckets.allow("one", now=1.0)[0]

    def test_refilling_never_goes_past_the_burst(self):
        buckets = TokenBuckets(per_minute=60, burst=2)
        buckets.allow("one", now=0.0)
        assert [buckets.allow("one", now=600.0)[0] for _ in range(3)] == [
            True,
            True,
            False,
        ]

    def test_callers_do_not_share_a_bucket(self):
        buckets = TokenBuckets(per_minute=60, burst=1)
        assert buckets.allow("one", now=0.0)[0]
        assert buckets.allow("two", now=0.0)[0]
        assert not buckets.allow("one", now=0.0)[0]

    def test_a_refusal_says_how_long_to_wait(self):
        buckets = TokenBuckets(per_minute=60, burst=1)
        buckets.allow("one", now=0.0)
        allowed, wait = buckets.allow("one", now=0.0)
        assert not allowed
        assert 0.9 < wait <= 1.0

    def test_a_rate_of_nothing_is_refused(self):
        with pytest.raises(ValueError):
            TokenBuckets(per_minute=0)

    def test_idle_callers_are_forgotten_once_there_are_enough_of_them(self):
        buckets = TokenBuckets(per_minute=600, burst=10)
        for index in range(70):
            buckets.allow(f"caller-{index}", now=0.0)
        assert buckets.tracked == 70
        buckets.allow("caller-fresh", now=TokenBuckets.IDLE_SECONDS + 1)
        assert buckets.tracked < 70

    def test_a_bucket_refills_from_where_it_was(self):
        bucket = Bucket(tokens=0.0, last_filled=0.0)
        assert not bucket.take(rate=1.0, burst=5.0, now=0.5)
        assert bucket.take(rate=1.0, burst=5.0, now=1.0)


class TestSettings:
    def test_a_negative_rate_is_refused(self, database):
        with pytest.raises(ConfigurationError):
            load_settings(
                {
                    "BINYARD_DATABASE_URL": f"sqlite:///{database}",
                    "BINYARD_WRITES_PER_MINUTE": "-1",
                }
            )

    def test_the_rate_appears_in_the_settings_dump(self, database):
        settings = load_settings({"BINYARD_DATABASE_URL": f"sqlite:///{database}"})
        assert settings.describe()["writes_per_minute"] == 240


class TestThroughTheApp:
    def test_writes_past_the_ceiling_get_a_four_two_nine(self, database):
        client = Client(app_for(database, BINYARD_WRITES_PER_MINUTE="2"))
        first = client.post("/tickets", json=load())
        second = client.post("/tickets", json=load())
        third = client.post("/tickets", json=load())
        assert first.status_code == 201
        assert second.status_code == 201
        assert third.status_code == 429
        assert third.json()["code"] == "too_many_requests"

    def test_a_refusal_carries_a_retry_after(self, database):
        client = Client(app_for(database, BINYARD_WRITES_PER_MINUTE="1"))
        client.post("/tickets", json=load())
        refused = client.post("/tickets", json=load())
        assert refused.headers["retry-after"]
        assert refused.json()["detail"]["retry_after_seconds"] > 0

    def test_reads_are_never_throttled(self, database):
        client = Client(app_for(database, BINYARD_WRITES_PER_MINUTE="1"))
        client.post("/tickets", json=load())
        client.post("/tickets", json=load())
        for _ in range(10):
            assert client.get("/bins").status_code == 200

    def test_the_probes_stay_open(self, database):
        client = Client(app_for(database, BINYARD_WRITES_PER_MINUTE="1"))
        client.post("/tickets", json=load())
        client.post("/tickets", json=load())
        assert client.get("/health").status_code == 200

    def test_turning_it_off_lets_everything_through(self, database):
        client = Client(app_for(database, BINYARD_WRITES_PER_MINUTE="0"))
        for _ in range(6):
            assert client.post("/tickets", json=load()).status_code == 201

    def test_two_keys_are_two_callers(self, database):
        application = app_for(database, BINYARD_WRITES_PER_MINUTE="1", BINYARD_API_KEY="s3cret")
        one = Client(application, {"x-api-key": "s3cret"})
        assert one.post("/tickets", json=load()).status_code == 201
        assert one.post("/tickets", json=load()).status_code == 429

    def test_a_throttled_write_is_counted(self, database):
        client = Client(app_for(database, BINYARD_WRITES_PER_MINUTE="1"))
        client.post("/tickets", json=load())
        client.post("/tickets", json=load())
        assert "http_throttled_total" in client.get("/metrics").text
