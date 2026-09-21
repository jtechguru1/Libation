import asyncio
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from .auth import get_current_user
from ..database import SessionLocal, get_db
from ..models.download import Download, Scan
from ..models.user import DEFAULT_PERMISSIONS
from ..schemas.downloads import DownloadRequest, DownloadResponse, ScanResponse
from ..services import cli
from ..services.logger import get_logger

router = APIRouter(prefix="/api/downloads", tags=["downloads"])


def _require_permission(flag: str, user) -> None:
    if user.is_admin:
        return
    perms = user.permissions or DEFAULT_PERMISSIONS
    if not perms.get(flag, DEFAULT_PERMISSIONS.get(flag, True)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"Permission denied: {flag}")


def _enforce_cap(user, db: Session) -> None:
    """Raise 429 if user is at their 12-hour download cap."""
    if user.is_admin or user.download_cap is None:
        return
    window_start = datetime.now(timezone.utc) - timedelta(hours=12)
    used = db.query(func.count(Download.id)).filter(
        Download.user_id == user.id,
        Download.created_at > window_start,
    ).scalar() or 0
    if used >= user.download_cap:
        oldest = db.query(func.min(Download.created_at)).filter(
            Download.user_id == user.id,
            Download.created_at > window_start,
        ).scalar()
        resets_at = None
        if oldest:
            oldest_utc = oldest.replace(tzinfo=timezone.utc) if oldest.tzinfo is None else oldest
            resets_at = (oldest_utc + timedelta(hours=12)).isoformat()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"message": "Download cap reached", "resets_at": resets_at},
        )


# ── Background tasks ──────────────────────────────────────────────────────────

def enqueue_book(book_id: str, user_id: int, book_title: str | None = None) -> bool:
    """Add one book to the download queue. Returns False if it is already queued or running.

    Every path that wants a book downloaded goes through here — manual, bulk and auto-download — so
    the duplicate guard and the serial queue apply uniformly. If the only existing row for this book
    is a FAILED (`error`) one, that row is reused (reset to `queued`) rather than duplicated, so a
    retry never leaves two rows for the same ASIN. Nothing here starts a download; the single
    `_download_worker` does that, one book at a time.
    """
    with SessionLocal() as db:
        active = db.query(Download).filter(
            Download.book_id == book_id,
            Download.status.in_(["queued", "running"]),
        ).first()
        if active:
            return False

        # Reuse a prior FAILED row instead of inserting a second one. The old code only checked for
        # queued/running rows, so a book still stuck at `error` got a brand-new error row on every
        # retry — observed as duplicate "Failed" entries for the same ASIN. Flip the existing row
        # back to `queued`: it re-enters the SERIAL queue at the back (the worker picks queued rows
        # oldest-first) rather than running immediately. Matched by book_id — the ASIN is the book's
        # identity; book_title is display-only and may be null.
        failed = (
            db.query(Download)
            .filter(Download.book_id == book_id, Download.status == "error")
            .order_by(Download.id.desc())
            .first()
        )
        if failed:
            failed.status = "queued"
            failed.progress = 0
            failed.error_message = None
            failed.user_id = user_id
            failed.started_at = None
            failed.completed_at = None
            if book_title:
                failed.book_title = book_title
            db.commit()
            return True

        db.add(Download(
            book_id=book_id,
            book_title=book_title,
            user_id=user_id,
            status="queued",
        ))
        db.commit()
    return True


async def _auto_download_if_enabled() -> None:
    """After a successful scan, queue un-downloaded books for opted-in Audible accounts.

    This only ENQUEUES. The worker drains the queue one book at a time, so a scan that turns up 200
    new books does not start 200 downloads.

    The former 30-minute global cooldown was removed: with scheduled scans now able to run as often
    as every 15 minutes it would have silently skipped auto-download on most of them. The duplicate
    guard in `enqueue_book` plus the serial queue already prevent the stampede it was guarding
    against.
    """
    from ..services import libation as libation_svc
    from ..models.user import User as UserModel

    logger = get_logger()
    opted_in: list[str] = []
    admin_id: int = 1

    with SessionLocal() as db:
        try:
            conn = db.connection()
            rows = conn.execute(text(
                "SELECT account_id FROM audible_account_settings WHERE auto_download = 1"
            )).fetchall()
            opted_in = [r[0] for r in rows]
            if not opted_in:
                return

            admin = db.query(UserModel).filter(UserModel.is_admin == True).first()
            if admin:
                admin_id = admin.id

            conn.execute(text(
                "INSERT INTO system_settings (key, value) VALUES ('last_auto_download_at', :v) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ), {"v": datetime.now(timezone.utc).isoformat()})
            db.commit()
        except Exception as exc:
            logger.error("[auto-download] Could not read opted-in accounts: %s", exc, exc_info=True)
            return

    # Skip any book that currently has a FAILED row. enqueue_book (4a) would happily reuse an error
    # row, but the AUTOMATIC path must NOT: a licence-denied book fails on every scan, so re-queuing
    # it each time would hammer Audible and churn the Failed list endlessly. The user re-authenticates
    # and retries deliberately — manual and bulk downloads still reach enqueue_book and CAN retry a
    # failed book. Build the set once so the enqueue loop below is a cheap membership check.
    with SessionLocal() as db:
        failed_ids = {
            r.book_id
            for r in db.query(Download.book_id).filter(Download.status == "error").all()
        }

    total_queued = 0
    for account_id in opted_in:
        book_ids = libation_svc.get_liberate_book_ids(
            filter_status="not_liberated",
            account_id=account_id,
        )
        queued = sum(
            1 for book_id in book_ids
            if book_id not in failed_ids and enqueue_book(book_id, admin_id)
        )
        total_queued += queued
        logger.info("[auto-download] Account %s: %d un-downloaded book(s), %d newly queued",
                    account_id, len(book_ids), queued)

    if total_queued:
        logger.info("[auto-download] Queued %d book(s) across %d account(s)",
                    total_queued, len(opted_in))


# ── The download queue worker ─────────────────────────────────────────────────
#
# Exactly ONE of these runs, started from the lifespan in main.py. It is the only thing in the app
# that starts a download, which is what makes "one at a time" true rather than aspirational.
# Previously every caller spawned its own asyncio task, so queueing N books started N simultaneous
# downloads — and Audible is liable to flag an account downloading in bulk simultaneously.

_QUEUE_POLL_SECONDS = 2

# Stand-down period once the queue looks like it is failing systemically.
_BACKOFF_SECONDS = 30 * 60

# Consecutive failures before standing down. One or two failures are ordinary — a title genuinely
# not owned, a transient network error. Three in a row is a pattern, not bad luck.
_MAX_CONSECUTIVE_FAILURES = 3

# Explicit rate-limit markers. These trigger a stand-down on the FIRST occurrence when present, but
# they are a bonus, not the mechanism: Audible reports "CustomerThrottled" only inside the JSON body
# of the licence response, which Libation logs to its own file and does not surface in the output we
# capture. So the consecutive-failure counter above is what actually protects the account; matching
# on these strings alone would have been a guard that never fires.
_THROTTLE_MARKERS = ("customerthrottled", "being throttled", "too many requests", "429")


def _is_throttled(message: str) -> bool:
    low = (message or "").lower()
    return any(m in low for m in _THROTTLE_MARKERS)


async def _download_worker() -> None:
    """Drain the download queue serially, pausing `download_delay_seconds` between books."""
    from ..services.automation import get_download_delay_seconds

    logger = get_logger()
    logger.info("[queue] Download worker started — one download at a time")
    consecutive_failures = 0

    while True:
        try:
            with SessionLocal() as db:
                nxt = (
                    db.query(Download)
                    .filter(Download.status == "queued")
                    .order_by(Download.created_at.asc(), Download.id.asc())
                    .first()
                )
                job = (nxt.id, nxt.book_id, nxt.book_title) if nxt else None

            if job is None:
                await asyncio.sleep(_QUEUE_POLL_SECONDS)
                continue

            dl_id, book_id, title = job
            logger.info("[queue] Starting download %s (%s)", book_id, title or "untitled")
            await _run_download(dl_id, book_id)

            # Stand down when the queue starts failing systemically.
            #
            # A throttled account is the worst case: every queued book fails at the licence
            # endpoint, and without this the queue marches through all of them, making the
            # throttling worse. Observed live — Audible returned
            #     "RejectionReason": "CustomerThrottled"
            # and every download after that failed the same way, including a book owned outright.
            # Failed books are left as `error`; the ones still waiting stay QUEUED and resume on
            # their own once the backoff expires.
            with SessionLocal() as db:
                finished = db.get(Download, dl_id)
                failed = bool(finished and finished.status == "error")
                err = (finished.error_message or "") if finished else ""

            if failed:
                consecutive_failures += 1
            else:
                consecutive_failures = 0

            if _is_throttled(err) or consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "[queue] %d consecutive download failure(s) — pausing the queue for %d minutes "
                    "to avoid hammering Audible. Waiting books are kept and resume automatically. "
                    "Last error: %s",
                    consecutive_failures, _BACKOFF_SECONDS // 60, err[:200],
                )
                consecutive_failures = 0
                await asyncio.sleep(_BACKOFF_SECONDS)
                continue

            delay = get_download_delay_seconds()
            if delay > 0:
                # Only pause when more work is waiting — no reason to idle after the last book.
                with SessionLocal() as db:
                    more = db.query(Download).filter(Download.status == "queued").first()
                if more:
                    logger.info("[queue] Waiting %ds before the next download", delay)
                    await asyncio.sleep(delay)

        except asyncio.CancelledError:
            logger.info("[queue] Download worker stopping")
            raise
        except Exception as exc:
            # A crash here would silently stop every future download, so never let the loop die.
            logger.error("[queue] Worker error: %s", exc, exc_info=True)
            await asyncio.sleep(_QUEUE_POLL_SECONDS)


# ── The scheduled-scan loop ───────────────────────────────────────────────────
#
# Also started once from the lifespan. Without this, nothing ever re-scanned the library, so new
# books were never discovered and auto-download could never fire on its own.

async def _scan_scheduler() -> None:
    """Run a library scan every `scan_interval_minutes`. 0 disables it."""
    from ..services.automation import get_scan_interval_minutes

    logger = get_logger()

    # 🔴 last_run is PERSISTED, not in-memory.
    #
    # It used to start at None on every process start, so the scheduler scanned immediately on each
    # container restart. That looked harmless and is not: the entrypoint runs uvicorn in a
    # `while true` loop, so a crash-looping container would fire a full library scan every restart.
    # Observed live — three quick rebuilds produced five scans of a 681-title library in 21 minutes,
    # and Audible responded by throttling the account:
    #
    #     "RejectionReason": "CustomerThrottled"   (api.audible.com licenserequest)
    #
    # Once throttled, every download fails with ContentLicenseDenied — including books the customer
    # owns outright. Persisting the timestamp means a restart resumes the existing schedule instead
    # of restarting it, so restarts are free no matter how many happen.
    last_run = _get_last_scan_at()
    logger.info(
        "[scheduler] Scan scheduler started (last scan: %s)",
        last_run.isoformat() if last_run else "never",
    )

    while True:
        try:
            # Re-read every tick so a change in Settings applies without a restart.
            interval = get_scan_interval_minutes()
            if interval <= 0:
                await asyncio.sleep(60)
                continue

            now = datetime.now(timezone.utc)
            if last_run is not None and (now - last_run) < timedelta(minutes=interval):
                await asyncio.sleep(30)
                continue

            with SessionLocal() as db:
                already = db.query(Scan).filter(Scan.status == "running").first()
                if already:
                    await asyncio.sleep(30)
                    continue
                scan = Scan(status="running", started_at=now)
                db.add(scan)
                db.commit()
                db.refresh(scan)
                scan_id = scan.id

            last_run = now
            _set_last_scan_at(now)   # persist BEFORE scanning, so a crash mid-scan cannot loop
            logger.info("[scheduler] Starting scheduled library scan (every %d min)", interval)
            await _run_scan(scan_id)

        except asyncio.CancelledError:
            logger.info("[scheduler] Scan scheduler stopping")
            raise
        except Exception as exc:
            logger.error("[scheduler] Error: %s", exc, exc_info=True)
            await asyncio.sleep(60)


async def _run_download(download_id: int, book_id: str) -> None:
    with SessionLocal() as db:
        dl = db.get(Download, download_id)
        if dl:
            dl.status = "running"
            dl.started_at = datetime.now(timezone.utc)
            db.commit()

    async def _on_progress(pct: int, _line: str) -> None:
        with SessionLocal() as db:
            dl = db.get(Download, download_id)
            if dl:
                dl.progress = pct
                db.commit()

    try:
        exit_code, output = await cli.run_liberate([book_id], on_progress=_on_progress)
    except Exception as e:
        exit_code, output = 1, str(e)

    with SessionLocal() as db:
        dl = db.get(Download, download_id)
        if dl:
            dl.status = "complete" if exit_code == 0 else "error"
            dl.progress = 100 if exit_code == 0 else dl.progress
            dl.completed_at = datetime.now(timezone.utc)
            if exit_code != 0:
                dl.error_message = _summarize_error(output)
            db.commit()

            # A later success supersedes any earlier failure for the same book, so drop stale `error`
            # rows for this ASIN once it downloads cleanly — otherwise the book keeps showing under
            # Failed even though it is now downloaded. Matched by book_id because book_title may be
            # blank. The just-completed row is `complete`, so it is not touched.
            if exit_code == 0:
                db.query(Download).filter(
                    Download.book_id == book_id,
                    Download.status == "error",
                ).delete()
                db.commit()


_LAST_SCAN_KEY = "last_scheduled_scan_at"


def _get_last_scan_at() -> "datetime | None":
    """When the library was last scanned, across restarts. See the note in `_scan_scheduler`.

    Falls back to the newest row in `scans` when the key is absent. That matters on UPGRADE: an
    existing install has scan history but no stored key, so without this fallback the very first
    start on a new image would scan immediately — reintroducing, once per upgrade, exactly the
    behaviour this is here to prevent.
    """
    try:
        with SessionLocal() as db:
            conn = db.connection()
            row = conn.execute(
                text("SELECT value FROM system_settings WHERE key = :k"), {"k": _LAST_SCAN_KEY}
            ).first()
            if row and row[0]:
                dt = datetime.fromisoformat(row[0])
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

            prior = conn.execute(
                text("SELECT started_at FROM scans WHERE started_at IS NOT NULL "
                     "ORDER BY id DESC LIMIT 1")
            ).first()
        if prior and prior[0]:
            dt = prior[0] if isinstance(prior[0], datetime) else datetime.fromisoformat(str(prior[0]))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return None
    except Exception:
        return None


def _set_last_scan_at(when: datetime) -> None:
    try:
        with SessionLocal() as db:
            db.connection().execute(
                text(
                    "INSERT INTO system_settings (key, value) VALUES (:k, :v) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {"k": _LAST_SCAN_KEY, "v": when.isoformat()},
            )
            db.commit()
    except Exception as exc:
        get_logger().error("[scheduler] Could not persist last scan time: %s", exc)


_EXCEPTION_RE = re.compile(r"^\s*(?:[\w.]+\.)?(\w*Exception|\w*Error)\s*:\s*(.+)$", re.MULTILINE)


def _summarize_error(output: str, limit: int = 500) -> str:
    """Turn CLI/bridge output into something a user can act on.

    This used to be `output[-500:]` — the *tail* of the text. For a .NET stack trace that is the
    innermost frames, so the UI showed things like:

        s.Factory.cs:line 124
           at FileLiberator.DownloadOptions.GetDownloadLicenseAsync(...)

    ...cut off mid-word, while the line that actually says what went wrong sits at the TOP:

        AudibleApi.ContentLicenseDeniedException: Content License denied for asin: [B0H7KZ2YSB]

    So: lead with the exception message when there is one, and keep a little context after it.
    Falls back to the HEAD of the output rather than the tail, since CLI tools put the summary first.
    """
    if not output:
        return ""
    text_ = output.strip()

    m = _EXCEPTION_RE.search(text_)
    if m:
        headline = f"{m.group(1)}: {m.group(2)}".strip()
        return headline[:limit]

    # No exception line — the first few non-empty lines are the useful part.
    lines = [ln.strip() for ln in text_.splitlines() if ln.strip()]
    return " / ".join(lines[:3])[:limit] if lines else text_[:limit]


def _parse_books_added(output: str) -> int:
    """Pull the new-book count out of a `libationcli scan` result.

    LibationCli 13.x prints:

        Scan complete.
        Total processed: 682
        New: 1

    The original pattern looked for `N new book`, which that output never contains — so every scan
    recorded `books_added = 0` and the UI reported "Scan complete — 0 new books added" even when it
    had just imported one. The older phrasing is kept as a fallback in case a different CLI version
    uses it.
    """
    m = re.search(r"^\s*New:\s*(\d+)", output, re.IGNORECASE | re.MULTILINE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s+new\s+book", output, re.IGNORECASE)
    return int(m.group(1)) if m else 0


async def _run_scan(scan_id: int, account_id: str | None = None) -> None:
    try:
        exit_code, output = await cli.run_scan(account_id=account_id)
    except Exception as e:
        exit_code, output = 1, str(e)

    books_added = _parse_books_added(output)

    with SessionLocal() as db:
        scan = db.get(Scan, scan_id)
        if scan:
            scan.status = "complete" if exit_code == 0 else "error"
            scan.completed_at = datetime.now(timezone.utc)
            scan.books_added = books_added
            scan.output = output[:4000]
            if exit_code != 0:
                scan.error_message = _summarize_error(output)
            db.commit()

    if exit_code == 0:
        asyncio.create_task(_auto_download_if_enabled())


# ── Scan endpoints ────────────────────────────────────────────────────────────

# Minimum gap between MANUAL scans before we push back. Scheduled scans have their own interval and
# are not affected. 10 minutes is deliberately lenient: it is meant to stop back-to-back clicking,
# not to stop someone scanning when they have a reason to.
_MANUAL_SCAN_COOLDOWN_MINUTES = 10


@router.post("/scan", response_model=ScanResponse, tags=["library"])
async def start_scan(
    account_id: str | None = None,
    force: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Scan the library. With `account_id`, scans only that Audible account.

    `libationcli scan` takes optional positional account IDs; omitting one scans every account,
    which is the historical behaviour and remains the default.

    Refuses with 429 if a scan ran within the last `_MANUAL_SCAN_COOLDOWN_MINUTES`, unless
    `force=true`. Every scan queries Audible for the entire library, and scanning repeatedly gets
    the ACCOUNT rate-limited — after which downloads fail with a licence denial even for books the
    user owns outright. Observed for real: five scans of a 681-title library inside 21 minutes,
    followed by `"RejectionReason": "CustomerThrottled"` on every subsequent download.

    A user clicking a refresh button repeatedly has no way to know they are doing that to their own
    Audible account, so the UI has to say it. The override exists because there are legitimate
    reasons to rescan immediately — it just should not be the accidental default.
    """
    _require_permission("can_scan", current_user)
    already_running = db.query(Scan).filter(Scan.status == "running").first()
    if already_running:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="A scan is already in progress")

    if not force:
        last = (
            db.query(Scan)
            .filter(Scan.started_at.isnot(None))
            .order_by(Scan.id.desc())
            .first()
        )
        if last is not None and last.started_at is not None:
            started = last.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = datetime.now(timezone.utc) - started
            if elapsed < timedelta(minutes=_MANUAL_SCAN_COOLDOWN_MINUTES):
                mins_ago = max(0, int(elapsed.total_seconds() // 60))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "message": (
                            f"The library was scanned {mins_ago} minute(s) ago. Scanning repeatedly "
                            f"can get your Audible account rate-limited, which makes downloads fail "
                            f"until it clears."
                        ),
                        "last_scan_at": started.isoformat(),
                        "minutes_ago": mins_ago,
                        "cooldown_minutes": _MANUAL_SCAN_COOLDOWN_MINUTES,
                        "can_override": True,
                    },
                )
    scan = Scan(status="running", started_at=datetime.now(timezone.utc))
    db.add(scan)
    db.commit()
    db.refresh(scan)
    asyncio.create_task(_run_scan(scan.id, account_id))
    return scan


@router.get("/scan/latest", response_model=ScanResponse, tags=["library"])
def latest_scan(db: Session = Depends(get_db), _=Depends(get_current_user)):
    scan = db.query(Scan).order_by(Scan.id.desc()).first()
    if not scan:
        raise HTTPException(status_code=404, detail="No scans yet")
    return scan


# ── Download endpoints ────────────────────────────────────────────────────────

@router.post("", response_model=DownloadResponse, status_code=201)
async def queue_download(
    body: DownloadRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_permission("can_download", current_user)
    _enforce_cap(current_user, db)

    existing = (
        db.query(Download)
        .filter(
            Download.book_id == body.book_id,
            Download.status.in_(["queued", "running"]),
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This book is already queued or downloading",
        )

    dl = Download(
        book_id=body.book_id,
        book_title=body.book_title,
        user_id=current_user.id,
        status="queued",
    )
    db.add(dl)
    db.commit()
    db.refresh(dl)
    # No task is spawned here. The single `_download_worker` picks this row up, so queueing N books
    # results in N queued rows and one active download — not N concurrent downloads.
    return dl


@router.get("", response_model=list[DownloadResponse])
def list_downloads(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(Download).order_by(Download.created_at.desc()).limit(200).all()


@router.get("/{download_id}", response_model=DownloadResponse)
def get_download(
    download_id: int,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    dl = db.get(Download, download_id)
    if not dl:
        raise HTTPException(status_code=404, detail="Download not found")
    return dl


@router.delete("/failed", status_code=204)
def clear_failed_downloads(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Delete every failed (`error`) download row in one call.

    Same auth as removing a single error row: signed-in, no special permission. `_require_permission`
    only guards deleting COMPLETE downloads (a successful artifact); clearing failures just tidies the
    list. Declared BEFORE `/{download_id}` so the literal path `failed` is matched here instead of
    being routed into the int `download_id` param (which would 422).
    """
    db.query(Download).filter(Download.status == "error").delete()
    db.commit()
    return None


@router.delete("/{download_id}", status_code=204)
def delete_download(
    download_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    dl = db.get(Download, download_id)
    if not dl:
        raise HTTPException(status_code=404, detail="Download not found")
    if dl.status in ("queued", "running"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete an active download",
        )
    if dl.status == "complete":
        _require_permission("can_remove_downloads", current_user)
    db.delete(dl)
    db.commit()
