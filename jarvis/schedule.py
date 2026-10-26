"""Dependency-free schedule specs for unattended desktop tasks.

Exactly one of: {"at": ISO-8601 with offset} | {"cron": "m h dom mon dow"} | {"every_minutes": 5..10080}.
Cron evaluation uses tz-aware local wall time: nonexistent DST times are skipped and
ambiguous (repeated) times fire once (first occurrence, fold=0).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Kolkata"
HORIZON_DAYS = 366
MIN_EVERY, MAX_EVERY = 5, 10080

_MONTHS = {n: i for i, n in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_DAYS = {n: i for i, n in enumerate("sun mon tue wed thu fri sat".split())}
# (low, high, names)
_FIELDS = ((0, 59, {}), (0, 23, {}), (1, 31, {}), (1, 12, _MONTHS), (0, 7, _DAYS))


class ScheduleError(ValueError):
    """Invalid or unsatisfiable schedule. Message is safe to show to the user."""


def _number(text, low, high, names):
    text = text.lower()
    if text in names:
        return names[text]
    if not text.isdigit() or len(text) > 2:
        raise ScheduleError("Invalid cron value")
    value = int(text)
    if not low <= value <= high:
        raise ScheduleError("Cron value out of range")
    return value


def _field(text, low, high, names):
    values = set()
    for item in text.split(","):
        if not item:
            raise ScheduleError("Empty cron list item")
        base, _, step_text = item.partition("/")
        step = 1
        if _:
            if not step_text.isdigit() or int(step_text) < 1 or int(step_text) > high:
                raise ScheduleError("Invalid cron step")
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            a, _, b = base.partition("-")
            start, end = _number(a, low, high, names), _number(b, low, high, names)
            if start > end:
                raise ScheduleError("Invalid cron range")
        else:
            start = _number(base, low, high, names)
            end = high if _ else start
        values.update(range(start, end + 1, step))
    return frozenset(values)


class Cron:
    def __init__(self, expr):
        if not isinstance(expr, str) or len(expr) > 100:
            raise ScheduleError("Cron must be a 5-field string")
        parts = expr.split()
        if len(parts) != 5:
            raise ScheduleError("Cron must have exactly 5 fields: minute hour day-of-month month day-of-week")
        self.expr = " ".join(parts)
        self.minutes, self.hours, self.doms, self.months, dows = (_field(p, *f) for p, f in zip(parts, _FIELDS))
        self.dows = frozenset(0 if d == 7 else d for d in dows)
        # Vixie semantics: a field starting with '*' is unrestricted for the OR rule.
        self.dom_any, self.dow_any = parts[2].startswith("*"), parts[4].startswith("*")

    def _day_matches(self, day: date):
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.doms
        dow_ok = (day.isoweekday() % 7) in self.dows
        if self.dom_any or self.dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def next_after(self, moment: datetime):
        """First matching existing local time strictly after `moment`, or None within the horizon."""
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise ScheduleError("A timezone-aware datetime is required")
        tz = moment.tzinfo
        after = moment.timestamp()
        start = moment.astimezone(tz).date()
        hours, minutes = sorted(self.hours), sorted(self.minutes)
        for offset in range(HORIZON_DAYS + 1):
            day = start + timedelta(days=offset)
            if not self._day_matches(day):
                continue
            for hour in hours:
                for minute in minutes:
                    naive = datetime(day.year, day.month, day.day, hour, minute)
                    candidate = naive.replace(tzinfo=tz, fold=0)
                    stamp = candidate.timestamp()
                    if stamp <= after:
                        continue
                    if datetime.fromtimestamp(stamp, tz).replace(tzinfo=None) != naive:
                        continue  # Nonexistent local time (DST gap): skip, never shift.
                    return candidate
        return None


def parse_cron(expr):
    return Cron(expr)


# Used only when the OS/tzdata package lacks IANA data (e.g. bare Windows venvs).
# Only zones with no DST and no planned changes are safe as fixed offsets.
_FIXED = {"Asia/Kolkata": 330, "Asia/Calcutta": 330, "UTC": 0, "Etc/UTC": 0, "GMT": 0, "Etc/GMT": 0}


def zone(name):
    if name is None:
        name = DEFAULT_TZ
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or ".." in name or name.startswith(("/", "\\")):
        raise ScheduleError("Invalid timezone")
    try:
        return name, ZoneInfo(name)
    except Exception:
        if name in _FIXED:
            return name, timezone(timedelta(minutes=_FIXED[name]), name)
        raise ScheduleError("Unknown timezone (IANA data may be unavailable on this machine)") from None


def iana_available():
    try:
        ZoneInfo("America/New_York")
        return True
    except Exception:
        return False


def _at(text, now):
    if not isinstance(text, str) or len(text) > 64:
        raise ScheduleError("'at' must be an ISO-8601 datetime with offset")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ScheduleError("'at' must be an ISO-8601 datetime with offset") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ScheduleError("'at' must include a UTC offset, e.g. +05:30")
    stamp = moment.timestamp()
    if stamp <= now:
        raise ScheduleError("'at' is in the past")
    if stamp > now + HORIZON_DAYS * 86400:
        raise ScheduleError("'at' is more than 366 days away")
    return stamp


def validate_schedule(schedule, tz_name, now):
    """Return (normalized spec, tz name, first fire timestamp) or raise ScheduleError."""
    name, tz = zone(tz_name)
    if not isinstance(schedule, dict) or len(schedule) != 1 or not set(schedule) <= {"at", "cron", "every_minutes"}:
        raise ScheduleError("Schedule must be exactly one of: at, cron, every_minutes")
    (kind, value), = schedule.items()
    if kind == "at":
        return {"at": value}, name, _at(value, now)
    if kind == "every_minutes":
        if type(value) is not int or not MIN_EVERY <= value <= MAX_EVERY:
            raise ScheduleError(f"every_minutes must be an integer from {MIN_EVERY} to {MAX_EVERY}")
        return {"every_minutes": value}, name, now + value * 60
    cron = parse_cron(value)
    first = cron.next_after(datetime.fromtimestamp(now, tz))
    if first is None:
        raise ScheduleError("Cron never fires within 366 days")
    return {"cron": cron.expr}, name, first.timestamp()


def next_run(spec, tz_name, after, *, anchor):
    """Next fire timestamp strictly after `after`; None when finished (one-shot)."""
    _, tz = zone(tz_name)
    if "at" in spec:
        stamp = datetime.fromisoformat(spec["at"]).timestamp()
        return stamp if stamp > after else None
    if "every_minutes" in spec:
        interval = spec["every_minutes"] * 60
        steps = max(1, int((after - anchor) // interval) + 1)
        stamp = anchor + steps * interval
        while stamp <= after:
            stamp += interval
        return stamp
    found = parse_cron(spec["cron"]).next_after(datetime.fromtimestamp(after, tz))
    return found.timestamp() if found else None


def local_iso(stamp, tz_name):
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp, zone(tz_name)[1]).isoformat()


def spoken(stamp, tz_name):
    """Human-readable for voice; no symbols beyond the time itself."""
    if stamp is None:
        return "no further runs"
    name, tz = zone(tz_name)
    moment = datetime.fromtimestamp(stamp, tz)
    return f"{moment:%A} {moment.day} {moment:%B %Y} at {moment:%H:%M} {name} time"


__all__ = ["Cron", "ScheduleError", "parse_cron", "validate_schedule", "next_run", "local_iso", "spoken", "zone", "DEFAULT_TZ"]
