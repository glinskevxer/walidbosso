from datetime import date, datetime
from decimal import Decimal

import pytest

from binyard.bushels import bushels
from binyard.discounts.posted import default_book
from binyard.errors import Conflict, ValidationError
from binyard.feed import RECORD_WIDTH, read_feed
from binyard.identity import LocationCode, Sequence
from binyard.inventory import Yard
from binyard.percent import percent
from binyard.services import Outcome, import_feed
from binyard.store import MemoryUnitOfWork, SqliteUnitOfWork
from binyard.units import pounds
from conftest import bin_for

LOCATION = LocationCode("WBS")
BOOK = default_book()
BINS = {"CORN": "N1", "SOYBEANS": "S1"}
IMPORTED_AT = datetime(2025, 10, 14, 19, 0)


def record(
    kind="IN",
    sequence=1,
    day="20251014",
    clock="093015",
    account="HALVORSEN FARMS",
    crop="CORN",
    gross="07842000",
    tare="03018000",
    moisture="0150",
    test_weight="0570",
    hauler="TRAVIS",
) -> str:
    return (
        f"{kind:<2}"
        f"{sequence:06d}"
        f"{day}"
        f"{clock}"
        f"{account:<20}"
        f"{crop:<8}"
        f"{gross}"
        f"{tare}"
        f"{moisture:<4}"
        f"{test_weight:<4}"
        f"{hauler:<12}"
    )


@pytest.fixture(params=["memory", "sqlite"])
def work(request):
    yard = Yard(bins=(bin_for("N1"), bin_for("S1", capacity="60000")))
    if request.param == "memory":
        unit = MemoryUnitOfWork(yard)
        unit.commit()
        yield unit
        return
    unit = SqliteUnitOfWork.open()
    unit.yard.save(yard)
    unit.commit()
    try:
        yield unit
    finally:
        unit.close()


def run_import(work, text, source="scale-2025-10-14.dat"):
    return import_feed(
        work,
        read_feed(text),
        Sequence(LOCATION, 2025),
        source=source,
        book=BOOK,
        bin_for=BINS,
        at=IMPORTED_AT,
    )


class TestReading:
    def test_a_record_is_exactly_the_layout_width(self):
        assert len(record()) == RECORD_WIDTH

    def test_a_good_record_parses(self):
        batch = read_feed(record())
        assert batch.accepted == 1
        assert batch.is_clean
        one = batch.records[0]
        assert one.account == "HALVORSEN FARMS"
        assert one.net == pounds("48240")
        assert one.moisture == percent("15.0")
        assert one.test_weight == Decimal("57")
        assert one.weighed_on == date(2025, 10, 14)
        assert one.weighed_at.hour == 9

    def test_blank_lines_are_skipped(self):
        batch = read_feed(f"\n{record()}\n\n")
        assert batch.lines_read == 1

    def test_a_short_line_is_rejected_with_its_number(self):
        batch = read_feed(f"{record()}\nshort")
        assert batch.refused == 1
        assert batch.rejections[0].line_number == 2
        assert "short" in batch.rejections[0].reason

    def test_an_unknown_record_type_is_rejected(self):
        batch = read_feed(record(kind="ZZ"))
        assert batch.refused == 1
        assert "record type" in batch.rejections[0].reason

    def test_a_bad_date_is_rejected(self):
        batch = read_feed(record(day="20251340"))
        assert "not a date" in batch.rejections[0].reason

    def test_a_bad_time_is_rejected(self):
        batch = read_feed(record(clock="257000"))
        assert "not a time" in batch.rejections[0].reason

    def test_a_blank_required_field_is_rejected(self):
        batch = read_feed(record(account=" " * 20))
        assert "blank" in batch.rejections[0].reason

    def test_a_blank_optional_field_is_none_not_zero(self):
        one = read_feed(record(moisture="    ")).records[0]
        assert one.moisture is None
        assert not one.is_gradeable

    def test_a_missing_hauler_is_none(self):
        assert read_feed(record(hauler=" " * 12)).records[0].hauler is None

    def test_a_non_numeric_weight_is_rejected(self):
        batch = read_feed(record(gross="0784A000"))
        assert "not a number" in batch.rejections[0].reason

    def test_the_fingerprint_ignores_position_in_the_file(self):
        first = read_feed(f"{record()}\n{record(sequence=2)}").records[0]
        second = read_feed(f"{record(sequence=2)}\n{record()}").records[1]
        assert first.fingerprint == second.fingerprint

    def test_two_different_weighings_fingerprint_differently(self):
        one = read_feed(record(sequence=1)).records[0]
        two = read_feed(record(sequence=2)).records[0]
        assert one.fingerprint != two.fingerprint

    def test_the_batch_renders_its_rejections(self):
        described = read_feed(f"{record()}\nshort").describe()
        assert described["accepted"] == 1
        assert described["rejections"][0]["line"] == 2


class TestImporting:
    def test_a_good_file_becomes_tickets(self, work):
        text = "\n".join(record(sequence=index) for index in range(1, 4))
        result = run_import(work, text)
        assert result.taken == 3
        assert result.is_clean
        assert len(work.tickets.open_tickets()) == 3
        assert work.yard.load().contents_of("N1").quantity == result.bushels_taken

    def test_the_same_file_twice_changes_nothing(self, work):
        text = "\n".join(record(sequence=index) for index in range(1, 4))
        run_import(work, text)
        held = work.yard.load().contents_of("N1").quantity
        again = run_import(work, text, source="resent.dat")
        assert again.taken == 0
        assert again.duplicates == 3
        assert work.yard.load().contents_of("N1").quantity == held

    def test_a_duplicate_points_at_the_ticket_it_already_made(self, work):
        run_import(work, record())
        again = run_import(work, record(), source="resent.dat")
        assert again.records[0].ticket_number == "WBS-25-000001"
        assert "already imported" in again.records[0].reason

    def test_a_record_with_no_moisture_is_not_graded(self, work):
        result = run_import(work, record(moisture="    "))
        assert result.count(Outcome.NOT_GRADEABLE) == 1
        assert work.tickets.open_tickets() == ()

    def test_an_ungradeable_record_stays_refused_on_a_resend(self, work):
        run_import(work, record(moisture="    "))
        again = run_import(work, record(moisture="    "), source="resent.dat")
        assert again.duplicates == 1

    def test_an_outbound_record_is_left_to_the_loadout_desk(self, work):
        result = run_import(work, record(kind="OU"))
        assert result.count(Outcome.UNSUPPORTED) == 1

    def test_a_crop_the_house_does_not_handle(self, work):
        result = run_import(work, record(crop="RICE"))
        assert result.count(Outcome.REFUSED) == 1
        assert "does not handle" in result.records[0].reason

    def test_a_crop_with_no_nominated_bin(self, work):
        result = import_feed(
            work,
            read_feed(record()),
            Sequence(LOCATION, 2025),
            source="s.dat",
            book=BOOK,
            bin_for={},
            at=IMPORTED_AT,
        )
        assert result.count(Outcome.REFUSED) == 1
        assert "no bin is nominated" in result.records[0].reason

    def test_a_load_the_house_will_not_take_is_refused_not_crashed(self, work):
        result = run_import(work, record(moisture="0270"))
        assert result.count(Outcome.REFUSED) == 1
        assert "too wet" in result.records[0].reason
        assert not result.is_clean

    def test_a_refused_load_does_not_stop_the_rest_of_the_file(self, work):
        text = "\n".join(
            [record(sequence=1), record(sequence=2, moisture="0270"), record(sequence=3)]
        )
        result = run_import(work, text)
        assert result.taken == 2
        assert result.count(Outcome.REFUSED) == 1

    def test_a_bad_line_is_counted_but_does_not_stop_the_import(self, work):
        result = run_import(work, f"{record()}\nnonsense\n{record(sequence=2)}")
        assert result.taken == 2
        assert result.rejected_lines == 1
        assert not result.is_clean

    def test_soybeans_go_to_their_own_bin(self, work):
        result = run_import(work, record(crop="SOYBEANS", moisture="0130"))
        assert result.taken == 1
        assert work.yard.load().contents_of("S1").quantity.is_positive()
        assert work.yard.load().contents_of("N1").is_empty

    def test_an_import_needs_a_source(self, work):
        with pytest.raises(ValidationError):
            import_feed(
                work,
                read_feed(record()),
                Sequence(LOCATION, 2025),
                source="  ",
                book=BOOK,
                bin_for=BINS,
            )

    def test_the_feed_log_lists_what_came_from_a_file(self, work):
        run_import(work, "\n".join(record(sequence=i) for i in (1, 2)))
        entries = work.feed.for_source("scale-2025-10-14.dat")
        assert len(entries) == 2
        assert all(entry.outcome == Outcome.TAKEN for entry in entries)

    def test_the_log_refuses_the_same_fingerprint_twice(self, work):
        run_import(work, record())
        seen = work.feed.for_source("scale-2025-10-14.dat")[0]
        with pytest.raises(Conflict):
            work.feed.add(seen)

    def test_the_result_renders_with_a_count_per_outcome(self, work):
        described = run_import(work, f"{record()}\n{record(sequence=2, kind='OU')}").describe()
        assert described["counts"]["taken"] == 1
        assert described["counts"]["unsupported"] == 1
        assert described["bushels_taken"] != "0.00 bu"


def test_a_whole_day_of_feed_reconciles(work):
    text = "\n".join(
        record(sequence=index, clock=f"{7 + index // 6:02d}{(index * 7) % 60:02d}00")
        for index in range(1, 25)
    )
    result = run_import(work, text)
    assert result.taken == 24
    assert work.yard.load().contents_of("N1").quantity == result.bushels_taken
    assert work.yard.load().total_of("CORN") == result.bushels_taken
    assert sum(1 for _ in work.tickets.open_tickets()) == 24
    assert result.bushels_taken > bushels("20000")
