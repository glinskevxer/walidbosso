# binyard

Grain accounting for a country elevator: scale tickets in, graded and
discounted, held against storage obligations, blended to a contract spec, and
shipped out again.

## Layout

    src/binyard/    the package
    tests/          the suite, run with pytest

## Getting started

    python -m venv .venv && . .venv/bin/activate
    pip install -e ".[dev]"
    pytest
