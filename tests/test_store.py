"""The same suite against both backends.

Anything a service is allowed to rely on is tested here through the protocol,
parametrised over the in-memory and the SQLite unit of work. A behaviour that
only one of them has is a behaviour a service must not use.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest

from binyard.bushels import bushels
from binyard.contracts import Delivery, PriceKind, PurchaseContract
from binyard.errors import Conflict, NotFound
from binyard.identity import TicketNumber
from binyard.inventory import Yard
from binyard.money import Price, dollars
from binyard.obligations import ObligationKind, Release, StorageObligation
from binyard.percent import percent
from binyard.settlement import ChargeKind, SettlementBuilder
from binyard.store import MemoryUnitOfWork, SqliteUnitOfWork
from binyard.store.protocol import ContractStore, ObligationStore, TicketStore
from binyard.store.sqlite import Durability, connect, durability
from conftest import LOCATION, bin_for, graded_ticket, sample_for, ticket_for

DAY = date(2025, 10, 14)
MORNING = datetime(2025, 10, 14, 8, 0)


@pytest.fixture(params=["memory", "sqlite"])
def work(request):
    if request.param == "memory":
        unit = MemoryUnitOfWork()
        yield unit
        return
    unit = SqliteUnitOfWork.open()
    try:
        yield unit
    finally:
        unit.close()


def obligation(identifier="OS-1", **overrides) -> StorageObligation:
    fields: dict[str, object] = {
        "identifier": identifier,
        "account": "HALVORSEN FARMS",
        "commodity_code": "CORN",
        "kind": ObligationKind.OPEN_STORAGE,
        "quantity": bushels("10000"),
        "opened_on": DAY,
    }
    fields.update(overrides)
    return StorageObligation(**fields)  # type: ignore[arg-type]


def contract(identifier="C-1", **overrides) -> PurchaseContract:
    fields: dict[str, object] = {
        "identifier": identifier,
        "account": "HALVORSEN FARMS",
        "commodity_code": "CORN",
        "quantity": bushels("5000"),
        "delivery_from": date(2025, 10, 1),
        "delivery_to": date(2025, 12, 31),
        "price_kind": PriceKind.FLAT,
        "flat_price": Price.of("4.52"),
    }
    fields.update(overrides)
    return PurchaseContract(**fields)  # type: ignore[arg-type]


class TestProtocolConformance:
    def test_both_backends_satisfy_the_protocols(self, work):
        assert isinstance(work.tickets, TicketStore)
        assert isinstance(work.obligations, ObligationStore)
        assert isinstance(work.contracts, ContractStore)


class TestTickets:
    def test_a_ticket_round_trips_whole(self, work):
        ticket = graded_ticket()
        work.tickets.add(ticket)
        work.commit()
        assert work.tickets.get(ticket.number) == ticket

    def test_an_ungraded_ticket_round_trips_with_its_nulls(self, work):
        ticket = ticket_for()
        work.tickets.add(ticket)
        fetched = work.tickets.get(ticket.number)
        assert fetched.grade is None
        assert fetched.shrink is None
        assert fetched == ticket

    def test_the_history_survives_the_trip(self, work):
        ticket = graded_ticket().applied(at=MORNING, by="grain desk")
        work.tickets.add(ticket)
        fetched = work.tickets.get(ticket.number)
        assert [event.became for event in fetched.history] == [
            event.became for event in ticket.history
        ]

    def test_a_duplicate_number_is_a_conflict(self, work):
        work.tickets.add(ticket_for())
        with pytest.raises(Conflict):
            work.tickets.add(ticket_for())

    def test_a_missing_ticket_raises_not_found(self, work):
        with pytest.raises(NotFound):
            work.tickets.get(TicketNumber(LOCATION, 2025, 999))

    def test_exists_answers_without_raising(self, work):
        assert not work.tickets.exists(TicketNumber(LOCATION, 2025, 1))
        work.tickets.add(ticket_for())
        assert work.tickets.exists(TicketNumber(LOCATION, 2025, 1))

    def test_replacing_a_ticket_that_is_not_there(self, work):
        with pytest.raises(NotFound):
            work.tickets.replace(ticket_for())

    def test_replacing_writes_the_new_state(self, work):
        work.tickets.add(ticket_for())
        moved = work.tickets.get(TicketNumber(LOCATION, 2025, 1)).voided(
            "wrong account", at=MORNING, by="manager"
        )
        work.tickets.replace(moved)
        assert work.tickets.get(moved.number).void_reason == "wrong account"

    def test_listing_by_account(self, work):
        work.tickets.add(ticket_for(number=TicketNumber(LOCATION, 2025, 1)))
        work.tickets.add(
            ticket_for(number=TicketNumber(LOCATION, 2025, 2), account="OTHER FARMS")
        )
        assert len(work.tickets.for_account("HALVORSEN FARMS")) == 1

    def test_listing_by_day(self, work):
        work.tickets.add(ticket_for(number=TicketNumber(LOCATION, 2025, 1)))
        work.tickets.add(
            ticket_for(number=TicketNumber(LOCATION, 2025, 2), weighed_on=date(2025, 10, 15))
        )
        assert len(work.tickets.weighed_on(DAY)) == 1

    def test_open_tickets_leave_out_the_finished_ones(self, work):
        work.tickets.add(ticket_for(number=TicketNumber(LOCATION, 2025, 1)))
        voided = ticket_for(number=TicketNumber(LOCATION, 2025, 2)).voided(
            "duplicate", at=MORNING, by="manager"
        )
        work.tickets.add(voided)
        assert [t.number.sequence for t in work.tickets.open_tickets()] == [1]

    def test_the_last_issued_number_comes_back(self, work):
        assert work.tickets.last_issued() is None
        for sequence in (1, 7, 3):
            work.tickets.add(ticket_for(number=TicketNumber(LOCATION, 2025, sequence)))
        assert work.tickets.last_issued().sequence == 7


class TestObligations:
    def test_an_obligation_round_trips_with_its_releases(self, work):
        held = obligation().release(Release(date(2025, 11, 1), bushels("4000"), "LO-1"))
        work.obligations.add(held)
        work.commit()
        fetched = work.obligations.get("OS-1")
        assert fetched.balance == bushels("6000")
        assert fetched.releases[0].reference == "LO-1"

    def test_a_priced_contract_keeps_its_price(self, work):
        priced = obligation(identifier="DP-1", kind=ObligationKind.DEFERRED_PRICE).priced(
            Price.of("4.5225"), date(2026, 2, 10)
        )
        work.obligations.add(priced)
        assert work.obligations.get("DP-1").priced_at == Price.of("4.5225")

    def test_a_duplicate_identifier_is_a_conflict(self, work):
        work.obligations.add(obligation())
        with pytest.raises(Conflict):
            work.obligations.add(obligation())

    def test_open_for_filters_by_account_and_crop(self, work):
        work.obligations.add(obligation("OS-1"))
        work.obligations.add(obligation("OS-2", account="OTHER FARMS"))
        work.obligations.add(obligation("OS-3", commodity_code="SOYBEANS"))
        found = work.obligations.open_for("HALVORSEN FARMS", "CORN")
        assert [item.identifier for item in found] == ["OS-1"]

    def test_a_closed_obligation_is_not_open(self, work):
        closed = obligation().release(Release(date(2025, 11, 1), bushels("10000"), "LO-1"))
        work.obligations.add(closed)
        assert work.obligations.all_open() == ()

    def test_as_of_leaves_out_later_ones(self, work):
        work.obligations.add(obligation("OS-1"))
        work.obligations.add(obligation("OS-2", opened_on=date(2026, 1, 1)))
        assert len(work.obligations.as_of(date(2025, 12, 1))) == 1

    def test_replacing_writes_the_releases_too(self, work):
        work.obligations.add(obligation())
        moved = work.obligations.get("OS-1").release(
            Release(date(2025, 11, 1), bushels("1000"), "LO-9")
        )
        work.obligations.replace(moved)
        assert work.obligations.get("OS-1").released == bushels("1000")

    def test_replacing_one_that_is_not_there(self, work):
        with pytest.raises(NotFound):
            work.obligations.replace(obligation())


class TestContracts:
    def test_a_contract_round_trips_with_its_deliveries(self, work):
        held = contract().deliver(Delivery(DAY, "WBS-25-000001", bushels("860.71")))
        work.contracts.add(held)
        work.commit()
        fetched = work.contracts.get("C-1")
        assert fetched.delivered == bushels("860.71")
        assert fetched.deliveries[0].ticket_number == "WBS-25-000001"

    def test_a_basis_contract_keeps_its_legs(self, work):
        held = contract(
            price_kind=PriceKind.BASIS,
            flat_price=None,
            basis=Price.of("-0.20"),
            futures_month="CZ25",
        ).fix_futures(Price.of("4.70"))
        work.contracts.add(held)
        assert work.contracts.get("C-1").price() == Price.of("4.50")

    def test_open_for_respects_the_delivery_window(self, work):
        work.contracts.add(contract("C-1"))
        work.contracts.add(
            contract("C-2", delivery_from=date(2026, 1, 1), delivery_to=date(2026, 3, 1))
        )
        found = work.contracts.open_for("HALVORSEN FARMS", "CORN", DAY)
        assert [item.identifier for item in found] == ["C-1"]

    def test_a_duplicate_identifier_is_a_conflict(self, work):
        work.contracts.add(contract())
        with pytest.raises(Conflict):
            work.contracts.add(contract())

    def test_a_missing_contract_raises(self, work):
        with pytest.raises(NotFound):
            work.contracts.get("C-9")


class TestSettlements:
    def _settlement(self, number="S-1"):
        return (
            SettlementBuilder(number, "HALVORSEN FARMS", "CORN", DAY, bushels("857.12"))
            .grain(Price.of("4.52"))
            .discount("moisture", Price.from_cents("6"))
            .charge(ChargeKind.STORAGE, "storage", dollars("12.00"))
            .build()
        )

    def test_a_settlement_round_trips_line_for_line(self, work):
        settlement = self._settlement()
        work.settlements.add(settlement)
        work.commit()
        fetched = work.settlements.get("S-1")
        assert fetched.net == settlement.net
        assert [line.kind for line in fetched.lines] == [line.kind for line in settlement.lines]

    def test_a_duplicate_number_is_a_conflict(self, work):
        work.settlements.add(self._settlement())
        with pytest.raises(Conflict):
            work.settlements.add(self._settlement())

    def test_listing_by_account(self, work):
        work.settlements.add(self._settlement("S-1"))
        work.settlements.add(self._settlement("S-2"))
        assert len(work.settlements.for_account("HALVORSEN FARMS")) == 2
        assert work.settlements.for_account("OTHER FARMS") == ()


class TestYard:
    def _stocked(self):
        yard = Yard(bins=(bin_for("N1"), bin_for("N2", capacity="60000")))
        return yard.receive(
            "N1",
            "CORN",
            bushels("10000"),
            sample_for(),
            at=MORNING,
            reference="WBS-25-000001",
            by="scalehouse",
        )

    def test_the_yard_round_trips(self, work):
        yard = self._stocked()
        work.yard.save(yard)
        work.commit()
        loaded = work.yard.load()
        assert [b.identifier for b in loaded.bins] == ["N1", "N2"]
        assert loaded.contents_of("N1").quantity == bushels("10000")

    def test_the_quality_survives_the_trip(self, work):
        work.yard.save(self._stocked())
        loaded = work.yard.load()
        assert loaded.contents_of("N1").quality.moisture == percent("15.0")
        assert loaded.contents_of("N1").quality.test_weight == Decimal("57")

    def test_a_backdated_movement_lands_in_the_right_order(self, work):
        yard = self._stocked()
        yard = yard.receive(
            "N1",
            "CORN",
            bushels("5000"),
            sample_for(),
            at=datetime(2025, 10, 13, 8, 0),
            reference="WBS-25-000000",
            by="scalehouse",
        )
        work.yard.save(yard)
        loaded = work.yard.load()
        assert [entry.reference for entry in loaded.ledger] == [
            "WBS-25-000000",
            "WBS-25-000001",
        ]

    def test_saving_twice_does_not_double_the_ledger(self, work):
        yard = self._stocked()
        work.yard.save(yard)
        work.yard.save(yard)
        assert len(work.yard.load().ledger) == 1

    def test_bins_are_updated_not_duplicated(self, work):
        work.yard.save(self._stocked())
        wider = Yard(bins=(bin_for("N1", capacity="200000"), bin_for("N2", capacity="60000")))
        work.yard.save(wider)
        loaded = work.yard.load()
        assert len(loaded.bins) == 2
        assert loaded.bin("N1").capacity == bushels("200000")


class TestTransactions:
    def test_rolling_back_undoes_the_writes(self, work):
        work.tickets.add(ticket_for())
        work.rollback()
        assert not work.tickets.exists(TicketNumber(LOCATION, 2025, 1))

    def test_committing_keeps_them(self, work):
        work.tickets.add(ticket_for())
        work.commit()
        work.rollback()
        assert work.tickets.exists(TicketNumber(LOCATION, 2025, 1))

    def test_leaving_the_context_without_committing_rolls_back(self, work):
        with work:
            work.obligations.add(obligation())
        assert work.obligations.all_open() == ()

    def test_leaving_the_context_after_committing_keeps_it(self, work):
        with work:
            work.obligations.add(obligation())
            work.commit()
        assert len(work.obligations.all_open()) == 1


class TestDurability:
    """WAL on its own leaves synchronous at FULL, which fsyncs on every commit.

    That pairing cost a second and a quarter on a seven migration run and was
    most of the test suite. These pin the settings so it cannot come back.
    """

    def test_a_connection_does_not_fsync_every_commit(self, tmp_path):
        connection = connect(tmp_path / "b.sqlite3")
        try:
            settings = durability(connection)
            assert settings.journal_mode == "wal"
            assert settings.synchronous == "normal"
            assert settings.foreign_keys
        finally:
            connection.close()

    def test_the_settings_that_are_asked_for_are_the_ones_that_stick(self, tmp_path):
        connection = connect(tmp_path / "b.sqlite3")
        try:
            raw = connection.execute("PRAGMA synchronous").fetchone()[0]
            assert raw == 1
        finally:
            connection.close()

    def test_this_pairing_cannot_corrupt(self, tmp_path):
        connection = connect(tmp_path / "b.sqlite3")
        try:
            settings = durability(connection)
            assert not settings.can_corrupt
            assert "cannot" not in settings.note
            assert "lose the last commits" in settings.note
        finally:
            connection.close()

    def test_wal_with_synchronous_off_is_the_one_that_can(self):
        risky = Durability(journal_mode="wal", synchronous="off", foreign_keys=True)
        assert risky.can_corrupt
        assert "unreadable" in risky.note

    def test_full_is_called_out_as_slow_rather_than_unsafe(self):
        careful = Durability(journal_mode="wal", synchronous="full", foreign_keys=True)
        assert not careful.can_corrupt
        assert "waits on the disk" in careful.note

    def test_it_renders_for_an_operator(self, tmp_path):
        connection = connect(tmp_path / "b.sqlite3")
        try:
            described = durability(connection).describe()
            assert described["synchronous"] == "normal"
            assert described["can_corrupt"] is False
        finally:
            connection.close()

    def test_migrating_a_fresh_database_is_not_dominated_by_the_disk(self, tmp_path):
        import time

        connect(tmp_path / "warm.sqlite3").close()  # pay the import cost first
        started = time.perf_counter()
        connect(tmp_path / "fresh.sqlite3").close()
        # It was 1.25s with synchronous at FULL. A second is generous and still
        # an order of magnitude under what the old pairing cost.
        assert time.perf_counter() - started < 1.0


def test_sqlite_enforces_the_foreign_keys():
    unit = SqliteUnitOfWork.open()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            unit._connection.execute(
                "INSERT INTO movements (occurred_at, bin_identifier, kind, commodity_code,"
                " quantity_hundredths, reference, posted_by) VALUES"
                " ('2025-10-14T08:00:00', 'GHOST', 'receipt', 'CORN', 100, 'T', 's')"
            )
    finally:
        unit.close()


def test_sqlite_refuses_a_tare_heavier_than_the_gross():
    unit = SqliteUnitOfWork.open()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            unit._connection.execute(
                "INSERT INTO tickets (number, kind, commodity_code, account, weighed_on,"
                " gross_hundredths, tare_hundredths, tare_source, status)"
                " VALUES ('WBS-25-000001', 'inbound', 'CORN', 'H', '2025-10-14',"
                " 100, 200, 'weighed', 'weighed')"
            )
    finally:
        unit.close()
