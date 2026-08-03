"""What is installed, against what the build was pinned to."""

from __future__ import annotations

import sys
from unittest import mock

import pytest

from binyard.api import create_app
from binyard.bushels import bushels
from binyard.config import load_settings
from binyard.environment import (
    MINIMUM_PYTHON,
    Installed,
    check_python,
    declared_pins,
    drifted,
    inspect_environment,
)
from binyard.errors import ConfigurationError
from binyard.inventory import Bin, BinKind
from binyard.store.sqlite import SqliteUnitOfWork, connect
from test_api import Client, load


class TestPython:
    def test_this_interpreter_is_new_enough(self):
        check_python()
        assert sys.version_info[:2] >= MINIMUM_PYTHON

    def test_an_older_interpreter_is_refused(self):
        with (
            mock.patch.object(sys, "version_info", (3, 9, 7)),
            pytest.raises(ConfigurationError) as caught,
        ):
            check_python()
        assert caught.value.detail["running"] == "3.9.7"


class TestInstalled:
    def test_a_matching_version_has_not_drifted(self):
        item = Installed(name="starlette", declared="0.41.3", found="0.41.3")
        assert not item.drifted
        assert not item.missing
        assert item.note == "as pinned"

    def test_a_different_version_has_drifted(self):
        item = Installed(name="starlette", declared="0.41.3", found="0.47.0")
        assert item.drifted
        assert "pinned 0.41.3" in item.note

    def test_a_missing_distribution_says_so(self):
        item = Installed(name="gunicorn", declared="23.0.0", found=None)
        assert item.missing
        assert not item.drifted
        assert item.note == "not installed"

    def test_an_unpinned_distribution_never_reads_as_drift(self):
        item = Installed(name="pip", declared=None, found="24.0")
        assert not item.drifted

    def test_it_renders(self):
        described = Installed(name="ruff", declared="0.9.2", found="0.9.2").describe()
        assert described["name"] == "ruff"
        assert described["drifted"] is False


class TestInspecting:
    def test_it_reports_one_row_per_watched_package(self):
        found = inspect_environment(("pytest", "hypothesis"))
        assert [item.name for item in found] == ["pytest", "hypothesis"]

    def test_a_package_nobody_has_is_reported_missing(self):
        found = inspect_environment(("not-a-real-distribution",))
        assert found[0].missing

    def test_pins_come_back_as_a_mapping(self):
        pins = declared_pins()
        assert isinstance(pins, dict)
        assert all(isinstance(value, str) for value in pins.values())

    def test_drifted_only_returns_the_ones_that_differ(self):
        for item in drifted():
            assert item.drifted

    def test_nothing_here_imports_the_package_it_asks_about(self):
        # A check that imported would make a broken dependency fatal, which is
        # the opposite of the point.
        before = set(sys.modules)
        inspect_environment(("gunicorn",))
        assert "gunicorn" not in set(sys.modules) - before


class TestOverHttp:
    @pytest.fixture
    def client(self, tmp_path):
        database = str(tmp_path / "binyard.sqlite3")
        settings = load_settings({"BINYARD_DATABASE_URL": f"sqlite:///{database}"})
        application = create_app(settings)
        work = SqliteUnitOfWork(connect(database, run_migrations=False))
        yard = work.yard.load().with_bin(
            Bin("N1", "North 1", BinKind.UPRIGHT, bushels("200000"))
        )
        work.yard.save(yard)
        work.commit()
        work.close()
        return Client(application)

    def test_readiness_reports_the_interpreter(self, client):
        payload = client.get("/ready").json()
        assert payload["python"].startswith("3.")

    def test_readiness_carries_the_drift_without_failing_on_it(self, client):
        response = client.get("/ready")
        assert response.status_code == 200
        assert isinstance(response.json()["dependency_drift"], list)

    def test_the_house_still_takes_a_load_whatever_the_drift(self, client):
        assert client.post("/tickets", json=load()).status_code == 201
