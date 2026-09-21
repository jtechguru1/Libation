from pydantic import BaseModel, ConfigDict, field_serializer
from typing import Optional
from datetime import datetime, timezone


def _utc_iso(dt: Optional[datetime]) -> Optional[str]:
    """Serialise a datetime with an explicit UTC offset.

    The `downloads` and `scans` tables use plain `Column(DateTime)`, which drops tzinfo on write —
    so a value stored as `datetime.now(timezone.utc)` comes back naive and serialises as
    "2026-09-08T02:39:47" with no offset. JavaScript treats a date-time string WITHOUT an offset as
    LOCAL time, so a browser in CST would read a UTC timestamp as 02:39 local and be five hours out.

    Everything written to these columns is UTC, so attaching UTC to a naive value is a restatement
    of what is already true, not a guess.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class ScanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    books_added: int
    output: Optional[str] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @field_serializer("started_at", "completed_at")
    def _ser_dt(self, dt: Optional[datetime], _info) -> Optional[str]:
        return _utc_iso(dt)


class DownloadRequest(BaseModel):
    book_id: str
    book_title: Optional[str] = None


class DownloadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    book_id: str
    book_title: Optional[str] = None
    status: str
    progress: int
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime

    @field_serializer("started_at", "completed_at", "created_at")
    def _ser_dt(self, dt: Optional[datetime], _info) -> Optional[str]:
        return _utc_iso(dt)
