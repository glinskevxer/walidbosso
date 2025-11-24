import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from binyard.bushels import bushels
from binyard.errors import ValidationError
from binyard.grading.factors import FactorSample
from binyard.inventory import BinKind, Yard
from binyard.obligations import ObligationKind, Release, StorageObligation
from binyard.percent import percent
from binyard.position import build_position
from binyard.reporting import (
    Column,
    Table,
    bin_table,
    examiner_statement,
    obligation_table,
    position_table,
    to_csv,
    to_json,
    to_text,
)
from conftest import bin_for, yard_with

OPENED = date(2025, 10, 15)
TODAY = date(2025, 11, 30)
MORNING = datetime(2025, 10, 15, 8, 0)


def quality(moisture="14.0"):
    return FactorSample(moisture=percent(moisture), test_weight=Decimal("57.0"))


def obligation(identifier, kind, quantity, **overrides):
    fields: dict[str, object] = {
        "identifier": identifier,
        "account": "HALVORSEN FARMS",
        "commodity_code": "CORN",
        "kind": kind,
        "quantity": bushels(quantity),
        "opened_on": OPENED,
    }
    fields.update(overrides)
    return StorageObligation(**fields)  # type: ignore[arg-type]


def stocked_yard(quantity="10000"):
    return yard_with("N1", "N2").receive(
        "N1", "CORN", bushels(quantity), quality(), at=MORNING, reference="T", by="s"
    )


class TestPosition:
    def test_an_empty_yard_with_no_obligations_reports_nothing(self):
        report = build_position(on=TODAY, location="WBS", yard=yard_with("N1"), obligations=[])
        assert report.positions == ()
        assert report.is_clean

    def test_a_location_is_required(self):
        with pytest.raises(ValidationError):
            build_position(on=TODAY, location=" ", yard=yard_with("N1"), obligations=[])

    def test_grain_with_no_obligation_is_all_the_houses(self):
        report = build_position(on=TODAY, location="WBS", yard=stocked_yard(), obligations=[])
        corn = report.position_for("CORN")
        assert corn.on_hand == bushels("10000")
        assert corn.owed_out == bushels("0")
        assert corn.house_position == bushels("10000")
        assert not corn.is_short

    def test_open_storage_is_owed_out(self):
        held = [obligation("OS-1", ObligationKind.OPEN_STORAGE, "8000")]
        report = build_position(on=TODAY, location="WBS", yard=stocked_yard(), obligations=held)
        corn = report.position_for("CORN")
        assert corn.open_storage == bushels("8000")
        assert corn.house_position == bushels("2000")

    def test_owing_more_than_is_there_reads_short(self):
        held = [obligation("OS-1", ObligationKind.OPEN_STORAGE, "12000")]
        report = build_position(on=TODAY, location="WBS", yard=stocked_yard(), obligations=held)
        corn = report.position_for("CORN")
        assert corn.is_short
        assert corn.house_position == bushels("-2000")
        assert not report.is_clean
        assert report.shorts == (corn,)

    def test_deferred_price_grain_belongs_to_the_house(self):
        held = [obligation("DP-1", ObligationKind.DEFERRED_PRICE, "5000")]
        report = build_position(on=TODAY, location="WBS", yard=stocked_yard(), obligations=held)
        corn = report.position_for("CORN")
        assert corn.deferred_price == bushels("5000")
        assert corn.owed_out.is_zero()
        assert corn.unpaid == bushels("5000")

    def test_a_cancelled_obligation_drops_out(self):
        held = [obligation("WR-1", ObligationKind.WAREHOUSE_RECEIPT, "5000").cancelled()]
        report = build_position(on=TODAY, location="WBS", yard=stocked_yard(), obligations=held)
        assert report.position_for("CORN").warehouse_receipts.is_zero()

    def test_an_obligation_opened_later_is_not_counted_yet(self):
        held = [
            obligation("OS-1", ObligationKind.OPEN_STORAGE, "5000", opened_on=date(2026, 1, 1))
        ]
        report = build_position(on=TODAY, location="WBS", yard=stocked_yard(), obligations=held)
        assert report.position_for("CORN").open_storage.is_zero()

    def test_a_release_after_the_report_date_does_not_reduce_the_balance(self):
        held = [
            obligation("OS-1", ObligationKind.OPEN_STORAGE, "8000").release(
                Release(date(2026, 1, 15), bushels("3000"), "LO-1")
            )
        ]
        report = build_position(on=TODAY, location="WBS", yard=stocked_yard(), obligations=held)
        assert report.position_for("CORN").open_storage == bushels("8000")

    def test_a_release_before_it_does(self):
        held = [
            obligation("OS-1", ObligationKind.OPEN_STORAGE, "8000").release(
                Release(date(2025, 11, 1), bushels("3000"), "LO-1")
            )
        ]
        report = build_position(on=TODAY, location="WBS", yard=stocked_yard(), obligations=held)
        assert report.position_for("CORN").open_storage == bushels("5000")

    def test_a_pile_carries_its_allowance_as_its_own_line(self):
        pile = bin_for("PILE", kind=BinKind.PILE, pile_allowance=percent("1.0"))
        yard = Yard(bins=(pile,)).receive(
            "PILE", "CORN", bushels("10000"), quality(), at=MORNING, reference="T", by="s"
        )
        report = build_position(on=TODAY, location="WBS", yard=yard, obligations=[])
        corn = report.position_for("CORN")
        assert corn.pile_allowance == bushels("100")
        assert corn.measurable == bushels("9900")

    def test_the_position_is_read_as_of_the_report_date(self):
        yard = stocked_yard()
        yard = yard.ship(
            "N1", bushels("4000"), at=datetime(2026, 1, 5, 9, 0), reference="LO", by="l"
        )
        report = build_position(on=TODAY, location="WBS", yard=yard, obligations=[])
        assert report.position_for("CORN").on_hand == bushels("10000")

    def test_coverage_reads_as_a_ratio(self):
        held = [obligation("OS-1", ObligationKind.OPEN_STORAGE, "5000")]
        report = build_position(on=TODAY, location="WBS", yard=stocked_yard(), obligations=held)
        assert report.position_for("CORN").coverage() == Decimal("2")

    def test_coverage_is_undefined_with_nothing_owed(self):
        report = build_position(on=TODAY, location="WBS", yard=stocked_yard(), obligations=[])
        assert report.position_for("CORN").coverage() is None

    def test_a_crop_with_obligations_and_no_inventory_still_appears(self):
        held = [obligation("OS-1", ObligationKind.OPEN_STORAGE, "5000", commodity_code="OATS")]
        report = build_position(
            on=TODAY, location="WBS", yard=yard_with("N1"), obligations=held
        )
        oats = report.position_for("OATS")
        assert oats is not None
        assert oats.is_short


class TestTables:
    def test_a_table_needs_columns(self):
        with pytest.raises(ValidationError):
            Table(title="empty", columns=())

    def test_two_columns_cannot_share_a_key(self):
        with pytest.raises(ValidationError):
            Table(
                title="clash",
                columns=(Column("a", "A"), Column("a", "B")),
            )

    def test_a_column_needs_a_key_and_a_heading(self):
        with pytest.raises(ValidationError):
            Column(" ", "A")
        with pytest.raises(ValidationError):
            Column("a", " ")

    def test_an_empty_table_says_so_in_text(self):
        table = Table(title="Nothing", columns=(Column("a", "A"),))
        assert "(nothing to report)" in to_text(table)

    def test_blanks_use_the_columns_placeholder(self):
        table = Table(
            title="Blanks",
            columns=(Column.number("a", "A", blank="-"),),
            rows=({"a": None},),
        )
        assert "-" in to_text(table)

    def test_booleans_print_as_yes_and_no(self):
        table = Table(
            title="Flags", columns=(Column("f", "Flag"),), rows=({"f": True}, {"f": False})
        )
        rendered = to_text(table)
        assert "yes" in rendered and "no" in rendered

    def test_numbers_are_right_aligned(self):
        table = Table(
            title="Money",
            columns=(Column("a", "A"), Column.number("b", "Amount")),
            rows=({"a": "x", "b": "1"}, {"a": "y", "b": "1000"}),
        )
        lines = to_text(table).splitlines()
        assert lines[-2].endswith("   1")
        assert lines[-1].endswith("1000")

    def test_csv_carries_the_footers_as_rows(self):
        table = Table(
            title="T",
            columns=(Column("a", "A"),),
            rows=({"a": "one"},),
            footers=({"a": "total"},),
        )
        assert to_csv(table).splitlines() == ["A", "one", "total"]

    def test_csv_can_carry_the_title(self):
        table = Table(title="T", columns=(Column("a", "A"),))
        assert to_csv(table, include_title=True).splitlines()[0] == "T"

    def test_json_uses_keys_not_headings(self):
        table = Table(title="T", columns=(Column("bin_id", "Bin"),), rows=({"bin_id": "N1"},))
        payload = json.loads(to_json(table))
        assert payload["rows"] == [{"bin_id": "N1"}]
        assert payload["columns"][0]["heading"] == "Bin"

    def test_a_very_wide_table_is_clipped_not_wrapped(self):
        table = Table(
            title="Wide",
            columns=(Column("a", "A"), Column("b", "B")),
            rows=({"a": "x" * 100, "b": "y"},),
        )
        for line in to_text(table, width_limit=40).splitlines():
            assert len(line) <= 40

    def test_rows_can_be_appended(self):
        table = Table(title="T", columns=(Column("a", "A"),), rows=({"a": "1"},))
        assert len(table.with_rows([{"a": "2"}]).rows) == 2


class TestReportBuilders:
    def test_the_position_table_has_a_row_per_crop(self):
        held = [obligation("OS-1", ObligationKind.OPEN_STORAGE, "8000")]
        report = build_position(on=TODAY, location="WBS", yard=stocked_yard(), obligations=held)
        table = position_table(report)
        assert len(table.rows) == 1
        assert table.rows[0]["commodity"] == "CORN"
        assert "WBS" in table.title

    def test_the_examiner_statement_shows_the_two_columns_and_the_difference(self):
        held = [obligation("OS-1", ObligationKind.OPEN_STORAGE, "8000")]
        report = build_position(on=TODAY, location="WBS", yard=stocked_yard(), obligations=held)
        table = examiner_statement(report)
        assert table.keys == (
            "commodity",
            "measurable",
            "owed_out",
            "house_position",
            "coverage",
        )
        assert table.rows[0]["house_position"] == "2000.00 bu"

    def test_the_bin_table_lists_empty_bins_too(self):
        table = bin_table(stocked_yard())
        assert len(table.rows) == 2
        empty = next(row for row in table.rows if row["identifier"] == "N2")
        assert empty["commodity"] is None

    def test_the_obligation_table_hides_closed_ones_by_default(self):
        closed = obligation("OS-1", ObligationKind.OPEN_STORAGE, "1000").release(
            Release(date(2025, 11, 1), bushels("1000"), "LO-1")
        )
        held = [closed, obligation("OS-2", ObligationKind.OPEN_STORAGE, "2000")]
        assert len(obligation_table(held).rows) == 1
        assert len(obligation_table(held, include_closed=True).rows) == 2

    def test_the_obligation_table_runs_oldest_first(self):
        held = [
            obligation(
                "OS-2", ObligationKind.OPEN_STORAGE, "1000", opened_on=date(2025, 11, 1)
            ),
            obligation(
                "OS-1", ObligationKind.OPEN_STORAGE, "1000", opened_on=date(2025, 10, 1)
            ),
        ]
        assert [row["identifier"] for row in obligation_table(held).rows] == ["OS-1", "OS-2"]
