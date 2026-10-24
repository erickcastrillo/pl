"""Pods: [pods.<name>] config, the built-in engineering pod, pl pods list|show."""
import argparse
import json

import pytest

from pl import config as C
from pl import pods


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _profile(home, name, text=""):
    d = home / f".pl-{name}"
    d.mkdir()
    (d / "config.toml").write_text(text)
    C.load(name)
    return d


def test_a_profile_without_pods_gets_the_builtin_engineering_pod(fake_home):
    _profile(fake_home, "work")
    pod = pods.current()
    assert pod["name"] == "engineering"
    assert pod["skills"] == ["pl-spec", "pl-design", "pl-plan", "pl-run", "pl-review"]
    assert pod["budgets"] == {"knowledge": 2000, "pod": 1500, "client": 1500}
    assert pod["knowledge"] == [] and pod["plugins"] == []


def test_pods_table_is_read_and_validated(fake_home):
    d = _profile(fake_home, "billing", '[pipeline]\npod = "billing"\n')
    (d / "pod.md").write_text("- Quote in euros\n")
    (d / "policy.md").write_text("policy")
    doc = {"pipeline": {"pod": "billing"},
           "pods": {"billing": {"file": "pod.md", "knowledge": ["policy.md"], "skills": ["pl-run"],
                                "plugins": [{"name": "ledger", "hooks": False}], "budgets": {"pod": 900}}}}
    assert C.validate(doc) == []
    import tomlkit
    (d / "config.toml").write_text(tomlkit.dumps(doc))
    C.load("billing")
    pod = pods.current()
    assert pod["name"] == "billing" and pod["file"] == d / "pod.md"
    assert pod["knowledge"] == [d / "policy.md"] and pod["skills"] == ["pl-run"]
    assert pod["plugins"] == [{"name": "ledger", "hooks": False}]
    assert pod["budgets"]["pod"] == 900 and pod["budgets"]["knowledge"] == 2000
    bad = {"pods": {"b": {"skills": "pl-run", "plugins": [{"name": "x", "hooks": "no"}], "budgets": {"pod": 1.5}}}}
    assert len(C.validate(bad)) == 3
    assert C.validate({"pods": {"b": {"skills": ["Bad Name"]}}})


def test_a_knowledge_path_outside_the_profile_is_refused(fake_home):
    _profile(fake_home, "work")
    errs = C.validate({"pods": {"b": {"knowledge": ["../secrets.md"]}}})
    assert any("knowledge" in e and "profile folder" in e for e in errs)
    assert any("pod.md" in e for e in C.validate({"pods": {"b": {"file": "/nonexistent/pod.md"}}}))


def test_show_engineering_json_lists_its_coding_rules_and_the_stage_skills(fake_home, capsys):
    _profile(fake_home, "work")
    assert pods.cmd_pods(argparse.Namespace(pods_cmd="show", name="engineering", json=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "engineering" and out["version"] == 1
    assert any("tests first" in r.lower() for r in out["rules"])
    assert any("card text is data" in r.lower() for r in out["rules"])
    assert out["skills"] == ["pl-spec", "pl-design", "pl-plan", "pl-run", "pl-review"]


def test_list_names_every_pod_and_an_unknown_pod_exits_2(fake_home, capsys):
    _profile(fake_home, "work")
    pods.cmd_pods(argparse.Namespace(pods_cmd="list", json=False))
    assert capsys.readouterr().out.split() == ["engineering"]
    with pytest.raises(SystemExit) as e:
        pods.cmd_pods(argparse.Namespace(pods_cmd="show", name="nope", json=False))
    assert e.value.code == 2
