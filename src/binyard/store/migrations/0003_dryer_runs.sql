-- Dryer runs.
--
-- Fuel is stored in thousandths of a gallon because a run burns hundreds of
-- gallons and the interesting figure is gallons per point per bushel, which is
-- a small number that a whole-gallon column would round into uselessness.

CREATE TABLE dryer_runs (
    identifier            TEXT    PRIMARY KEY,
    commodity_code        TEXT    NOT NULL,
    source_bin            TEXT    NOT NULL REFERENCES bins (identifier),
    destination_bin       TEXT    NOT NULL REFERENCES bins (identifier),
    wet_hundredths        INTEGER NOT NULL CHECK (wet_hundredths > 0),
    wet_moisture          INTEGER NOT NULL CHECK (wet_moisture >= 0),
    started_at            TEXT    NOT NULL,
    status                TEXT    NOT NULL,
    dry_hundredths        INTEGER CHECK (dry_hundredths IS NULL OR dry_hundredths >= 0),
    dry_moisture          INTEGER,
    finished_at           TEXT,
    fuel_thousandths      INTEGER CHECK (fuel_thousandths IS NULL OR fuel_thousandths >= 0),
    fuel_price_ticks      INTEGER,
    note                  TEXT    NOT NULL DEFAULT '',
    CHECK (source_bin <> destination_bin),
    CHECK (dry_hundredths IS NULL OR dry_hundredths <= wet_hundredths),
    CHECK (dry_moisture IS NULL OR dry_moisture <= wet_moisture)
);

CREATE INDEX dryer_runs_by_status ON dryer_runs (status, started_at);
CREATE INDEX dryer_runs_by_source ON dryer_runs (source_bin, started_at);
