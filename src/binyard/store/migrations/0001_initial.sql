-- The opening schema.
--
-- Money is stored in cents, prices in ten-thousandths of a dollar, weights in
-- hundredths of a pound, quantities in hundredths of a bushel and percentages
-- in hundredths of a point. All of them are integers, all of them are the same
-- units the Python types use, and there is no floating point column anywhere in
-- this file. That is deliberate: a REAL column is how a bin ends up holding
-- 9999.999999 bushels.
--
-- Quality readings travel as JSON on the row that carries them rather than as
-- their own table. They are read as a unit, written once and never queried by
-- factor, so a nine column join buys nothing.

CREATE TABLE bins (
    identifier          TEXT    PRIMARY KEY,
    name                TEXT    NOT NULL,
    kind                TEXT    NOT NULL,
    capacity_hundredths INTEGER NOT NULL CHECK (capacity_hundredths > 0),
    allowed_commodities TEXT    NOT NULL DEFAULT '',
    aerated             INTEGER NOT NULL DEFAULT 1 CHECK (aerated IN (0, 1)),
    pile_allowance      INTEGER NOT NULL DEFAULT 0 CHECK (pile_allowance >= 0)
);

CREATE TABLE movements (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at         TEXT    NOT NULL,
    bin_identifier      TEXT    NOT NULL REFERENCES bins (identifier),
    kind                TEXT    NOT NULL,
    commodity_code      TEXT    NOT NULL,
    quantity_hundredths INTEGER NOT NULL CHECK (quantity_hundredths <> 0),
    reference           TEXT    NOT NULL,
    posted_by           TEXT    NOT NULL,
    quality_json        TEXT,
    note                TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX movements_by_bin  ON movements (bin_identifier, occurred_at);
CREATE INDEX movements_by_ref  ON movements (reference);

CREATE TABLE tickets (
    number            TEXT    PRIMARY KEY,
    kind              TEXT    NOT NULL,
    commodity_code    TEXT    NOT NULL,
    account           TEXT    NOT NULL,
    weighed_on        TEXT    NOT NULL,
    gross_hundredths  INTEGER NOT NULL CHECK (gross_hundredths > 0),
    tare_hundredths   INTEGER NOT NULL CHECK (tare_hundredths >= 0),
    tare_source       TEXT    NOT NULL,
    gross_at          TEXT,
    tare_at           TEXT,
    status            TEXT    NOT NULL,
    hauler            TEXT,
    sample_json       TEXT,
    grade_json        TEXT,
    shrink_json       TEXT,
    voids             TEXT    REFERENCES tickets (number),
    void_reason       TEXT,
    history_json      TEXT    NOT NULL DEFAULT '[]',
    CHECK (tare_hundredths < gross_hundredths)
);

CREATE INDEX tickets_by_account ON tickets (account, weighed_on);
CREATE INDEX tickets_by_day     ON tickets (weighed_on);
CREATE INDEX tickets_by_status  ON tickets (status);

CREATE TABLE obligations (
    identifier          TEXT    PRIMARY KEY,
    account             TEXT    NOT NULL,
    commodity_code      TEXT    NOT NULL,
    kind                TEXT    NOT NULL,
    quantity_hundredths INTEGER NOT NULL CHECK (quantity_hundredths > 0),
    opened_on           TEXT    NOT NULL,
    free_days           INTEGER NOT NULL DEFAULT 0 CHECK (free_days >= 0),
    storage_rate_ticks  INTEGER NOT NULL DEFAULT 0 CHECK (storage_rate_ticks >= 0),
    priced_at_ticks     INTEGER,
    priced_on           TEXT,
    status              TEXT    NOT NULL,
    superseded_by       TEXT    REFERENCES obligations (identifier)
);

CREATE INDEX obligations_by_account ON obligations (account, commodity_code, status);

CREATE TABLE obligation_releases (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    obligation          TEXT    NOT NULL REFERENCES obligations (identifier),
    released_on         TEXT    NOT NULL,
    quantity_hundredths INTEGER NOT NULL CHECK (quantity_hundredths > 0),
    reference           TEXT    NOT NULL,
    note                TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX releases_by_obligation ON obligation_releases (obligation);

CREATE TABLE contracts (
    identifier           TEXT    PRIMARY KEY,
    account              TEXT    NOT NULL,
    commodity_code       TEXT    NOT NULL,
    quantity_hundredths  INTEGER NOT NULL CHECK (quantity_hundredths > 0),
    delivery_from        TEXT    NOT NULL,
    delivery_to          TEXT    NOT NULL,
    price_kind           TEXT    NOT NULL,
    flat_price_ticks     INTEGER,
    basis_ticks          INTEGER,
    futures_month        TEXT,
    futures_price_ticks  INTEGER,
    tolerance_hundredths INTEGER NOT NULL DEFAULT 500,
    status               TEXT    NOT NULL,
    written_on           TEXT,
    CHECK (delivery_to >= delivery_from)
);

CREATE INDEX contracts_by_account ON contracts (account, commodity_code, status);

CREATE TABLE contract_deliveries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    contract            TEXT    NOT NULL REFERENCES contracts (identifier),
    delivered_on        TEXT    NOT NULL,
    ticket_number       TEXT    NOT NULL,
    quantity_hundredths INTEGER NOT NULL CHECK (quantity_hundredths > 0)
);

CREATE INDEX deliveries_by_contract ON contract_deliveries (contract);

CREATE TABLE settlements (
    number              TEXT    PRIMARY KEY,
    account             TEXT    NOT NULL,
    commodity_code      TEXT    NOT NULL,
    settled_on          TEXT    NOT NULL,
    quantity_hundredths INTEGER NOT NULL,
    net_cents           INTEGER NOT NULL
);

CREATE INDEX settlements_by_account ON settlements (account, settled_on);

CREATE TABLE settlement_lines (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement          TEXT    NOT NULL REFERENCES settlements (number),
    position            INTEGER NOT NULL,
    kind                TEXT    NOT NULL,
    description         TEXT    NOT NULL,
    quantity_hundredths INTEGER,
    rate_ticks          INTEGER,
    amount_cents        INTEGER NOT NULL,
    UNIQUE (settlement, position)
);
