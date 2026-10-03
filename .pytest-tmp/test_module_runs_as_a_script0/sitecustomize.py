import datetime as _datetime
class _FixedDateTime(_datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 3, 15, 0, tzinfo=_datetime.timezone.utc)
_datetime.datetime = _FixedDateTime
