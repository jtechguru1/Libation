"""Through the real endpoints: re-auth refuses a different Amazon customer and never loses the account; the
download queue's pause is visible and an admin can end it. Runs in the app image (the app modules create /data on import)."""

import asyncio
import json
import os
import tempfile
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db"))

from fastapi import HTTPException          # noqa: E402
from sqlalchemy import text                # noqa: E402

from app.api import accounts as acc_api    # noqa: E402
from app.api import downloads as dl_api    # noqa: E402
from app.database import Base, SessionLocal, engine as db_engine   # noqa: E402
import app.models.download  # noqa: E402,F401
import app.models.user      # noqa: E402,F401
from app.models.download import Download  # noqa: E402
from app.schemas.accounts import CompleteLoginRequest  # noqa: E402
from app.services import reauth_guard as g  # noqa: E402

ADMIN = SimpleNamespace(id=1, is_admin=True)
USER = SimpleNamespace(id=2, is_admin=False)


def entry(account_id, customer, serial="0" * 20):
    return {"AccountId": account_id, "IdentityTokens": {"AmazonAccountId": customer, "DeviceSerialNumber": serial}}


@pytest.fixture()
def cfg(monkeypatch, tmp_path):
    (tmp_path / g.ACCOUNTS).write_text(json.dumps({"Accounts": [entry("josh", "J"), entry("christian", "X", "a" * 40)]}))
    monkeypatch.setattr(acc_api.settings, "LIBATION_CONFIG", str(tmp_path))
    state = {"next_customer": "X", "complete_fails": False, "start_fails": False, "live": []}

    async def list_accounts():
        return [{"account_id": "christian", "locale": "us"}, {"account_id": "josh", "locale": "us"}]

    async def start_login(email, locale):
        if state["start_fails"]:
            raise RuntimeError("login-external did not start")
        state["live"].append("s1")
        # like the real login tool: a placeholder entry appears the moment the sign-in starts
        data = json.loads((tmp_path / g.ACCOUNTS).read_text())
        data["Accounts"].append({"AccountId": email, "AccountName": email, "IdentityTokens": None})
        (tmp_path / g.ACCOUNTS).write_text(json.dumps(data))
        return {"session_id": "s1", "login_url": "https://amazon/login"}

    async def complete_login(session_id, response_url):
        state["live"].remove(session_id)
        if state["complete_fails"]:
            raise RuntimeError("bad redirect url")
        data = json.loads((tmp_path / g.ACCOUNTS).read_text())          # what login-external does: save the entry
        data["Accounts"] = [a for a in data["Accounts"] if a["AccountId"] != "christian"]
        data["Accounts"].append(entry("christian", state["next_customer"]))
        (tmp_path / g.ACCOUNTS).write_text(json.dumps(data))
        return "ok"
    monkeypatch.setattr(acc_api.cli, "list_accounts", list_accounts)
    monkeypatch.setattr(acc_api.cli, "start_login", start_login)
    monkeypatch.setattr(acc_api.cli, "complete_login", complete_login)
    monkeypatch.setattr(acc_api.cli, "live_login_sessions", lambda: list(state["live"]))
    return SimpleNamespace(path=tmp_path, state=state)


def christian(c):
    return next((a for a in json.loads((c.path / g.ACCOUNTS).read_text())["Accounts"] if a["AccountId"] == "christian"), None)


def reauth():
    return asyncio.run(acc_api.reauthenticate_account("christian", _=ADMIN))


def complete():
    return asyncio.run(acc_api.login_complete(CompleteLoginRequest(session_id="s1", response_url="https://x"),
                                              current_user=ADMIN, db=None))


def test_a_sign_in_as_a_different_amazon_customer_is_refused_and_the_old_entry_comes_back(cfg):
    reauth()
    assert not g.signed_in(christian(cfg))                            # only the start placeholder while signing in
    cfg.state["next_customer"] = "Y"
    with pytest.raises(HTTPException) as e:
        complete()
    assert e.value.status_code == 409 and "different Amazon account" in e.value.detail
    assert g.customer_id(christian(cfg)) == "X" and g.pending(cfg.path) == {}


def test_the_same_customer_is_accepted_and_the_new_registration_kept(cfg):
    reauth()
    complete()
    c = christian(cfg)
    assert g.customer_id(c) == "X" and len(c["IdentityTokens"]["DeviceSerialNumber"]) == 20
    assert g.pending(cfg.path) == {}


def test_a_failed_sign_in_puts_the_account_back(cfg):
    reauth()
    cfg.state["complete_fails"] = True
    with pytest.raises(HTTPException) as e:
        complete()
    assert "put back" in e.value.detail and g.customer_id(christian(cfg)) == "X"


def test_a_sign_in_that_cannot_start_changes_nothing(cfg):
    cfg.state["start_fails"] = True
    with pytest.raises(HTTPException) as e:
        reauth()
    assert "Nothing was changed" in e.value.detail and g.customer_id(christian(cfg)) == "X"


def test_closing_the_window_stops_the_login_tool_and_puts_the_account_back(cfg, monkeypatch):
    aborted = []
    monkeypatch.setattr(acc_api.cli, "abort_login", lambda sid: aborted.append(sid) or True)
    reauth()
    r = asyncio.run(acc_api.cancel_login({"session_id": "s1"}, _=ADMIN))
    assert "put back" in r.message and g.customer_id(christian(cfg)) == "X" and aborted == ["s1"]


def test_an_expired_or_restarted_sign_in_is_put_back_when_the_accounts_are_listed(cfg, monkeypatch):
    reauth()
    cfg.state["live"].clear()                                         # session expired / app restarted
    monkeypatch.setattr(acc_api, "_load_accounts_file", lambda: {})
    with SessionLocal() as db:
        db.connection().execute(text("CREATE TABLE IF NOT EXISTS audible_account_settings (account_id TEXT PRIMARY KEY, "
                                     "added_by_user_id INTEGER, auto_download INTEGER NOT NULL DEFAULT 0)"))
        Base.metadata.create_all(db_engine)
        db.commit()
        asyncio.run(acc_api.get_accounts(current_user=ADMIN, db=db))
    assert g.customer_id(christian(cfg)) == "X"


def test_a_plain_add_account_is_not_checked(cfg):
    cfg.state["live"].append("s1")
    cfg.state["next_customer"] = "NEW"
    complete()                                                        # no backup for this session: nothing refused
    assert g.pending(cfg.path) == {}


# ── the queue pause ────────────────────────────────────────────────────────────

def test_the_queue_says_when_it_paused_and_an_admin_can_start_it_again(monkeypatch):
    Base.metadata.create_all(db_engine)
    with db_engine.connect() as c:
        c.execute(text("DELETE FROM downloads"))
        c.execute(text("INSERT OR IGNORE INTO users (id, username, hashed_password, totp_enabled, is_active, is_admin) "
                       "VALUES (1, 'admin', 'x', 0, 1, 1)"))
        c.execute(text("INSERT INTO downloads (id, book_id, user_id, status, progress) VALUES (1, 'B0FAIL0001', 1, 'queued', 0)"))
        c.commit()

    async def failing(download_id, book_id):
        with SessionLocal() as db:
            d = db.get(Download, download_id)
            d.status, d.error_message = "error", "ContentLicenseDeniedException: Content License denied"
            db.commit()
    monkeypatch.setattr(dl_api, "_run_download", failing)
    monkeypatch.setattr(dl_api, "_MAX_CONSECUTIVE_FAILURES", 1)
    monkeypatch.setattr(dl_api, "_BACKOFF_SECONDS", 600)
    monkeypatch.setattr(dl_api, "_QUEUE_POLL_SECONDS", 0.05)

    async def scenario():
        assert dl_api.queue_state(_=USER)["paused_until"] is None
        task = asyncio.create_task(dl_api._download_worker())
        for _ in range(100):
            if dl_api._QUEUE_STATE["until"]:
                break
            await asyncio.sleep(0.02)
        st = dl_api.queue_state(_=USER)
        assert st["state"] == "paused" and st["paused_until"] and st["queued"] == 0
        assert "1 downloads in a row failed" in st["reason"] and "License" in st["reason"]
        with pytest.raises(HTTPException):
            dl_api.queue_resume(current_user=USER)                     # admins only
        assert dl_api.queue_resume(current_user=ADMIN)["resumed"] is True
        for _ in range(100):
            if not dl_api._QUEUE_STATE["until"]:
                break
            await asyncio.sleep(0.02)
        assert dl_api.queue_state(_=USER)["paused_until"] is None      # ended long before the 600 s
        task.cancel()
    asyncio.run(scenario())



def _queue(*ids):
    Base.metadata.create_all(db_engine)
    with db_engine.connect() as c:
        c.execute(text("DELETE FROM downloads"))
        c.execute(text("INSERT OR IGNORE INTO users (id, username, hashed_password, totp_enabled, is_active, is_admin) "
                       "VALUES (1, 'admin', 'x', 0, 1, 1)"))
        for i in ids:
            c.execute(text("INSERT INTO downloads (id, book_id, book_title, user_id, status, progress) "
                           "VALUES (:i, :b, :t, 1, 'queued', 0)"), {"i": i, "b": f"B0OK{i:06d}", "t": f"Book {i}"})
        c.commit()


def test_the_gap_between_downloads_is_shown_and_start_now_skips_it(monkeypatch):
    _queue(1, 2)

    async def ok(download_id, book_id):
        with SessionLocal() as db:
            db.get(Download, download_id).status = "complete"
            db.commit()
    monkeypatch.setattr(dl_api, "_run_download", ok)
    monkeypatch.setattr(dl_api, "_QUEUE_POLL_SECONDS", 0.05)
    import app.services.automation as auto
    monkeypatch.setattr(auto, "get_download_delay_seconds", lambda: 600)

    async def scenario():
        task = asyncio.create_task(dl_api._download_worker())
        for _ in range(100):
            if dl_api._QUEUE_STATE["state"] == "waiting":
                break
            await asyncio.sleep(0.02)
        st = dl_api.queue_state(_=USER)
        assert st["state"] == "waiting" and st["queued"] == 1 and "between downloads" in st["reason"]
        dl_api.queue_resume(current_user=ADMIN)
        for _ in range(100):
            with SessionLocal() as db:
                if db.get(Download, 2).status == "complete":
                    break
            await asyncio.sleep(0.02)
        with SessionLocal() as db:
            assert db.get(Download, 2).status == "complete"          # did not wait the 600 s
        task.cancel()
    asyncio.run(scenario())


def test_a_dead_worker_shows_as_stalled_and_start_now_starts_a_new_one(monkeypatch):
    _queue(5)
    started = []
    monkeypatch.setattr(dl_api, "_download_worker", lambda: started.append(1) or asyncio.sleep(0))
    dl_api._QUEUE_STATE.update(state="idle", beat="2000-01-01T00:00:00+00:00")       # no heartbeat for ages

    async def scenario():
        st = dl_api.queue_state(_=USER)
        assert st["state"] == "stalled" and st["queued"] == 1
        dl_api._WORKER["task"] = None
        r = dl_api.queue_resume(current_user=ADMIN)
        assert r["restarted_worker"] is True and started == [1]
    asyncio.run(scenario())


def test_a_healthy_idle_worker_is_not_called_stalled(monkeypatch):
    _queue(6)
    dl_api._QUEUE_STATE.update(state="idle", beat=dl_api.datetime.now(dl_api.timezone.utc).isoformat())
    assert dl_api.queue_state(_=USER)["state"] == "idle"
