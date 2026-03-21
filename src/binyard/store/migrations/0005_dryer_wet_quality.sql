-- What the grain looked like going into the dryer.
--
-- The run used to go back to the source bin afterwards to find out, which
-- works right up until a run empties the bin, and a run that empties a bin is
-- the normal case rather than the odd one. Carry it on the run.

ALTER TABLE dryer_runs ADD COLUMN wet_quality_json TEXT;
