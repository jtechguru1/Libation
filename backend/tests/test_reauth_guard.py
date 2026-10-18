"""Re-authenticating must never leave an account signed in as a different Amazon customer, or gone."""

import json

from app.services import reauth_guard as g


def entry(account_id, customer, serial="0" * 20):
    return {"AccountId": account_id, "LibraryScan": True,
            "IdentityTokens": {"AmazonAccountId": customer, "DeviceSerialNumber": serial, "RefreshToken": "secret"}}


def setup(tmp_path, *entries):
    (tmp_path / g.ACCOUNTS).write_text(json.dumps({"Accounts": list(entries), "Other": 1}))
    return tmp_path


def accounts(cfg):
    return {a["AccountId"]: a for a in json.loads((cfg / g.ACCOUNTS).read_text())["Accounts"]}


def replace_entry(cfg, new):
    data = json.loads((cfg / g.ACCOUNTS).read_text())
    data["Accounts"] = [a for a in data["Accounts"] if a["AccountId"] != new["AccountId"]] + [new]
    (cfg / g.ACCOUNTS).write_text(json.dumps(data))


def remove(cfg, account_id):
    data = json.loads((cfg / g.ACCOUNTS).read_text())
    data["Accounts"] = [a for a in data["Accounts"] if a["AccountId"] != account_id]
    (cfg / g.ACCOUNTS).write_text(json.dumps(data))


def test_same_customer_only_when_both_ids_exist_and_match():
    assert g.same_customer(entry("c", "X"), entry("c", "X"))
    assert not g.same_customer(entry("c", "X"), entry("c", "Y"))
    assert not g.same_customer(entry("c", ""), entry("c", ""))
    assert not g.same_customer(entry("c", "X"), None)


def test_restore_puts_the_old_entry_back_replacing_a_wrong_one_and_keeps_the_rest(tmp_path):
    cfg = setup(tmp_path, entry("josh", "J"), entry("christian", "X", "a" * 40))
    g.backup(cfg, "christian", g.find_entry(cfg, "christian"))
    replace_entry(cfg, entry("christian", "Y"))                       # the wrong-customer sign-in landed
    assert g.restore(cfg, "christian") is True
    acc = accounts(cfg)
    assert g.customer_id(acc["christian"]) == "X" and g.customer_id(acc["josh"]) == "J"
    assert json.loads((cfg / g.ACCOUNTS).read_text())["Other"] == 1       # other settings untouched
    assert g.pending(cfg) == {} and not (cfg / g.BACKUP).exists()


def test_a_removed_account_comes_back_on_restore(tmp_path):
    cfg = setup(tmp_path, entry("christian", "X"))
    g.backup(cfg, "christian", g.find_entry(cfg, "christian"))
    remove(cfg, "christian")
    g.restore(cfg, "christian")
    assert g.customer_id(accounts(cfg)["christian"]) == "X"


def test_sessions_map_back_to_their_account(tmp_path):
    cfg = setup(tmp_path, entry("christian", "X"))
    g.backup(cfg, "christian", g.find_entry(cfg, "christian"))
    g.set_session(cfg, "christian", "s-1")
    assert g.account_for_session(cfg, "s-1") == "christian" and g.account_for_session(cfg, "s-2") is None


def test_sweep_restores_an_abandoned_reauth_but_leaves_a_running_one(tmp_path):
    cfg = setup(tmp_path, entry("christian", "X"))
    g.backup(cfg, "christian", g.find_entry(cfg, "christian"), "s-1")
    remove(cfg, "christian")
    assert g.sweep(cfg, live_sessions=["s-1"]) == [] and "christian" not in accounts(cfg)      # still signing in
    assert g.sweep(cfg, live_sessions=[]) == ["christian"] and "christian" in accounts(cfg)   # gone -> put back


def test_sweep_drops_a_stale_backup_when_the_account_is_back(tmp_path):
    cfg = setup(tmp_path, entry("christian", "X"))
    g.backup(cfg, "christian", g.find_entry(cfg, "christian"), "s-1")
    assert g.sweep(cfg, live_sessions=[]) == [] and g.pending(cfg) == {}
    assert g.customer_id(accounts(cfg)["christian"]) == "X"


def test_restore_without_a_backup_changes_nothing(tmp_path):
    cfg = setup(tmp_path, entry("christian", "Y"))
    assert g.restore(cfg, "christian") is False and g.customer_id(accounts(cfg)["christian"]) == "Y"


def test_pending_never_carries_the_tokens(tmp_path):
    cfg = setup(tmp_path, entry("christian", "X"))
    g.backup(cfg, "christian", g.find_entry(cfg, "christian"), "s-1")
    assert "secret" not in json.dumps(g.pending(cfg))


def placeholder(account_id):
    return {"AccountId": account_id, "AccountName": account_id, "DecryptKey": "", "LibraryScan": True, "IdentityTokens": None}


def test_the_placeholder_written_at_sign_in_start_counts_as_not_signed_in(tmp_path):
    cfg = setup(tmp_path, entry("christian", "X"))
    g.backup(cfg, "christian", g.find_entry(cfg, "christian"), "s-1")
    replace_entry(cfg, placeholder("christian"))
    assert g.signed_in(g.find_entry(cfg, "christian")) is False
    assert g.sweep(cfg, live_sessions=[]) == ["christian"] and g.customer_id(accounts(cfg)["christian"]) == "X"
