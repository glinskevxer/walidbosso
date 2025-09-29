-- Load-out orders and the loads that go out against them.
--
-- Held in a module level dict until now, which meant an order written in one
-- shell invocation was gone by the next one. Same units as everywhere else:
-- hundredths of a bushel, integers only.

CREATE TABLE loadout_orders (
    identifier           TEXT    PRIMARY KEY,
    destination          TEXT    NOT NULL,
    commodity_code       TEXT    NOT NULL,
    quantity_hundredths  INTEGER NOT NULL CHECK (quantity_hundredths > 0),
    ship_from            TEXT    NOT NULL,
    ship_to              TEXT    NOT NULL,
    status               TEXT    NOT NULL,
    overship_tolerance   INTEGER NOT NULL DEFAULT 0 CHECK (overship_tolerance >= 0),
    CHECK (ship_to >= ship_from)
);

CREATE INDEX loadout_orders_by_status ON loadout_orders (status, ship_from);

CREATE TABLE loadout_loads (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    order_identifier     TEXT    NOT NULL REFERENCES loadout_orders (identifier),
    sequence             INTEGER NOT NULL CHECK (sequence >= 1),
    carrier_identifier   TEXT    NOT NULL,
    quantity_hundredths  INTEGER NOT NULL CHECK (quantity_hundredths > 0),
    shipped_on           TEXT,
    ticket_number        TEXT,
    UNIQUE (order_identifier, sequence)
);

CREATE INDEX loadout_loads_by_order ON loadout_loads (order_identifier, sequence);
