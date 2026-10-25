"""Plan 11 slice 1: machine-wide connectors.toml: validation at config load, and pl connectors list."""
import argparse
import json
import stat

import pytest

from pl import config as C
from pl import connectors


@pytest.fixture(autouse=True)
def machine(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    (tmp_path / "machine").mkdir()
    (tmp_path / ".pl-t").mkdir()
    (tmp_path / ".pl-t" / "config.toml").write_text("")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _connectors(text):
    connectors.path().write_text(text)


def _load_profile():
    C.load("t")


def test_a_literal_token_exits_2_and_asks_for_var_or_keychain(capsys):
    _connectors('[connectors.billing]\nurl = "https://mcp.example.com/mcp"\nauth = "token"\ntoken = "abc"\n')
    with pytest.raises(SystemExit) as e:
        _load_profile()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "connectors.billing.token" in err and "${VAR}" in err and "keychain" in err
    assert "abc" not in err


def test_var_and_keychain_tokens_load():
    _connectors('[connectors.a]\nurl = "https://a.example.com/mcp"\nauth = "token"\ntoken = "${A_TOKEN}"\n'
                '[connectors.b]\ncommand = "b-mcp"\nargs = ["--x"]\nauth = "token"\ntoken = "keychain"\n'
                'teams = ["billing"]\nrole = ["payments"]\n')
    _load_profile()
    assert set(connectors.load()) == {"a", "b"}
    assert connectors.get("b")["access"] == "read"
    assert [n for n, _ in connectors.for_team("billing")] == ["b"]
    assert connectors.for_team("engineering") == []


def test_unknown_role_and_bad_access_name_the_key_path(capsys):
    _connectors('[connectors.crm]\nurl = "https://crm.example.com/mcp"\nrole = ["payments", "weather"]\n'
                'access = "write"\ntier = "official"\n')
    with pytest.raises(SystemExit) as e:
        _load_profile()
    assert e.value.code == 2
    err = capsys.readouterr().err
    for key in ("connectors.crm.role", "connectors.crm.access", "connectors.crm.tier"):
        assert key in err


def test_no_connectors_file_changes_nothing():
    _load_profile()
    assert connectors.load() == {}
    assert connectors.get("billing") is None


def test_a_header_with_a_literal_bearer_is_refused(capsys):
    _connectors('[connectors.billing]\nurl = "https://mcp.example.com/mcp"\n'
                '[connectors.billing.headers]\nAuthorization = "Bearer abc123"\n')
    with pytest.raises(SystemExit) as e:
        _load_profile()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "connectors.billing.headers.Authorization" in err and "abc123" not in err


def test_url_userinfo_one_server_kind_and_bad_names_are_refused():
    errs = connectors.validate({"connectors": {
        "a": {"url": "https://user:pw@h.example.com/mcp"},
        "b": {"url": "https://h.example.com", "command": "x"},
        "c": {},
        "Bad Name": {"url": "https://h.example.com"},
    }})
    text = "\n".join(errs)
    assert "connectors.a.url" in text and "pw" not in text
    assert "connectors.b" in text and "connectors.c" in text and "Bad Name" in text


def test_save_writes_a_0600_file_and_refuses_an_invalid_doc():
    import tomlkit
    doc = tomlkit.parse('[connectors.a]\nurl = "https://a.example.com/mcp"\n')
    connectors.save(doc)
    assert stat.S_IMODE(connectors.path().stat().st_mode) == 0o600
    assert "a" in connectors.load()
    with pytest.raises(SystemExit):
        connectors.save(tomlkit.parse('[connectors.a]\nurl = "https://a.example.com"\nauth = "none"\ntoken = "x"\n'))


def _list(capsys, **kw):
    connectors.cmd_connectors(argparse.Namespace(connectors_cmd="list", json=kw.get("json", False)))
    return capsys.readouterr().out


def test_list_shows_the_tier_and_warns_for_an_adapter(capsys):
    _connectors('[connectors.billing]\nurl = "https://mcp.example.com/mcp?k=1"\nrole = ["payments"]\nteams = ["billing"]\n'
                'access = "read-write"\ntier = "adapter"\n'
                '[connectors.off]\ncommand = "x"\nenabled = false\n')
    out = _list(capsys)
    assert "billing" in out and "payments" in out and "read-write" in out and "adapter" in out
    assert "filled by an adapter, not the vendor's own server" in out
    assert "not signed in" in out and "disabled" in out
    assert "?k=1" not in out


def test_list_json_has_no_secret_values(capsys, monkeypatch):
    monkeypatch.setenv("BILL_TOKEN", "sentinel-secret-value")
    _connectors('[connectors.billing]\nurl = "https://mcp.example.com/mcp"\nauth = "token"\ntoken = "${BILL_TOKEN}"\n'
                '[connectors.billing.headers]\nX-Api-Key = "${BILL_TOKEN}"\n'
                '[connectors.local]\ncommand = "srv"\n[connectors.local.env]\nKEY = "${BILL_TOKEN}"\n')
    out = _list(capsys, json=True)
    assert "sentinel-secret-value" not in out and "BILL_TOKEN" not in out
    rows = {r["name"]: r for r in json.loads(out)["connectors"]}
    assert rows["billing"]["server"] == "https://mcp.example.com"
    assert rows["billing"]["status"] == "connected"
    assert not {"token", "headers", "env"} & set(rows["billing"])
