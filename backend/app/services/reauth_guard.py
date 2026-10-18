"""Re-authenticate safety net.

The key icon removes an account's device registration and signs in again. On 2026-10-18 that sign-in silently
registered a DIFFERENT Amazon customer (the browser was logged into another Amazon account), and every download then
failed with "Content License denied / No Ownership ... for customer". So:

- before the old entry is removed, it is backed up next to AccountsSettings.json (same folder, same sensitivity);
- after sign-in, the new entry must belong to the SAME Amazon customer (IdentityTokens.AmazonAccountId) - if not,
  the old entry is put back and the caller is told plainly;
- a sign-in that fails, is cancelled, or never finishes (expired / app restarted) also gets the old entry back.

Only ids and booleans ever leave this module - never token values.
"""

import json
import os
import time
from pathlib import Path
from typing import Iterable, Optional

ACCOUNTS = "AccountsSettings.json"
BACKUP = "AccountsSettings.reauth-backup.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _write(path: Path, data: dict) -> None:
    """Atomic: a crash mid-write never leaves a half file (this one holds every account's login)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def find_entry(cfg: Path, account_id: str) -> Optional[dict]:
    for a in _read(cfg / ACCOUNTS).get("Accounts") or []:
        if isinstance(a, dict) and a.get("AccountId") == account_id:
            return a
    return None


def customer_id(entry: Optional[dict]) -> str:
    return str(((entry or {}).get("IdentityTokens") or {}).get("AmazonAccountId") or "")


def signed_in(entry: Optional[dict]) -> bool:
    """False for a missing entry AND for the placeholder Libation's login tool writes the moment a sign-in STARTS
    (AccountId only, no device, no customer - found on the test copy 2026-10-18)."""
    return bool(customer_id(entry))


def same_customer(old: Optional[dict], new: Optional[dict]) -> bool:
    """True only when both carry an Amazon customer id and it is the same one."""
    a, b = customer_id(old), customer_id(new)
    return bool(a) and a == b


def backup(cfg: Path, account_id: str, entry: dict, session_id: Optional[str] = None) -> None:
    data = _read(cfg / BACKUP)
    data[account_id] = {"entry": entry, "session_id": session_id, "at": time.time()}
    _write(cfg / BACKUP, data)


def set_session(cfg: Path, account_id: str, session_id: str) -> None:
    data = _read(cfg / BACKUP)
    if account_id in data:
        data[account_id]["session_id"] = session_id
        _write(cfg / BACKUP, data)


def pending(cfg: Path) -> dict:
    """{account_id: {"session_id", "at"}} - without the entries."""
    return {k: {"session_id": v.get("session_id"), "at": v.get("at")} for k, v in _read(cfg / BACKUP).items()}


def account_for_session(cfg: Path, session_id: str) -> Optional[str]:
    return next((k for k, v in _read(cfg / BACKUP).items() if v.get("session_id") == session_id), None)


def backed_up_entry(cfg: Path, account_id: str) -> Optional[dict]:
    rec = _read(cfg / BACKUP).get(account_id)
    return rec.get("entry") if rec else None


def drop(cfg: Path, account_id: str) -> None:
    data = _read(cfg / BACKUP)
    if data.pop(account_id, None) is not None:
        if data:
            _write(cfg / BACKUP, data)
        else:
            (cfg / BACKUP).unlink(missing_ok=True)


def restore(cfg: Path, account_id: str) -> bool:
    """Put the backed-up entry back (replacing whatever entry now has that AccountId) and forget the backup."""
    entry = backed_up_entry(cfg, account_id)
    if entry is None:
        return False
    data = _read(cfg / ACCOUNTS)
    accounts = [a for a in (data.get("Accounts") or []) if not (isinstance(a, dict) and a.get("AccountId") == account_id)]
    accounts.append(entry)
    data["Accounts"] = accounts
    _write(cfg / ACCOUNTS, data)
    drop(cfg, account_id)
    return True


def sweep(cfg: Path, live_sessions: Iterable[str]) -> list:
    """Backups whose sign-in is no longer running (expired, cancelled, app restarted): restore the old entry if the
    account is missing; if it is present again, the backup is stale and is dropped. Returns restored account ids."""
    live = set(live_sessions)
    restored = []
    for account_id, rec in pending(cfg).items():
        if rec.get("session_id") in live:
            continue
        if not signed_in(find_entry(cfg, account_id)):
            if restore(cfg, account_id):
                restored.append(account_id)
        else:
            drop(cfg, account_id)
    return restored
