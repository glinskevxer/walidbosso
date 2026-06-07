"""Writing a report to a file, and what happens when that goes wrong."""

from __future__ import annotations

import json
import os
from datetime import date

import pytest

from binyard.errors import ConfigurationError, ValidationError
from binyard.reporting import Column, Format, Table, suggested_name, write_table
from test_cli import invoke

TABLE = Table(
    title="Position at WBS on 2025-11-30",
    columns=(Column("commodity", "Crop"), Column.number("on_hand", "On hand")),
    rows=({"commodity": "CORN", "on_hand": "10000.00 bu"},),
    footers=({"commodity": "total", "on_hand": "10000.00 bu"},),
)


class TestNaming:
    def test_a_name_sorts_and_says_what_it_is(self):
        assert (
            suggested_name("position", date(2025, 11, 30), "wbs", Format.CSV)
            == "position-WBS-2025-11-30.csv"
        )

    def test_spaces_become_hyphens(self):
        assert suggested_name(
            "examiner statement", date(2025, 11, 30), "WBS", Format.JSON
        ).startswith("examiner-statement-")

    def test_every_format_has_its_own_suffix(self):
        suffixes = {fmt.suffix for fmt in Format}
        assert suffixes == {".txt", ".csv", ".json"}

    def test_a_nameless_report_is_refused(self):
        with pytest.raises(ValidationError):
            suggested_name("  ", date(2025, 11, 30), "WBS", Format.CSV)


class TestWriting:
    def test_csv_carries_the_title_and_the_footers(self, tmp_path):
        target = tmp_path / "position.csv"
        written = write_table(TABLE, target, Format.CSV)
        assert written.rows == 1
        assert written.bytes_written > 0
        lines = target.read_text().splitlines()
        assert lines[0] == "Position at WBS on 2025-11-30"
        assert lines[-1].startswith("total")

    def test_json_round_trips(self, tmp_path):
        target = tmp_path / "position.json"
        write_table(TABLE, target, Format.JSON)
        payload = json.loads(target.read_text())
        assert payload["rows"][0]["commodity"] == "CORN"

    def test_text_is_what_the_terminal_would_show(self, tmp_path):
        target = tmp_path / "position.txt"
        write_table(TABLE, target, Format.TEXT)
        assert "Position at WBS" in target.read_text()

    def test_an_existing_file_is_not_replaced_by_accident(self, tmp_path):
        target = tmp_path / "position.csv"
        write_table(TABLE, target, Format.CSV)
        with pytest.raises(ConfigurationError):
            write_table(TABLE, target, Format.CSV)

    def test_overwrite_says_so_explicitly(self, tmp_path):
        target = tmp_path / "position.csv"
        write_table(TABLE, target, Format.CSV)
        written = write_table(TABLE, target, Format.CSV, overwrite=True)
        assert written.path == target

    def test_a_directory_is_not_a_file(self, tmp_path):
        with pytest.raises(ValidationError):
            write_table(TABLE, tmp_path, Format.CSV)

    def test_a_folder_that_is_not_there(self, tmp_path):
        with pytest.raises(ValidationError):
            write_table(TABLE, tmp_path / "nowhere" / "position.csv", Format.CSV)

    def test_nothing_partial_is_left_behind(self, tmp_path):
        target = tmp_path / "position.csv"
        write_table(TABLE, target, Format.CSV)
        leftovers = [name for name in os.listdir(tmp_path) if name.endswith(".part")]
        assert leftovers == []

    def test_an_empty_table_still_writes(self, tmp_path):
        empty = Table(title="Nothing", columns=(Column("a", "A"),))
        written = write_table(empty, tmp_path / "empty.csv", Format.CSV)
        assert written.rows == 0

    def test_the_result_renders(self, tmp_path):
        described = write_table(TABLE, tmp_path / "p.csv", Format.CSV).describe()
        assert described["format"] == "csv"
        assert described["rows"] == 1


class TestThroughTheCommandLine:
    @pytest.fixture
    def stocked(self, tmp_path):
        database = "sqlite:///" + str(tmp_path / "binyard.sqlite3")
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
            "15.0",
            "--test-weight",
            "57",
        )
        return database

    def test_out_writes_the_file_and_says_so(self, stocked, tmp_path):
        target = tmp_path / "position.csv"
        code, out, _ = invoke(
            stocked,
            "--on",
            "2025-10-14",
            "--format",
            "csv",
            "report",
            "--out",
            str(target),
            "position",
        )
        assert code == 0
        assert "wrote 1 rows" in out
        assert "CORN" in target.read_text()

    def test_running_twice_refuses_to_replace(self, stocked, tmp_path):
        target = tmp_path / "position.csv"
        arguments = (
            "--on",
            "2025-10-14",
            "--format",
            "csv",
            "report",
            "--out",
            str(target),
            "position",
        )
        invoke(stocked, *arguments)
        code, _, err = invoke(stocked, *arguments)
        assert code == 2
        assert "already there" in err

    def test_overwrite_replaces_it(self, stocked, tmp_path):
        target = tmp_path / "position.csv"
        base = ("--on", "2025-10-14", "--format", "csv", "report", "--out", str(target))
        invoke(stocked, *base, "position")
        code, _, _ = invoke(stocked, *base, "--overwrite", "position")
        assert code == 0

    def test_the_examiner_statement_writes_too(self, stocked, tmp_path):
        target = tmp_path / "examiner.csv"
        code, _, _ = invoke(
            stocked,
            "--on",
            "2025-10-14",
            "--format",
            "csv",
            "report",
            "--out",
            str(target),
            "examiner",
        )
        assert code == 0
        assert target.read_text().splitlines()[1].startswith("Crop,")

    def test_without_out_it_still_goes_to_the_terminal(self, stocked):
        code, out, _ = invoke(stocked, "--on", "2025-10-14", "report", "position")
        assert code == 0
        assert "CORN" in out
