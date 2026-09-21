import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends

from .auth import get_current_user
from ..version import APP_VERSION

router = APIRouter(prefix="/api/updates", tags=["updates"])

# Where CHANGELOG.md lives relative to this file, depending on where we're running:
#   - In the Docker image: Dockerfile copies backend/app -> /app/app and CHANGELOG.md -> /app/CHANGELOG.md,
#     so this file lives at /app/app/api/updates.py and the changelog is two levels up from /app/app.
#   - In local dev: this file lives at backend/app/api/updates.py and the changelog is at the repo root,
#     three levels up from backend/app/api.
_CHANGELOG_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "CHANGELOG.md",  # image: /app/CHANGELOG.md
    Path(__file__).resolve().parents[3] / "CHANGELOG.md",  # local dev: repo root
]


@router.get("/version")
def get_cli_version(_=Depends(get_current_user)):
    """Return the installed LibationCLI version. Read-only — updating is done via image rebuild."""
    try:
        result = subprocess.run(
            ["libationcli", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        output = (result.stderr or "") + (result.stdout or "")
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return {"cli_version": match.group(1) if match else None, "app_version": APP_VERSION}
    except Exception:
        return {"cli_version": None, "app_version": APP_VERSION}


@router.get("/changelog")
def get_changelog(_=Depends(get_current_user)):
    """Return the raw CHANGELOG.md markdown for the Settings -> About "What's new" viewer.

    Never 500s: a missing file (unexpected layout, stripped-down image) just means nothing to show.
    """
    for candidate in _CHANGELOG_CANDIDATES:
        try:
            if candidate.is_file():
                return {"markdown": candidate.read_text(encoding="utf-8"), "available": True}
        except Exception:
            continue
    return {"markdown": "", "available": False}
