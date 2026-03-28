from datetime import date, datetime
from decimal import Decimal

import pytest

from binyard import commodity
from binyard.blend import BlendSpec, SpecLimit
from binyard.bushels import bushels
from binyard.contracts import PriceKind, PurchaseContract
from binyard.discounts.posted import default_book
from binyard.errors import (
    BinCapacityExceeded,
    InsufficientInventory,
    LoadoutError,
    TicketError,
    TicketStateError,
    ValidationError,
)
from binyard.grading import FactorName
from binyard.identity import Sequence
from binyard.inventory import Yard
from binyard.loadout import AxleConfiguration, Carrier, CarrierKind, LoadoutOrder
from binyard.money import Price, dollars
from binyard.obligations import ObligationKind, StorageTerms
from binyard.percent import percent
from binyard.services import (
    ReceiptRequest,
    SettlementRequest,
    ShipmentRequest,
    day_sheet,
    examiner_report,
    plan_shipment,
    position_on,
    receive_load,
    review_queue,
    settle_tickets,
    ship_next_load,
)
from binyard.settlement import Checkoff
from binyard.store import MemoryUnitOfWork
from binyard.tickets import TareSource, TicketStatus
from binyard.units import pounds
from conftest import LOCATION, bin_for, sample_for, weights_for

DAY = date(2025, 10, 14)
NOON = datetime(2025, 10, 14, 12, 0)
BOOK = default_book()


@pytest.fixture
def work():
    yard = Yard(bins=(bin_for("N1"), bin_for("N2", capacity="60000")))
    return MemoryUnitOfWork(yard)


@pytest.fixture
def numbers():
    return Sequence(LOCATION, 2025)


def request_for(**overrides) -> ReceiptRequest:
    fields: dict[str, object] = {
        "account": "HALVORSEN FARMS",
        "standard": commodity.CORN,
        "weights": weights_for(),
        "sample": sample_for(),
        "bin_identifier": "N1",
        "weighed_on": DAY,
        "at": NOON,
        "by": "scalehouse",
    }
    fields.update(overrides)
    return ReceiptRequest(**fields)  # type: ignore[arg-type]


def contract_for(identifier="C-1", quantity="5000", **overrides) -> PurchaseContract:
    fields: dict[str, object] = {
        "identifier": identifier,
        "account": "HALVORSEN FARMS",
        "commodity_code": "CORN",
        "quantity": bushels(quantity),
        "delivery_from": date(2025, 10, 1),
        "delivery_to": date(2025, 12, 31),
        "price_kind": PriceKind.FLAT,
        "flat_price": Price.of("4.52"),
    }
    fields.update(overrides)
    return PurchaseContract(**fields)  # type: ignore[arg-type]


class TestReceiving:
    def test_a_load_with_no_contract_goes_to_storage(self, work, numbers):
        receipt = receive_load(work, numbers, request_for(), book=BOOK)
        assert receipt.ticket.status is TicketStatus.APPLIED
        assert receipt.obligation is not None
        assert receipt.obligation.kind is ObligationKind.OPEN_STORAGE
        assert receipt.obligation.quantity == receipt.net_bushels
        assert receipt.contracts_filled == ()

    def test_the_bin_gets_the_net_bushels(self, work, numbers):
        receipt = receive_load(work, numbers, request_for(), book=BOOK)
        assert work.yard.load().contents_of("N1").quantity == receipt.net_bushels

    def test_a_contract_takes_the_load_before_storage_does(self, work, numbers):
        work.contracts.add(contract_for())
        work.commit()
        receipt = receive_load(work, numbers, request_for(), book=BOOK)
        assert receipt.contracts_filled == ("C-1",)
        assert receipt.obligation is None
        assert work.contracts.get("C-1").delivered == receipt.net_bushels

    def test_a_load_bigger_than_the_contract_splits_into_storage(self, work, numbers):
        work.contracts.add(contract_for(quantity="100"))
        work.commit()
        receipt = receive_load(work, numbers, request_for(), book=BOOK)
        assert receipt.contracts_filled == ("C-1",)
        assert receipt.obligation is not None
        assert receipt.obligation.quantity == receipt.net_bushels - bushels("100")

    def test_the_ticket_number_comes_off_the_sequence(self, work, numbers):
        first = receive_load(work, numbers, request_for(), book=BOOK)
        second = receive_load(work, numbers, request_for(), book=BOOK)
        assert first.ticket.number.sequence == 1
        assert second.ticket.number.sequence == 2

    def test_the_sequence_picks_up_where_the_store_left_off(self, work):
        receive_load(work, Sequence(LOCATION, 2025), request_for(), book=BOOK)
        fresh = Sequence(LOCATION, 2025)
        second = receive_load(work, fresh, request_for(), book=BOOK)
        assert second.ticket.number.sequence == 2

    def test_a_load_over_the_receiving_ceiling_is_turned_away(self, work, numbers):
        wet = request_for(sample=sample_for(commodity.CORN, moisture=percent("26.0")))
        with pytest.raises(TicketError) as caught:
            receive_load(work, numbers, wet, book=BOOK)
        assert "too wet" in caught.value.message

    def test_a_load_the_board_rejects_writes_nothing(self, work, numbers):
        # Inside the receiving ceiling but past the board's reject band.
        bad = request_for(
            sample=sample_for(
                commodity.CORN, total_damage=percent("21.0"), heat_damage=percent("0.0")
            )
        )
        with pytest.raises(TicketError):
            receive_load(work, numbers, bad, book=BOOK)
        assert work.tickets.open_tickets() == ()
        assert work.yard.load().contents_of("N1").is_empty

    def test_a_load_into_a_full_bin_writes_nothing(self, work, numbers):
        small = bin_for("SMALL", capacity="100")
        work.yard.save(work.yard.load().with_bin(small))
        work.commit()
        with pytest.raises(BinCapacityExceeded):
            receive_load(work, numbers, request_for(bin_identifier="SMALL"), book=BOOK)
        assert work.tickets.open_tickets() == ()

    def test_the_discounts_come_back_on_the_receipt(self, work, numbers):
        wet = request_for(sample=sample_for(commodity.CORN, moisture=percent("18.0")))
        receipt = receive_load(work, numbers, wet, book=BOOK)
        assert receipt.assessment.line_for("moisture") is not None

    def test_topping_up_an_existing_obligation(self, work, numbers):
        first = receive_load(work, numbers, request_for(), book=BOOK)
        assert first.obligation is not None
        second = receive_load(
            work,
            numbers,
            request_for(obligation_identifier=first.obligation.identifier),
            book=BOOK,
        )
        assert second.obligation is not None
        assert second.obligation.identifier == first.obligation.identifier
        assert second.obligation.quantity == first.net_bushels + second.net_bushels

    def test_topping_up_another_growers_obligation_is_refused(self, work, numbers):
        first = receive_load(work, numbers, request_for(), book=BOOK)
        assert first.obligation is not None
        with pytest.raises(ValidationError):
            receive_load(
                work,
                numbers,
                request_for(
                    account="OTHER FARMS", obligation_identifier=first.obligation.identifier
                ),
                book=BOOK,
            )

    def test_the_receipt_renders_whole(self, work, numbers):
        described = receive_load(work, numbers, request_for(), book=BOOK).describe()
        assert described["ticket"]["status"] == "applied"
        assert described["application"]["to_storage"] != "0.00 bu"


class TestProbedLoads:
    def _probes(self, moistures, position_cycle=None, grams=400):
        from binyard.grading import Position, Probe

        places = position_cycle or [Position.FRONT, Position.MIDDLE, Position.REAR]
        return tuple(
            Probe(
                position=places[index % len(places)],
                grams=grams,
                reading=sample_for(commodity.CORN, moisture=percent(moisture)),
            )
            for index, moisture in enumerate(moistures)
        )

    def test_a_properly_probed_load_needs_no_review(self, work, numbers):
        receipt = receive_load(
            work,
            numbers,
            request_for(sample=None, probes=self._probes(["15.0", "15.2", "15.1"])),
            book=BOOK,
        )
        assert not receipt.ticket.needs_review
        assert receipt.ticket.review_notes == ()
        assert receipt.ticket.sample.moisture == percent("15.1")

    def test_one_probe_flags_the_ticket(self, work, numbers):
        receipt = receive_load(
            work,
            numbers,
            request_for(sample=None, probes=self._probes(["15.0"])),
            book=BOOK,
        )
        assert receipt.ticket.needs_review
        assert any("minimum" in note for note in receipt.ticket.review_notes)

    def test_a_load_wet_at_one_end_is_flagged_but_still_taken(self, work, numbers):
        receipt = receive_load(
            work,
            numbers,
            request_for(sample=None, probes=self._probes(["13.5", "15.5", "19.0"])),
            book=BOOK,
        )
        assert receipt.ticket.status is TicketStatus.APPLIED
        assert receipt.ticket.needs_review
        assert any("varied" in note for note in receipt.ticket.review_notes)

    def test_a_flagged_ticket_reaches_the_review_queue(self, work, numbers):
        receive_load(
            work,
            numbers,
            request_for(sample=None, probes=self._probes(["15.0"])),
            book=BOOK,
        )
        queue = review_queue(work)
        assert len(queue.rows) == 1
        assert "minimum" in queue.rows[0]["reason"]

    def test_a_load_with_neither_a_sample_nor_probes_is_refused(self):
        with pytest.raises(ValidationError):
            request_for(sample=None)

    def test_a_load_with_both_is_refused(self):
        with pytest.raises(ValidationError):
            request_for(probes=self._probes(["15.0"]))

    def test_a_load_with_nowhere_to_go_is_refused(self):
        with pytest.raises(ValidationError):
            request_for(bin_identifier="")

    def test_the_probed_moisture_still_decides_the_ceiling(self, work, numbers):
        with pytest.raises(TicketError):
            receive_load(
                work,
                numbers,
                request_for(sample=None, probes=self._probes(["26.0", "26.5", "27.0"])),
                book=BOOK,
            )


class TestShipping:
    def _stock(self, work, numbers, loads=6):
        for _ in range(loads):
            receive_load(work, numbers, request_for(), book=BOOK)
        return work

    def _order(self, quantity="2000"):
        return LoadoutOrder(
            identifier="LO-1",
            destination="GULF TERMINAL",
            commodity_code="CORN",
            quantity=bushels(quantity),
            ship_from=date(2025, 11, 1),
            ship_to=date(2025, 11, 30),
        )

    def _spec(self, quantity="2000"):
        return BlendSpec(
            commodity_code="CORN",
            quantity=bushels(quantity),
            limits=(SpecLimit(FactorName.MOISTURE, Decimal("15.5")),),
        )

    def _truck(self, identifier="T-14"):
        return Carrier(
            identifier=identifier,
            kind=CarrierKind.TRUCK,
            empty_weight=pounds("31000"),
            axles=AxleConfiguration.TRACTOR_SEMI_5,
        )

    def test_a_plan_comes_back_without_writing(self, work, numbers):
        self._stock(work, numbers)
        before = len(work.yard.load().ledger)
        plan = plan_shipment(
            work,
            ShipmentRequest(
                order=self._order(),
                spec=self._spec(),
                standard=commodity.CORN,
                carriers=(self._truck("A"), self._truck("B"), self._truck("C")),
                at=NOON,
                by="loadout",
            ),
        )
        assert plan.blend.quantity == bushels("2000")
        assert plan.load_count == 3
        assert len(work.yard.load().ledger) == before

    def test_a_spec_for_the_wrong_crop_is_refused(self, work, numbers):
        self._stock(work, numbers)
        with pytest.raises(ValidationError):
            plan_shipment(
                work,
                ShipmentRequest(
                    order=self._order(),
                    spec=BlendSpec(commodity_code="SOYBEANS", quantity=bushels("100")),
                    standard=commodity.SOYBEANS,
                    carriers=(self._truck(),),
                    at=NOON,
                    by="loadout",
                ),
            )

    def test_shipping_a_load_draws_the_bin_and_the_obligation(self, work, numbers):
        self._stock(work, numbers)
        held = work.yard.load().contents_of("N1").quantity
        owed = sum(o.balance.hundredths for o in work.obligations.all_open())
        order, ticket = ship_next_load(
            work,
            numbers,
            order=self._order(),
            standard=commodity.CORN,
            carrier=self._truck(),
            quantity=bushels("800"),
            draws=(("N1", bushels("800")),),
            on=date(2025, 11, 3),
            at=datetime(2025, 11, 3, 9, 0),
            by="loadout",
        )
        assert work.yard.load().contents_of("N1").quantity == held - bushels("800")
        assert sum(o.balance.hundredths for o in work.obligations.all_open()) == owed - 80000
        assert ticket.kind.value == "outbound"
        assert order.shipped == bushels("800")

    def test_a_draw_list_that_does_not_add_up_is_refused(self, work, numbers):
        self._stock(work, numbers)
        with pytest.raises(LoadoutError):
            ship_next_load(
                work,
                numbers,
                order=self._order(),
                standard=commodity.CORN,
                carrier=self._truck(),
                quantity=bushels("800"),
                draws=(("N1", bushels("700")),),
                on=date(2025, 11, 3),
                at=datetime(2025, 11, 3, 9, 0),
                by="loadout",
            )

    def test_a_load_over_the_carrier_limit_is_refused(self, work, numbers):
        self._stock(work, numbers)
        with pytest.raises(LoadoutError):
            ship_next_load(
                work,
                numbers,
                order=self._order(),
                standard=commodity.CORN,
                carrier=self._truck(),
                quantity=bushels("1000"),
                draws=(("N1", bushels("1000")),),
                on=date(2025, 11, 3),
                at=datetime(2025, 11, 3, 9, 0),
                by="loadout",
            )

    def test_shipping_more_than_is_owed_out_is_refused(self, work, numbers):
        first = receive_load(work, numbers, request_for(), book=BOOK)
        receive_load(work, numbers, request_for(), book=BOOK)
        assert first.obligation is not None
        work.obligations.replace(work.obligations.get(first.obligation.identifier).cancelled())
        work.commit()
        with pytest.raises(InsufficientInventory) as caught:
            ship_next_load(
                work,
                numbers,
                order=self._order(),
                standard=commodity.CORN,
                carrier=self._truck(),
                quantity=bushels("875"),
                draws=(("N1", bushels("875")),),
                on=date(2025, 11, 3),
                at=datetime(2025, 11, 3, 9, 0),
                by="loadout",
            )
        assert "open obligation" in caught.value.message

    def test_shipping_more_than_the_bin_holds_is_refused(self, work, numbers):
        receive_load(work, numbers, request_for(), book=BOOK)
        with pytest.raises(InsufficientInventory):
            ship_next_load(
                work,
                numbers,
                order=self._order(),
                standard=commodity.CORN,
                carrier=self._truck(),
                quantity=bushels("870"),
                draws=(("N1", bushels("870")),),
                on=date(2025, 11, 3),
                at=datetime(2025, 11, 3, 9, 0),
                by="loadout",
            )


class TestSettling:
    def test_a_settlement_covers_every_applied_ticket(self, work, numbers):
        first = receive_load(work, numbers, request_for(), book=BOOK)
        second = receive_load(work, numbers, request_for(), book=BOOK)
        settlement = settle_tickets(
            work,
            SettlementRequest(
                number="S-1",
                account="HALVORSEN FARMS",
                commodity_code="CORN",
                on=DAY,
                ticket_numbers=(first.ticket.number, second.ticket.number),
                price=Price.of("4.52"),
                at=NOON,
                by="accounting",
            ),
            book=BOOK,
        )
        assert settlement.quantity == first.net_bushels + second.net_bushels
        assert settlement.gross == Price.of("4.52").extend(settlement.quantity)
        assert work.tickets.get(first.ticket.number).status is TicketStatus.SETTLED

    def test_settling_the_same_ticket_twice_is_refused(self, work, numbers):
        receipt = receive_load(work, numbers, request_for(), book=BOOK)
        base = SettlementRequest(
            number="S-1",
            account="HALVORSEN FARMS",
            commodity_code="CORN",
            on=DAY,
            ticket_numbers=(receipt.ticket.number,),
            price=Price.of("4.52"),
            at=NOON,
            by="accounting",
        )
        settle_tickets(work, base, book=BOOK)
        with pytest.raises(TicketStateError):
            settle_tickets(work, base, book=BOOK)

    def test_a_ticket_on_another_account_is_refused(self, work, numbers):
        receipt = receive_load(work, numbers, request_for(), book=BOOK)
        with pytest.raises(ValidationError):
            settle_tickets(
                work,
                SettlementRequest(
                    number="S-1",
                    account="OTHER FARMS",
                    commodity_code="CORN",
                    on=DAY,
                    ticket_numbers=(receipt.ticket.number,),
                    price=Price.of("4.52"),
                    at=NOON,
                    by="accounting",
                ),
                book=BOOK,
            )

    def test_no_tickets_at_all_is_refused(self, work):
        with pytest.raises(ValidationError):
            settle_tickets(
                work,
                SettlementRequest(
                    number="S-1",
                    account="HALVORSEN FARMS",
                    commodity_code="CORN",
                    on=DAY,
                    ticket_numbers=(),
                    price=Price.of("4.52"),
                    at=NOON,
                    by="accounting",
                ),
                book=BOOK,
            )

    def test_a_wet_load_carries_its_discounts_and_drying_onto_the_statement(
        self, work, numbers
    ):
        receipt = receive_load(
            work,
            numbers,
            request_for(sample=sample_for(commodity.CORN, moisture=percent("18.0"))),
            book=BOOK,
        )
        settlement = settle_tickets(
            work,
            SettlementRequest(
                number="S-1",
                account="HALVORSEN FARMS",
                commodity_code="CORN",
                on=DAY,
                ticket_numbers=(receipt.ticket.number,),
                price=Price.of("4.52"),
                at=NOON,
                by="accounting",
                drying_rate_per_point=Price.from_cents("2"),
                checkoffs=(Checkoff("corn checkoff", per_bushel=Price.from_cents("1")),),
            ),
            book=BOOK,
        )
        kinds = {line.kind.value for line in settlement.lines}
        assert {"grain", "discount", "drying", "checkoff"} <= kinds
        assert settlement.net < settlement.gross

    def test_storage_lands_on_the_statement_when_terms_are_given(self, work, numbers):
        receipt = receive_load(work, numbers, request_for(), book=BOOK)
        assert receipt.obligation is not None
        settlement = settle_tickets(
            work,
            SettlementRequest(
                number="S-1",
                account="HALVORSEN FARMS",
                commodity_code="CORN",
                on=date(2026, 3, 1),
                ticket_numbers=(receipt.ticket.number,),
                price=Price.of("4.52"),
                at=NOON,
                by="accounting",
                obligation_identifiers=(receipt.obligation.identifier,),
                storage_terms=StorageTerms(
                    rate_per_bushel_month=Price.from_cents("4"), free_days=30
                ),
            ),
            book=BOOK,
        )
        assert any(line.kind.value == "storage" for line in settlement.lines)

    def test_advances_and_hauling_come_off(self, work, numbers):
        receipt = receive_load(work, numbers, request_for(), book=BOOK)
        settlement = settle_tickets(
            work,
            SettlementRequest(
                number="S-1",
                account="HALVORSEN FARMS",
                commodity_code="CORN",
                on=DAY,
                ticket_numbers=(receipt.ticket.number,),
                price=Price.of("4.52"),
                at=NOON,
                by="accounting",
                advances=dollars("1000.00"),
                hauling=dollars("120.00"),
            ),
            book=BOOK,
        )
        assert settlement.net == settlement.gross - dollars("1120.00")


class TestReports:
    def test_the_position_matches_what_went_in(self, work, numbers):
        receipt = receive_load(work, numbers, request_for(), book=BOOK)
        report = position_on(work, on=DAY, location=LOCATION)
        corn = report.position_for("CORN")
        assert corn.on_hand == receipt.net_bushels
        assert corn.open_storage == receipt.net_bushels
        assert corn.house_position == bushels("0")

    def test_the_examiner_statement_has_a_row_per_crop(self, work, numbers):
        receive_load(work, numbers, request_for(), book=BOOK)
        table = examiner_report(work, on=DAY, location=LOCATION)
        assert len(table.rows) == 1
        assert table.rows[0]["commodity"] == "CORN"

    def test_the_day_sheet_totals_what_was_received(self, work, numbers):
        first = receive_load(work, numbers, request_for(), book=BOOK)
        second = receive_load(work, numbers, request_for(), book=BOOK)
        sheet = day_sheet(work, on=DAY, location=LOCATION)
        assert sheet.received == first.net_bushels + second.net_bushels
        assert sheet.shipped == bushels("0")
        assert len(sheet.tickets.rows) == 2
        assert len(sheet.tables) == 4

    def test_the_day_sheet_is_empty_on_a_quiet_day(self, work):
        sheet = day_sheet(work, on=DAY, location=LOCATION)
        assert sheet.tickets.is_empty
        assert sheet.received == bushels("0")

    def test_the_review_queue_only_holds_flagged_tickets(self, work, numbers):
        receive_load(work, numbers, request_for(), book=BOOK)
        assert review_queue(work).is_empty
        receive_load(
            work,
            numbers,
            request_for(weights=weights_for(tare_source=TareSource.BOOK)),
            book=BOOK,
        )
        queue = review_queue(work)
        assert len(queue.rows) == 1
        assert "fleet book" in queue.rows[0]["reason"]
