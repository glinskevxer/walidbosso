-- Why a ticket wants a second pair of eyes, beyond the tare.
--
-- The weights already knew when they needed review. Now the sample does too:
-- a load probed once, or probed three times in the same place, or reading four
-- points of moisture between one end and the other. All of that used to be
-- computed and thrown away.

ALTER TABLE tickets ADD COLUMN review_notes TEXT NOT NULL DEFAULT '';
