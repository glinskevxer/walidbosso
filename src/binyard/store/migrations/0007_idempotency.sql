-- Replies already sent, keyed by the caller's idempotency key.
--
-- A scale house terminal on a flaky link retries a POST it never saw the
-- answer to. Without this table the retry weighs the same truck twice, and the
-- second ticket is indistinguishable from a real second load. The request hash
-- is stored beside the key so a caller that reuses a key for a different body
-- is told, rather than being handed somebody else's answer.

CREATE TABLE idempotent_replies (
    key           TEXT    PRIMARY KEY,
    route         TEXT    NOT NULL,
    request_hash  TEXT    NOT NULL,
    status        INTEGER NOT NULL,
    body          TEXT    NOT NULL,
    stored_at     TEXT    NOT NULL
);

CREATE INDEX idempotent_replies_by_route ON idempotent_replies (route, stored_at);
