import asyncio
import re
from datetime import datetime, timedelta, timezone

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models.download import Download, Scan
from ..models.user import DEFAULT_PERMISSIONS
from ..schemas.liberate import LiberateResponse, DownloadCapStatus
from ..schemas.downloads import ScanResponse
from ..services import libation as lib_svc
from ..services import cli as cli_svc
from .auth import get_current_user

router = APIRouter(prefix="/api/liberate", tags=["liberate"])


def _require_permission(flag: str, user) -> None:
    if user.is_admin:
        return
    perms = user.permissions or DEFAULT_PERMISSIONS
    if not perms.get(flag, DEFAULT_PERMISSIONS.get(flag, True)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"Permission denied: {flag}")


def _cap_status(user, db: Session) -> dict:
    """Returns cap accounting info for the current 12h window."""
    if user.is_admin or user.download_cap is None:
        return {"cap": None, "used": 0, "remaining": None, "resets_at": None}
    window_start = datetime.now(timezone.utc) - timedelta(hours=12)
    used = db.query(func.count(Download.id)).filter(
        Download.user_id == user.id,
        Download.created_at > window_start,
    ).scalar() or 0
    remaining = max(0, user.download_cap - used)
    # resets_at = oldest download in window + 12h
    oldest = db.query(func.min(Download.created_at)).filter(
        Download.user_id == user.id,
        Download.created_at > window_start,
    ).scalar()
    resets_at = None
    if oldest and used >= user.download_cap:
        oldest_utc = oldest.replace(tzinfo=timezone.utc) if oldest.tzinfo is None else oldest
        resets_at = (oldest_utc + timedelta(hours=12)).isoformat()
    return {"cap": user.download_cap, "used": used, "remaining": remaining, "resets_at": resets_at}


@router.get("/cap", response_model=DownloadCapStatus)
def get_cap_status(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    return _cap_status(current_user, db)


class BookStatusUpdate(BaseModel):
    liberated: bool


@router.patch("/books/{book_id}")
def update_book_status(
    book_id: str,
    body: BookStatusUpdate,
    current_user=Depends(get_current_user),
):
    _require_permission("can_liberate", current_user)
    ok = lib_svc.set_book_status(book_id, body.liberated)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Book not found")
    return {"book_id": book_id, "liberated": body.liberated}


@router.get("/book-ids")
def list_liberate_book_ids(
    filter_status: str = "all",
    account_id: Optional[str] = None,
    search: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_permission("can_liberate", current_user)
    active_rows = db.query(Download).filter(Download.status.in_(["queued", "running"])).all()
    active_ids = {r.book_id for r in active_rows}
    ids = lib_svc.get_liberate_book_ids(
        filter_status=filter_status,
        account_id=account_id or None,
        active_download_ids=active_ids,
        search=search,
    )
    return {"ids": ids, "total": len(ids)}


@router.get("/books", response_model=LiberateResponse)
def list_liberate_books(
    filter_status: str = "all",
    page: int = 1,
    page_size: int = 48,
    account_id: Optional[str] = None,
    search: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_permission("can_liberate", current_user)

    # Build active download map: book_id → (status, progress)
    active_rows = (
        db.query(Download)
        .filter(Download.status.in_(["queued", "running"]))
        .all()
    )
    active = {r.book_id: (r.status, r.progress) for r in active_rows}

    result = lib_svc.get_liberate_books(
        active_downloads=active,
        filter_status=filter_status,
        page=page,
        page_size=page_size,
        account_id=account_id or None,
        search=search,
    )
    return result


class BulkQueueResponse(BaseModel):
    """Result of a bulk enqueue — what the UI reports back to the user."""
    queued: int
    skipped: int
    total: int


@router.post("/download-all", response_model=BulkQueueResponse, status_code=202)
async def download_all(
    account_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Queue every not-yet-downloaded book. Only available when the user has no download cap.

    Two things changed here and both matter:

    1. This used to be `def`, not `async def`. FastAPI runs sync handlers in a worker thread with no
       running event loop, so the `asyncio.create_task(...)` it ended with raised
       `RuntimeError: no running event loop` — a 500 on *every* call, for every user including
       admin. That is the "Failed to start bulk download" the UI reported.

    2. It used to shell out to `libationcli liberate --force`, which downloads books concurrently
       under its own control. Audible is liable to flag an account downloading in bulk
       simultaneously, so bulk now enqueues into the same serial queue everything else uses and the
       worker drains it one book at a time.

    Already-downloaded books are never enqueued: the filter is `not_liberated`, and the CLI would
    not re-download them anyway.
    """
    from .downloads import enqueue_book

    _require_permission("can_liberate", current_user)

    if not current_user.is_admin and current_user.download_cap is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Download cap is set on your account. Use individual downloads.",
        )

    book_ids = lib_svc.get_liberate_book_ids(
        filter_status="not_liberated",
        account_id=account_id or None,
    )
    if not book_ids:
        return BulkQueueResponse(queued=0, skipped=0, total=0)

    queued = sum(1 for book_id in book_ids if enqueue_book(book_id, current_user.id))
    return BulkQueueResponse(
        queued=queued,
        skipped=len(book_ids) - queued,
        total=len(book_ids),
    )
