"""Bare day replies use the month shown in the dates example."""

from datetime import date

from app.services.dates import dates_prompt_anchor, resolve_bare_day_range_with_anchor


def test_bare_days_use_the_dates_prompt_example_month():
    today = date(2026, 11, 2)
    anchor = dates_prompt_anchor(today)
    assert anchor.month == 11
    start, end = resolve_bare_day_range_with_anchor("7 TO 11", anchor)
    assert start == date(2026, 11, 7)
    assert end == date(2026, 11, 11)
