-- What the scale house feed has already been imported.
--
-- A feed file gets re-sent. The share is on a machine that reboots, somebody
-- copies yesterday's file into today's folder, the indicator resends after a
-- network blip. Every one of those has happened, and the result without this
-- table is a bin holding twice what it should and no way to tell which of the
-- two tickets is the real one.
--
-- The fingerprint is a hash of the record as the indicator wrote it, so a
-- record that arrives twice in different positions in the file is recognised
-- as the same weighing.

CREATE TABLE feed_log (
    fingerprint    TEXT    PRIMARY KEY,
    imported_at    TEXT    NOT NULL,
    source         TEXT    NOT NULL,
    ticket_number  TEXT    REFERENCES tickets (number),
    outcome        TEXT    NOT NULL,
    note           TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX feed_log_by_source ON feed_log (source, imported_at);
CREATE INDEX feed_log_by_ticket ON feed_log (ticket_number);
