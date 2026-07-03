"""Reading dates, and the difference between a day and a moment."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from binyard.dates import (
    days_between,
    each_day,
    end_of,
    read_day,
    read_moment,
    start_of,
)
from binyard.errors import ValidationError

DAY = date(2025, 10, 14)


class TestReadingDays:
    def test_an_iso_date_reads(self):
        assert read_day("2025-10-14", "on") == DAY

    def test_whitespace_is_trimmed(self):
        assert read_day("  2025-10-14  ", "on") == DAY

    def test_a_date_passes_straight_through(self):
        assert read_day(DAY, "on") == DAY

    def test_a_datetime_gives_its_date(self):
        assert read_day(datetime(2025, 10, 14, 23, 59), "on") == DAY

    def test_nothing_with_a_default_gives_the_default(self):
        assert read_day(None, "on", DAY) == DAY
        assert read_day("", "on", DAY) == DAY

    def test_nothing_without_a_default_is_required(self):
        with pytest.raises(ValidationError) as caught:
            read_day(None, "on")
        assert caught.value.detail["field"] == "on"

    @pytest.mark.parametrize("bad", ["14-10-2025", "2025/10/14", "yesterday", "2025-13-01"])
    def test_what_is_not_a_date(self, bad):
        with pytest.raises(ValidationError) as caught:
            read_day(bad, "on")
        assert caught.value.detail["field"] == "on"
        assert "YYYY-MM-DD" in caught.value.message


class TestReadingMoments:
    def test_an_iso_timestamp_reads(self):
        assert read_moment("2025-10-14T09:30:15", "when") == datetime(2025, 10, 14, 9, 30, 15)

    def test_a_datetime_passes_straight_through(self):
        moment = datetime(2025, 10, 14, 9, 30)
        assert read_moment(moment, "when") is moment

    def test_a_default_is_used_when_nothing_is_given(self):
        moment = datetime(2025, 10, 14, 9, 30)
        assert read_moment(None, "when", moment) is moment

    def test_nothing_without_a_default_is_required(self):
        with pytest.raises(ValidationError):
            read_moment(None, "when")

    @pytest.mark.parametrize("bad", ["yesterday", "09:30", "2025-10-14T25:00:00"])
    def test_what_is_not_a_moment(self, bad):
        with pytest.raises(ValidationError) as caught:
            read_moment(bad, "when")
        assert caught.value.detail["field"] == "when"


class TestDayBoundaries:
    def test_the_start_is_midnight(self):
        assert start_of(DAY) == datetime(2025, 10, 14, 0, 0, 0)

    def test_the_end_is_the_last_instant_of_the_same_day(self):
        finish = end_of(DAY)
        assert finish.date() == DAY
        assert finish.hour == 23
        assert finish > start_of(DAY)

    def test_the_end_of_one_day_is_before_the_start_of_the_next(self):
        assert end_of(DAY) < start_of(date(2025, 10, 15))

    def test_a_report_as_of_a_day_includes_that_day(self):
        # The bug this replaced: a report built on start_of showed the yard as
        # it stood before anything on its own date had happened.
        morning = datetime(2025, 10, 14, 8, 0)
        assert morning > start_of(DAY)
        assert morning < end_of(DAY)


class TestSpans:
    def test_days_between_counts_forward(self):
        assert days_between(DAY, date(2025, 10, 20)) == 6

    def test_the_same_day_is_no_days(self):
        assert days_between(DAY, DAY) == 0

    def test_backwards_is_never_negative(self):
        assert days_between(date(2025, 10, 20), DAY) == 0

    def test_each_day_is_inclusive_at_both_ends(self):
        walked = list(each_day(DAY, date(2025, 10, 17)))
        assert walked[0] == DAY
        assert walked[-1] == date(2025, 10, 17)
        assert len(walked) == 4

    def test_each_day_over_one_day_gives_that_day(self):
        assert list(each_day(DAY, DAY)) == [DAY]

    def test_each_day_backwards_gives_nothing(self):
        assert list(each_day(date(2025, 10, 20), DAY)) == []
