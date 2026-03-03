"""The settle and feed commands, driven through the real command line."""

from __future__ import annotations

import pytest

from test_cli import invoke

FEED_RECORD = (
    "IN{sequence:06d}20251014{clock}{account:<20}{crop:<8}"
    "{gross}{tare}{moisture:<4}{test_weight:<4}{hauler:<12}"
)


def record(sequence=1, clock="093015", crop="CORN", moisture="0150", **overrides):
    fields = {
        "sequence": sequence,
        "clock": clock,
        "account": "HALVORSEN FARMS",
        "crop": crop,
        "gross": "07842000",
        "tare": "03018000",
        "moisture": moisture,
        "test_weight": "0570",
        "hauler": "TRAVIS",
    }
    fields.update(overrides)
    return FEED_RECORD.format(**fields)


@pytest.fixture
def database(tmp_path):
    return "sqlite:///" + str(tmp_path / "binyard.sqlite3")


@pytest.fixture
def stocked(database):
    invoke(database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000")
    invoke(
        database,
        "--on",
        "2025-10-14",
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
        "16.5",
        "--test-weight",
        "57",
    )
    return database


def settle(database, verb="preview", *extra):
    return invoke(
        database,
        "--on",
        "2025-10-20",
        "settle",
        verb,
        "S-1",
        "--account",
        "HALVORSEN FARMS",
        "--crop",
        "corn",
        "--price",
        "4.5225",
        "--ticket",
        "WBS-25-000001",
        *extra,
    )


class TestSettle:
    def test_a_preview_prints_the_statement(self, stocked):
        code, out, _ = settle(stocked)
        assert code == 0
        assert "Settlement S-1" in out
        assert "grain" in out
        assert "net" in out

    def test_a_preview_writes_nothing(self, stocked):
        settle(stocked)
        code, out, _ = settle(stocked)
        assert code == 0
        assert "Settlement S-1" in out

    def test_posting_settles_the_ticket(self, stocked):
        code, out, _ = settle(stocked, "post")
        assert code == 0
        assert "written" in out
        code, out, _ = invoke(
            stocked, "--on", "2025-10-14", "report", "day", "--only", "tickets"
        )
        assert "settled" in out

    def test_posting_twice_is_refused(self, stocked):
        settle(stocked, "post")
        code, _, err = settle(stocked, "post")
        assert code == 2
        assert "ticket_state_error" in err

    def test_a_wet_load_carries_a_drying_line(self, stocked):
        code, out, _ = settle(stocked, "preview", "--drying-rate", "3")
        assert code == 0
        assert "drying" in out

    def test_a_checkoff_lands_on_the_statement(self, stocked):
        code, out, _ = settle(stocked, "preview", "--checkoff", "iowa corn=1")
        assert code == 0
        assert "iowa corn" in out

    def test_a_malformed_checkoff_is_refused(self, stocked):
        code, _, err = settle(stocked, "preview", "--checkoff", "iowa corn")
        assert code == 2
        assert "NAME=RATE" in err

    def test_advances_and_hauling_come_off(self, stocked):
        _, plain, _ = settle(stocked)
        _, with_advance, _ = settle(stocked, "preview", "--advance", "1000")
        assert "advances already paid" in with_advance
        assert plain != with_advance

    def test_a_ticket_on_another_account_is_refused(self, stocked):
        code, _, err = invoke(
            stocked,
            "--on",
            "2025-10-20",
            "settle",
            "preview",
            "S-1",
            "--account",
            "OTHER FARMS",
            "--crop",
            "corn",
            "--price",
            "4.52",
            "--ticket",
            "WBS-25-000001",
        )
        assert code == 2
        assert "another account" in err

    def test_a_ticket_that_is_not_there(self, stocked):
        code, _, err = invoke(
            stocked,
            "settle",
            "preview",
            "S-1",
            "--account",
            "HALVORSEN FARMS",
            "--crop",
            "corn",
            "--price",
            "4.52",
            "--ticket",
            "WBS-25-000999",
        )
        assert code == 2
        assert "not_found" in err


class TestFeedCommand:
    @pytest.fixture
    def yard(self, database):
        invoke(database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000")
        return database

    def _file(self, tmp_path, text):
        path = tmp_path / "scale.dat"
        path.write_text(text, encoding="ascii")
        return str(path)

    def test_a_clean_file_imports(self, yard, tmp_path):
        path = self._file(tmp_path, "\n".join(record(sequence=i) for i in (1, 2)))
        code, out, _ = invoke(yard, "--on", "2025-10-14", "feed", path, "--bin", "corn=N1")
        assert code == 0
        assert "2 taken" in out

    def test_the_same_file_twice_takes_nothing(self, yard, tmp_path):
        path = self._file(tmp_path, record())
        invoke(yard, "--on", "2025-10-14", "feed", path, "--bin", "corn=N1")
        code, out, _ = invoke(yard, "--on", "2025-10-14", "feed", path, "--bin", "corn=N1")
        assert code == 0
        assert "0 taken, 1 already seen" in out

    def test_a_dry_run_writes_nothing(self, yard, tmp_path):
        path = self._file(tmp_path, record())
        code, out, _ = invoke(
            yard, "--on", "2025-10-14", "feed", path, "--bin", "corn=N1", "--dry-run"
        )
        assert code == 0
        assert "1 readable" in out
        code, out, _ = invoke(yard, "--on", "2025-10-14", "report", "day", "--only", "tickets")
        assert "(nothing to report)" in out

    def test_an_unreadable_line_exits_non_zero(self, yard, tmp_path):
        path = self._file(tmp_path, f"{record()}\nnonsense")
        code, _, _ = invoke(yard, "--on", "2025-10-14", "feed", path, "--bin", "corn=N1")
        assert code == 2

    def test_a_refused_load_is_reported_on_stderr(self, yard, tmp_path):
        path = self._file(tmp_path, record(moisture="0270"))
        code, _, err = invoke(yard, "--on", "2025-10-14", "feed", path, "--bin", "corn=N1")
        assert code == 2
        assert "too wet" in err

    def test_a_file_that_is_not_there(self, yard):
        code, _, err = invoke(yard, "feed", "/nowhere/scale.dat", "--bin", "corn=N1")
        assert code == 2
        assert "no such feed file" in err

    def test_a_malformed_bin_mapping(self, yard, tmp_path):
        path = self._file(tmp_path, record())
        code, _, err = invoke(yard, "feed", path, "--bin", "corn")
        assert code == 2
        assert "CROP=BIN" in err
