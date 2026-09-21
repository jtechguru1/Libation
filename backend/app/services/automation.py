"""Automation settings — scheduled library scans and download pacing.

Values live in the existing `system_settings` key/value table (created by `_migrate_db`), so no new
table is needed. They are read fresh on every scheduler tick and before every download, which is
what lets a change in Settings take effect without restarting the container.
"""

from typing import Optional

from sqlalchemy import text

from ..database import SessionLocal

# Key names in system_settings
SCAN_INTERVAL_KEY = "scan_interval_minutes"
DOWNLOAD_DELAY_KEY = "download_delay_seconds"

# Defaults applied when the row is missing or unparseable.
DEFAULT_SCAN_INTERVAL_MINUTES = 360   # 6 hours
DEFAULT_DOWNLOAD_DELAY_SECONDS = 30

# Allowed values, mirrored by the Settings page dropdowns. 0 = off / no delay.
SCAN_INTERVAL_CHOICES = [0, 15, 30, 60, 180, 360, 720, 1440]
DOWNLOAD_DELAY_CHOICES = [0, 15, 30, 60, 300]


def get_int_setting(key: str, default: int) -> int:
    """Read an integer from system_settings, falling back to `default` on anything unexpected."""
    try:
        with SessionLocal() as db:
            row = db.connection().execute(
                text("SELECT value FROM system_settings WHERE key = :k"), {"k": key}
            ).first()
        if not row or row[0] in (None, ""):
            return default
        return int(row[0])
    except Exception:
        return default


def set_int_setting(key: str, value: int) -> None:
    """Upsert an integer into system_settings.

    INSERT-then-UPDATE via ON CONFLICT: a bare UPDATE silently affects zero rows when the key was
    never seeded, which would make a saved setting look accepted while changing nothing.
    """
    with SessionLocal() as db:
        db.connection().execute(
            text(
                "INSERT INTO system_settings (key, value) VALUES (:k, :v) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ),
            {"k": key, "v": str(int(value))},
        )
        db.commit()


def get_scan_interval_minutes() -> int:
    """Minutes between automatic library scans. 0 disables scheduled scanning."""
    val = get_int_setting(SCAN_INTERVAL_KEY, DEFAULT_SCAN_INTERVAL_MINUTES)
    return val if val in SCAN_INTERVAL_CHOICES else DEFAULT_SCAN_INTERVAL_MINUTES


def get_download_delay_seconds() -> int:
    """Seconds to wait between consecutive downloads. 0 means back-to-back."""
    val = get_int_setting(DOWNLOAD_DELAY_KEY, DEFAULT_DOWNLOAD_DELAY_SECONDS)
    return val if val in DOWNLOAD_DELAY_CHOICES else DEFAULT_DOWNLOAD_DELAY_SECONDS


def get_automation_settings() -> dict:
    return {
        "scan_interval_minutes": get_scan_interval_minutes(),
        "download_delay_seconds": get_download_delay_seconds(),
        "scan_interval_choices": SCAN_INTERVAL_CHOICES,
        "download_delay_choices": DOWNLOAD_DELAY_CHOICES,
    }


def update_automation_settings(
    scan_interval_minutes: Optional[int] = None,
    download_delay_seconds: Optional[int] = None,
) -> dict:
    """Validate and persist. Raises ValueError on a value outside the allowed set."""
    if scan_interval_minutes is not None:
        if scan_interval_minutes not in SCAN_INTERVAL_CHOICES:
            raise ValueError(
                f"scan_interval_minutes must be one of {SCAN_INTERVAL_CHOICES}"
            )
        set_int_setting(SCAN_INTERVAL_KEY, scan_interval_minutes)

    if download_delay_seconds is not None:
        if download_delay_seconds not in DOWNLOAD_DELAY_CHOICES:
            raise ValueError(
                f"download_delay_seconds must be one of {DOWNLOAD_DELAY_CHOICES}"
            )
        set_int_setting(DOWNLOAD_DELAY_KEY, download_delay_seconds)

    return get_automation_settings()
