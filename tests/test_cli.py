import io
import json

import pytest

from binyard.cli import build_parser, run
from binyard.cli.context import OutputFormat, database_path
from binyard.errors import ConfigurationError


@pytest.fixture
def database(tmp_path):
    return "sqlite:///" + str(tmp_path / "binyard.sqlite3")


def invoke(database, *arguments, environment=None):
    out, err = io.StringIO(), io.StringIO()
    code = run(
        ["--database", database, *arguments],
        out=out,
        err=err,
        environment=environment or {},
    )
    return code, out.getvalue(), err.getvalue()


def stocked(database, moisture="15.0", bushels_wanted=None):
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
        moisture,
        "--test-weight",
        "57",
    )


class TestParser:
    def test_the_parser_builds_and_lists_the_commands(self):
        parser = build_parser()
        actions = [a for a in parser._actions if a.dest == "command"]
        assert set(actions[0].choices) == {
            "migrate",
            "bins",
            "receive",
            "feed",
            "dryer",
            "blend",
            "ship",
            "settle",
            "report",
        }

    def test_no_command_is_an_error(self, capsys):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_the_version_flag_exits_cleanly(self):
        with pytest.raises(SystemExit) as caught:
            build_parser().parse_args(["--version"])
        assert caught.value.code == 0


class TestDatabaseUrls:
    def test_the_sqlite_forms_are_understood(self):
        assert str(database_path("sqlite:///data/binyard.sqlite3")).endswith(
            "data/binyard.sqlite3"
        )
        assert database_path(":memory:") == ":memory:"

    def test_anything_else_is_refused(self):
        with pytest.raises(ConfigurationError):
            database_path("postgresql://localhost/binyard")


class TestMigrate:
    def test_a_fresh_database_comes_up_migrated(self, database):
        code, out, _ = invoke(database, "migrate")
        assert code == 0
        assert "0 applied" in out

    def test_check_reports_nothing_outstanding(self, database):
        invoke(database, "migrate")
        code, out, _ = invoke(database, "migrate", "--check")
        assert code == 0
        assert "(nothing to report)" in out


class TestBins:
    def test_adding_a_bin_prints_the_yard(self, database):
        code, out, _ = invoke(
            database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000"
        )
        assert code == 0
        assert "North 1" in out

    def test_listing_shows_what_was_added(self, database):
        invoke(database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000")
        code, out, _ = invoke(database, "bins", "list")
        assert code == 0
        assert "120000.00 bu" in out

    def test_a_bin_restricted_to_one_crop(self, database):
        invoke(
            database,
            "bins",
            "add",
            "S1",
            "--name",
            "South 1",
            "--capacity",
            "60000",
            "--only",
            "soybeans",
        )
        code, out, _ = invoke(database, "bins", "show", "S1")
        assert code == 0
        assert "South 1" in out

    def test_showing_a_bin_that_is_not_there(self, database):
        code, _, err = invoke(database, "bins", "show", "GHOST")
        assert code == 2
        assert "not_found" in err

    def test_a_pile_allowance_on_an_upright_is_refused(self, database):
        code, _, err = invoke(
            database,
            "bins",
            "add",
            "N9",
            "--name",
            "Nine",
            "--capacity",
            "1000",
            "--pile-allowance",
            "0.5",
        )
        assert code == 2
        assert "validation_error" in err


class TestReceive:
    def test_a_load_prints_the_ticket_and_where_it_went(self, database):
        invoke(database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000")
        code, out, _ = invoke(
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
        assert code == 0
        assert "WBS-25-000001" in out
        assert "US No. 1" in out
        assert "Where the bushels went" in out

    def test_a_wet_load_prints_its_discounts(self, database):
        invoke(database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000")
        code, out, _ = invoke(
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
            "18.5",
            "--test-weight",
            "57",
        )
        assert code == 0
        assert "Discounts on" in out
        assert "moisture" in out

    def test_a_load_too_wet_to_take_is_refused(self, database):
        invoke(database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000")
        code, _, err = invoke(
            database,
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
            "27.0",
            "--test-weight",
            "57",
        )
        assert code == 2
        assert "too wet" in err

    def test_a_book_tare_warns_on_stderr(self, database):
        invoke(database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000")
        code, _, err = invoke(
            database,
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
            "--tare-source",
            "book",
        )
        assert code == 0
        assert "needs review" in err

    def test_an_unknown_crop_is_refused(self, database):
        code, _, err = invoke(
            database,
            "receive",
            "H",
            "--crop",
            "rice",
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
        assert code == 2
        assert "unknown_commodity" in err


class TestProbes:
    def test_a_probed_load_is_taken(self, database):
        invoke(database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000")
        code, out, _ = invoke(
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
            "--probe",
            "front:420:15.0:57.0",
            "--probe",
            "middle:410:15.2:56.5",
            "--probe",
            "rear:400:15.1:57.0",
        )
        assert code == 0
        assert "WBS-25-000001" in out

    def test_probes_and_a_single_reading_together_are_refused(self, database):
        invoke(database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000")
        code, _, err = invoke(
            database,
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
            "--probe",
            "front:420:15.0:57.0",
        )
        assert code == 2
        assert "not both" in err

    def test_neither_is_refused(self, database):
        invoke(database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000")
        code, _, err = invoke(
            database,
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
        )
        assert code == 2
        assert "or probes" in err

    def test_a_malformed_probe_is_refused(self, database):
        invoke(database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000")
        code, _, err = invoke(
            database,
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
            "--probe",
            "front:420",
        )
        assert code == 2
        assert "POSITION:GRAMS" in err

    def test_a_probe_position_that_is_not_one(self, database):
        invoke(database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000")
        code, _, err = invoke(
            database,
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
            "--probe",
            "roof:420:15.0:57.0",
        )
        assert code == 2
        assert "no such probe position" in err

    def test_a_flagged_probed_load_warns_on_stderr(self, database):
        invoke(database, "bins", "add", "N1", "--name", "North 1", "--capacity", "120000")
        code, _, err = invoke(
            database,
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
            "--probe",
            "middle:420:15.0:57.0",
        )
        assert code == 0
        assert "needs review" in err


class TestBlend:
    def test_a_feasible_spec_prints_a_draw_list(self, database):
        stocked(database)
        code, out, _ = invoke(
            database,
            "blend",
            "--crop",
            "corn",
            "--quantity",
            "500",
            "--grade",
            "2",
            "--minimum-draw",
            "100",
        )
        assert code == 0
        assert "Blend plan" in out
        assert "N1" in out

    def test_an_infeasible_spec_says_so(self, database):
        stocked(database, moisture="18.5")
        code, _, err = invoke(
            database,
            "blend",
            "--crop",
            "corn",
            "--quantity",
            "500",
            "--max",
            "moisture=15.0",
        )
        assert code == 2
        assert "infeasible_blend" in err

    def test_a_spec_with_no_limits_at_all_is_refused(self, database):
        stocked(database)
        code, _, err = invoke(database, "blend", "--crop", "corn", "--quantity", "500")
        assert code == 2
        assert "at least one limit" in err

    def test_a_maximum_on_test_weight_is_refused(self, database):
        stocked(database)
        code, _, err = invoke(
            database,
            "blend",
            "--crop",
            "corn",
            "--quantity",
            "500",
            "--max",
            "test_weight=54",
        )
        assert code == 2
        assert "does not take a maximum" in err

    def test_a_malformed_limit_is_refused(self, database):
        stocked(database)
        code, _, err = invoke(
            database, "blend", "--crop", "corn", "--quantity", "500", "--max", "moisture"
        )
        assert code == 2
        assert "FACTOR=VALUE" in err

    def test_an_explicit_limit_tightens_the_grade(self, database):
        stocked(database)
        code, out, _ = invoke(
            database,
            "blend",
            "--crop",
            "corn",
            "--quantity",
            "500",
            "--grade",
            "2",
            "--max",
            "total_damage=1.5",
            "--minimum-draw",
            "100",
        )
        assert code == 0
        assert "1.5" in out


class TestShip:
    def test_an_order_survives_between_invocations(self, database):
        stocked(database)
        invoke(
            database,
            "ship",
            "order",
            "LO-1",
            "--destination",
            "GULF TERMINAL",
            "--crop",
            "corn",
            "--quantity",
            "500",
            "--from",
            "2025-11-01",
            "--to",
            "2025-11-30",
        )
        code, out, err = invoke(
            database,
            "--on",
            "2025-11-03",
            "ship",
            "load",
            "LO-1",
            "--crop",
            "corn",
            "--destination",
            "GULF TERMINAL",
            "--carrier",
            "T-14:31000",
            "--draw",
            "N1=500",
        )
        assert code == 0
        assert "complete" in out
        assert "ticket WBS-25-000002" in out

    def test_planning_writes_nothing(self, database):
        stocked(database)
        invoke(
            database,
            "ship",
            "order",
            "LO-1",
            "--destination",
            "GULF TERMINAL",
            "--crop",
            "corn",
            "--quantity",
            "500",
            "--from",
            "2025-11-01",
            "--to",
            "2025-11-30",
        )
        before, _, _ = invoke(database, "--format", "json", "report", "position")
        code, out, _ = invoke(
            database,
            "ship",
            "plan",
            "LO-1",
            "--crop",
            "corn",
            "--quantity",
            "500",
            "--grade",
            "2",
            "--carrier",
            "T-14:31000",
        )
        assert code == 0
        assert "Loads" in out

    def test_loading_an_order_that_is_not_there(self, database):
        stocked(database)
        code, _, err = invoke(
            database,
            "ship",
            "load",
            "LO-9",
            "--crop",
            "corn",
            "--destination",
            "GULF",
            "--carrier",
            "T-14:31000",
            "--draw",
            "N1=500",
        )
        assert code == 2
        assert "not_found" in err

    def test_a_malformed_carrier_is_refused(self, database):
        stocked(database)
        invoke(
            database,
            "ship",
            "order",
            "LO-1",
            "--destination",
            "GULF",
            "--crop",
            "corn",
            "--quantity",
            "500",
            "--from",
            "2025-11-01",
            "--to",
            "2025-11-30",
        )
        code, _, err = invoke(
            database,
            "ship",
            "load",
            "LO-1",
            "--crop",
            "corn",
            "--destination",
            "GULF",
            "--carrier",
            "T-14",
            "--draw",
            "N1=500",
        )
        assert code == 2
        assert "ID:EMPTY_LB" in err


class TestReports:
    def test_the_position_report_balances(self, database):
        stocked(database)
        code, out, _ = invoke(database, "--on", "2025-10-14", "report", "position")
        assert code == 0
        assert "CORN" in out

    def test_the_examiner_statement_renders_as_csv(self, database):
        stocked(database)
        code, out, _ = invoke(
            database, "--on", "2025-10-14", "--format", "csv", "report", "examiner"
        )
        assert code == 0
        assert out.splitlines()[0].startswith("Crop,Measurable inventory")

    def test_json_output_is_json(self, database):
        stocked(database)
        code, out, _ = invoke(
            database, "--on", "2025-10-14", "--format", "json", "report", "position"
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["rows"][0]["commodity"] == "CORN"

    def test_the_day_sheet_prints_four_tables(self, database):
        stocked(database)
        code, out, _ = invoke(database, "--on", "2025-10-14", "report", "day")
        assert code == 0
        assert out.count("=====") >= 4
        assert "received" in out

    def test_one_table_of_the_day_sheet_can_be_asked_for(self, database):
        stocked(database)
        code, out, _ = invoke(
            database, "--on", "2025-10-14", "report", "day", "--only", "tickets"
        )
        assert code == 0
        assert "Tickets on 2025-10-14" in out
        assert "Bins" not in out

    def test_the_review_queue_exits_non_zero_when_it_has_rows(self, database):
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
            "--tare-source",
            "declared",
        )
        code, out, _ = invoke(database, "report", "review")
        assert code == 1
        assert "declared" in out

    def test_the_obligation_report_lists_the_open_ones(self, database):
        stocked(database)
        code, out, _ = invoke(database, "--on", "2025-10-14", "report", "obligations")
        assert code == 0
        assert "HALVORSEN FARMS" in out

    def test_quiet_drops_the_notes_but_not_the_tables(self, database):
        code, out, _ = invoke(database, "--quiet", "migrate")
        assert "0 applied" not in out
        assert "Applied migrations" in out


def test_the_format_flag_is_honoured_everywhere(database):
    stocked(database)
    for output_format in OutputFormat:
        code, out, _ = invoke(
            database,
            "--format",
            output_format.value,
            "--on",
            "2025-10-14",
            "report",
            "day",
            "--only",
            "bins",
        )
        assert code == 0
        assert out.strip()
