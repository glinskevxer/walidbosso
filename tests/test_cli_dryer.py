"""The dryer subcommands, through the real command line."""

from __future__ import annotations

import pytest

from test_cli import invoke


@pytest.fixture
def yard(tmp_path):
    database = "sqlite:///" + str(tmp_path / "binyard.sqlite3")
    invoke(database, "bins", "add", "WET", "--name", "Wet bin", "--capacity", "200000")
    invoke(database, "bins", "add", "DRY", "--name", "Dry bin", "--capacity", "200000")
    invoke(
        database,
        "--on",
        "2025-10-14",
        "receive",
        "HALVORSEN FARMS",
        "--crop",
        "corn",
        "--bin",
        "WET",
        "--gross",
        "78420",
        "--tare",
        "30180",
        "--moisture",
        "20.0",
        "--test-weight",
        "56",
    )
    return database


def start(database, quantity="500"):
    return invoke(
        database,
        "--on",
        "2025-10-15",
        "dryer",
        "start",
        "DR-1",
        "--from",
        "WET",
        "--to",
        "DRY",
        "--quantity",
        quantity,
    )


class TestStatus:
    def test_nothing_is_on_the_dryer_to_start_with(self, yard):
        code, out, _ = invoke(yard, "dryer", "status")
        assert code == 0
        assert "(nothing to report)" in out

    def test_a_lit_run_shows_up(self, yard):
        start(yard)
        code, out, _ = invoke(yard, "dryer", "status")
        assert code == 0
        assert "DR-1" in out
        assert "500.00 bu" in out

    def test_a_finished_run_drops_off(self, yard):
        start(yard)
        invoke(
            yard,
            "--on",
            "2025-10-15",
            "dryer",
            "finish",
            "DR-1",
            "--bushels",
            "470",
            "--moisture",
            "15.0",
        )
        code, out, _ = invoke(yard, "dryer", "status")
        assert "(nothing to report)" in out


class TestRunning:
    def test_lighting_takes_the_grain_out_of_the_wet_bin(self, yard):
        code, out, _ = start(yard)
        assert code == 0
        assert "lit with 500.00 bu" in out
        _, bins, _ = invoke(yard, "bins", "show", "WET")
        assert "500.00 bu" not in bins.split("quantity")[1][:40]

    def test_lighting_off_an_empty_bin_is_refused(self, yard):
        code, _, err = invoke(
            yard,
            "dryer",
            "start",
            "DR-9",
            "--from",
            "DRY",
            "--to",
            "WET",
            "--quantity",
            "100",
        )
        assert code == 2
        assert "empty" in err

    def test_finishing_prints_the_variance_without_being_asked(self, yard):
        start(yard)
        code, out, _ = invoke(
            yard,
            "--on",
            "2025-10-15",
            "dryer",
            "finish",
            "DR-1",
            "--bushels",
            "470",
            "--moisture",
            "15.0",
        )
        assert code == 0
        assert "the formula allowed" in out
        assert "difference" in out

    def test_fuel_gives_the_rate_that_decides_drying(self, yard):
        start(yard)
        code, out, _ = invoke(
            yard,
            "--on",
            "2025-10-15",
            "dryer",
            "finish",
            "DR-1",
            "--bushels",
            "470",
            "--moisture",
            "15.0",
            "--fuel",
            "62.5",
            "--fuel-price",
            "1.80",
        )
        assert code == 0
        assert "$112.50" in out
        assert "gal per point bushel" in out

    def test_the_dry_bin_ends_up_with_the_grain(self, yard):
        start(yard)
        invoke(
            yard,
            "--on",
            "2025-10-15",
            "dryer",
            "finish",
            "DR-1",
            "--bushels",
            "470",
            "--moisture",
            "15.0",
        )
        code, out, _ = invoke(yard, "bins", "show", "DRY")
        assert code == 0
        assert "470.00 bu" in out
        assert "15%" in out

    def test_finishing_a_run_that_is_not_there(self, yard):
        code, _, err = invoke(
            yard, "dryer", "finish", "DR-9", "--bushels", "1", "--moisture", "15.0"
        )
        assert code == 2
        assert "not_found" in err

    def test_abandoning_warns_and_keeps_what_came_back(self, yard):
        start(yard)
        code, out, err = invoke(
            yard,
            "--on",
            "2025-10-15",
            "dryer",
            "abandon",
            "DR-1",
            "--reason",
            "burner tripped",
            "--bushels",
            "120",
            "--moisture",
            "18.0",
        )
        assert code == 0
        assert "burner tripped" in err
        assert "120.00 bu came back" in out
