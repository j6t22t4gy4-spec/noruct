"""Small deterministic five-field cron evaluator for Noruct schedules.

It deliberately excludes names, seconds, time zones, macros and arbitrary
plugins. All matching is UTC and the standard day-of-month/day-of-week OR
rule applies when both fields are restricted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def _field(value: str, minimum: int, maximum: int) -> tuple[set[int], bool]:
    values: set[int] = set(); wildcard = value == "*"
    if not value or len(value) > 128: raise ValueError("Cron field is invalid")
    for part in value.split(","):
        base, step = (part.split("/", 1) + [None])[:2] if "/" in part else (part, None)
        if part.count("/") > 1: raise ValueError("Cron step is invalid")
        if step is not None and (not step.isdigit() or not 1 <= int(step) <= maximum - minimum + 1): raise ValueError("Cron step is invalid")
        if base == "*": start, end = minimum, maximum
        elif "-" in base:
            pair = base.split("-", 1)
            if len(pair) != 2 or not all(item.isdigit() for item in pair): raise ValueError("Cron range is invalid")
            start, end = map(int, pair)
        elif base.isdigit(): start = end = int(base)
        else: raise ValueError("Cron field is invalid")
        if not minimum <= start <= end <= maximum: raise ValueError("Cron value is outside its field range")
        values.update(range(start, end + 1, int(step or "1")))
    return values, wildcard


@dataclass(frozen=True, slots=True)
class CronExpression:
    value: str
    minute: set[int]; hour: set[int]; day: set[int]; month: set[int]; weekday: set[int]
    day_wildcard: bool; weekday_wildcard: bool

    @classmethod
    def parse(cls, value: str) -> "CronExpression":
        if not isinstance(value, str): raise ValueError("Cron expression must be text")
        fields = value.strip().split()
        if len(fields) != 5: raise ValueError("Cron expression requires exactly five fields: minute hour day month weekday")
        parsed = [_field(field, *bounds) for field, bounds in zip(fields, _FIELDS, strict=True)]
        return cls(" ".join(fields), parsed[0][0], parsed[1][0], parsed[2][0], parsed[3][0], parsed[4][0], parsed[2][1], parsed[4][1])

    def matches(self, moment: datetime) -> bool:
        weekday = (moment.weekday() + 1) % 7
        if moment.minute not in self.minute or moment.hour not in self.hour or moment.month not in self.month: return False
        dom, dow = moment.day in self.day, weekday in self.weekday
        day_match = dom and dow if self.day_wildcard or self.weekday_wildcard else dom or dow
        return day_match

    def next_after(self, moment: datetime) -> datetime:
        candidate = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        # Five years bounds malformed calendars without silently scheduling forever.
        for _ in range(5 * 366 * 24 * 60):
            if self.matches(candidate): return candidate
            candidate += timedelta(minutes=1)
        raise ValueError("Cron expression has no matching UTC time within five years")
