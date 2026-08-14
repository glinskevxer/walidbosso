# binyard

Grain accounting for a country elevator: scale tickets in, graded and
discounted, held against storage obligations, dried, blended to a contract
spec, shipped out, and settled.

    pip install -e ".[api,dev]"
    make seed          # a yard with a harvest in it
    make serve         # the api on 8000
    binyard --help

## What it does

A truck crosses the scale. The load is probed, graded against the published
tables, shrunk to market moisture, discounted off the posted board, put in a
bin whose quality is the weighted average of everything already in it, and
applied to a contract or to storage. Later it is blended with other bins to
make a buyer's spec, drawn out against the oldest obligation first, shipped,
reconciled against the destination's weights, and settled.

Everything above is exact. Weights are hundredths of a pound, bushels are
hundredths of a bushel, money is cents and prices are ten-thousandths of a
dollar, all as integers. There is no floating point anywhere in the package
that decides a number, and a check in CI keeps it that way.

## Layout

    src/binyard/
      units, bushels, money, percent   exact quantities
      commodity                        the crops and their constants
      grading/                         factor tables, grade determination, sampling
      shrink                           water and handling shrink
      discounts/                       the posted board and how it is applied
      tickets/                         scale tickets and their state machine
      inventory/                       bins, commingling, the movement ledger
      drying/                          dryer runs and the variance
      obligations/                     who owns the bushels, and storage charges
      contracts/                       purchase contracts and load application
      blend/                           an exact rational simplex and the planner
      loadout/                         carriers, orders, rail demurrage, reconciliation
      settlement/                      statements
      position/, analysis/             what is owed out, and the market position
      feed/                            the scale house indicator file
      reporting/                       one table type, three renderings
      store/                           protocols, sqlite, migrations
      services/                        the use cases
      api/, cli/                       the two front ends
    tests/                             the suite
    scripts/                           seeding and the checks CI runs

The layers only point downward. Everything above `store/` is pure: no database,
no clock it did not receive, no network.

## Running it

    make check         # lint, types, tests, release check
    make test
    make cover
    make migrate
    binyard --database sqlite:///binyard.sqlite3 report day

The API is a Starlette app built by a factory:

    uvicorn --factory binyard.api:create_app

`GET /` describes every route the build serves. The container runs the same
thing under gunicorn as a non-root user with the database on a volume:

    docker compose up --build

## Configuration

Everything is `BINYARD_`-prefixed and everything has a default that works for a
single location. `BINYARD_API_KEY` turns on key checking for everything except
the health probes. The full set is in `src/binyard/config.py`, and an
unparseable value stops the process at startup rather than becoming a zero.

## Version one

The shapes are settled. The schema moves forward by migration and never
sideways; the exact types keep their units; a ticket written by this release
reads back under the next one. `scripts/check_release.py` refuses a tree whose
version, exports, migrations, schema and console script do not all agree, and
CI refuses a tag that does not match the version in the tree.

## Conventions worth knowing before reading the code

- **Nothing mutates.** Tickets, contracts, obligations and the yard are frozen;
  a transition returns a new one.
- **Nothing is deleted.** A wrong ticket is voided and replaced. A wrong
  movement is reversed. A measure-up difference is posted, not absorbed.
- **A bin's contents are a fold over the ledger**, not a stored total, so they
  cannot drift from the entries that produced them.
- **Test weight runs the other way** from every other factor, and exactly one
  function knows that.
