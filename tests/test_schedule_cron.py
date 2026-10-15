"""The in-repo 5-field cron (epic #142 W2, S2): tables, no clock, no dependency.

Every expectation is a naive local wall-clock datetime constructed directly,
so the tables are the same on every machine and in every time zone.
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from aish.schedule import (
    CRON_HORIZON,
    InvalidTask,
    NoOccurrence,
    cron_next,
    cron_prev,
    parse_cron,
)

D = datetime

# expr, after, the first occurrence strictly after it
NEXT = [
    # every minute, and strictly-after
    ("* * * * *", D(2026, 10, 15, 9, 0), D(2026, 10, 15, 9, 1)),
    ("* * * * *", D(2026, 10, 15, 9, 0, 30), D(2026, 10, 15, 9, 1)),
    # */n
    ("*/15 * * * *", D(2026, 10, 15, 9, 1), D(2026, 10, 15, 9, 15)),
    ("*/15 * * * *", D(2026, 10, 15, 9, 45), D(2026, 10, 15, 10, 0)),
    ("0 */6 * * *", D(2026, 10, 15, 13, 0), D(2026, 10, 15, 18, 0)),
    # explicit lists, ranges, and the two combined
    ("5,35 * * * *", D(2026, 10, 15, 9, 5), D(2026, 10, 15, 9, 35)),
    ("0 9-11 * * *", D(2026, 10, 15, 11, 0), D(2026, 10, 16, 9, 0)),
    ("0 1-5,7 * * *", D(2026, 10, 15, 5, 0), D(2026, 10, 15, 7, 0)),
    ("0-30/10 8 * * *", D(2026, 10, 15, 8, 20), D(2026, 10, 15, 8, 30)),
    # the common shapes, where the dom/dow OR rule never engages
    ("0 7 * * *", D(2026, 10, 15, 7, 0), D(2026, 10, 16, 7, 0)),  # each morning
    ("0 * * * *", D(2026, 10, 15, 9, 59), D(2026, 10, 15, 10, 0)),  # hourly
    ("0 9 * * 1-5", D(2026, 10, 16, 9, 0), D(2026, 10, 19, 9, 0)),  # Fri -> Mon
    ("0 9 * * 1-5", D(2026, 10, 15, 8, 0), D(2026, 10, 15, 9, 0)),  # Thu
    # dow 7 is Sunday, as 0 is
    ("0 12 * * 7", D(2026, 10, 15, 0, 0), D(2026, 10, 18, 12, 0)),
    ("0 12 * * 0", D(2026, 10, 15, 0, 0), D(2026, 10, 18, 12, 0)),
    # month ends, February, year rollover
    ("0 0 31 * *", D(2026, 10, 31, 0, 0), D(2026, 12, 31, 0, 0)),  # Nov has 30
    ("0 0 29 2 *", D(2026, 1, 1), D(2028, 2, 29, 0, 0)),  # next leap day
    ("0 0 1 * *", D(2026, 12, 31, 23, 59), D(2027, 1, 1, 0, 0)),
    ("59 23 31 12 *", D(2026, 12, 31, 23, 59), D(2027, 12, 31, 23, 59)),
    ("0 0 * 2 *", D(2026, 2, 28, 0, 0), D(2027, 2, 1, 0, 0)),
    # dom AND dow both restricted: vixie ORs them
    ("0 9 13 * 5", D(2026, 10, 15, 0, 0), D(2026, 10, 16, 9, 0)),  # the Friday
    ("0 9 15 * 1", D(2026, 10, 14, 0, 0), D(2026, 10, 15, 9, 0)),  # the 15th, a Thu
    # one of them starred: they AND, and `*/2` counts as starred (vixie)
    ("0 9 */2 * 1", D(2026, 10, 15, 0, 0), D(2026, 10, 19, 9, 0)),  # Mon the 19th, odd
]


@pytest.mark.parametrize("expr,after,expected", NEXT)
def test_cron_next(expr, after, expected):
    assert cron_next(expr, after) == expected


# expr, at_or_before, the latest occurrence at or before it
PREV = [
    ("* * * * *", D(2026, 10, 15, 9, 0, 30), D(2026, 10, 15, 9, 0)),
    ("0 9 * * *", D(2026, 10, 15, 9, 0), D(2026, 10, 15, 9, 0)),  # inclusive
    ("0 9 * * *", D(2026, 10, 15, 8, 59), D(2026, 10, 14, 9, 0)),
    ("*/15 * * * *", D(2026, 10, 15, 9, 14), D(2026, 10, 15, 9, 0)),
    ("0 9 * * 1-5", D(2026, 10, 19, 8, 0), D(2026, 10, 16, 9, 0)),  # Mon -> Fri
    ("0 0 31 * *", D(2026, 12, 1), D(2026, 10, 31, 0, 0)),
    ("0 0 1 1 *", D(2026, 12, 31, 23, 59), D(2026, 1, 1, 0, 0)),
    ("0 0 29 2 *", D(2027, 6, 1), D(2024, 2, 29, 0, 0)),
    ("0 12 * * 7", D(2026, 10, 17, 0, 0), D(2026, 10, 11, 12, 0)),
    ("0 9 13 * 5", D(2026, 10, 15, 0, 0), D(2026, 10, 13, 9, 0)),  # OR rule, the 13th
]


@pytest.mark.parametrize("expr,before,expected", PREV)
def test_cron_prev(expr, before, expected):
    assert cron_prev(expr, before) == expected


def test_prev_and_next_bracket_every_time():
    expr = "7 */3 * * 1-5"
    when = D(2026, 10, 16, 13, 30)
    prev, nxt = cron_prev(expr, when), cron_next(expr, when)
    assert prev <= when < nxt
    assert cron_next(expr, prev) == nxt


class TestTheHorizon:
    def test_an_impossible_expr_raises_instead_of_hanging(self):
        started = time.monotonic()
        with pytest.raises(NoOccurrence):
            cron_next("0 0 30 2 *", D(2026, 10, 15))
        with pytest.raises(NoOccurrence):
            cron_prev("0 0 30 2 *", D(2026, 10, 15))
        assert time.monotonic() - started < 2.0

    def test_the_horizon_is_about_four_years(self):
        assert 4 * 365 <= CRON_HORIZON.days <= 4 * 366 + 1

    def test_no_occurrence_is_an_invalid_task(self):
        # So creation and load surface it exactly as any other invalid file.
        assert issubclass(NoOccurrence, InvalidTask)


@pytest.mark.parametrize(
    "expr",
    [
        "@daily",
        "0 9 * * MON",
        "0 9 * JAN *",
        "0 0 9 * * *",  # a seconds field
        "0 9 * *",
        "0 9 L * *",
        "0 9 * * 1#2",
        "0 9 15W * *",
        "0 9 ? * *",
        "60 * * * *",
        "0 24 * * *",
        "0 0 0 * *",
        "0 0 * 13 *",
        "0 0 * * 8",
        "*/0 * * * *",
        "5/10 * * * *",
        "0 5-3 * * *",
        "0 9,,10 * * *",
        "",
    ],
)
def test_unsupported_syntax_is_refused(expr):
    with pytest.raises(InvalidTask):
        parse_cron(expr)


def test_the_parsed_expr_is_normalised():
    assert parse_cron("0  9 *   * 1-5").expr == "0 9 * * 1-5"
