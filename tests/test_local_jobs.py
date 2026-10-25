"""Checked local jobs: one runner asks Ollama for JSON, validates it, falls back on any failure and logs the outcome."""
import argparse
import json
import subprocess
import urllib.error

import pytest

from pl import config as C
from pl import harnesses, local_jobs, local_model


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text('[models.local]\nhost = "http://localhost:11434"\nroute = "r:1"\n'
                                   'summarise = "s:1"\ntitle = "t:1"\n[accounts.main]\nharness = "claude"\n')
    C.load("t")
    yield
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def ollama(monkeypatch):
    """Every request made; the test sets .reply to the model's JSON answer (a dict) or an exception to raise."""
    class Calls(list):
        reply = {"summary": "All calm."}

    got = Calls()

    def fake(url, payload, timeout):
        got.append((url, payload, timeout))
        if isinstance(got.reply, BaseException):
            raise got.reply
        body = got.reply if isinstance(got.reply, str) else json.dumps(got.reply)
        return json.dumps({"message": {"content": body}})

    monkeypatch.setattr(local_model, "_request", fake)
    return got


def log():
    p = C.STATE_DIR / "local-jobs.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def test_summarise_calls_the_summarise_model_on_the_configured_host(ollama):
    value, meta = local_jobs.run("summarise", "standup text")
    url, payload, _ = ollama[0]
    assert (value, meta["ok"]) == ("All calm.", True)
    assert url == "http://localhost:11434/api/chat" and payload["model"] == "s:1" and payload["format"] == "json"
    assert payload["messages"][1]["content"] == "standup text"


def test_a_schema_miss_uses_the_fallback_and_records_it(ollama):
    ollama.reply = {"summary": 7}
    value, meta = local_jobs.run("summarise", "x")
    assert value is None and meta["note"].startswith("skipped: ")
    assert log()[0]["ok"] is False and log()[0]["fallback"] == "skip" and log()[0]["job"] == "summarise"


def test_a_summary_over_its_limit_is_a_schema_miss(ollama):
    ollama.reply = {"summary": "x" * 601}
    assert local_jobs.run("summarise", "x")[0] is None


def test_bad_json_uses_the_fallback(ollama):
    ollama.reply = "not json"
    assert local_jobs.run("title", "x")[1]["fallback"] == "skip"


@pytest.mark.parametrize("err", [TimeoutError("slow"), urllib.error.URLError(TimeoutError("slow"))])
def test_a_timeout_uses_the_fallback_and_records_it(ollama, err):
    ollama.reply = err
    value, meta = local_jobs.run("summarise", "x")
    assert value is None and meta["why"] == "timeout"
    assert log()[0]["fallback"] == "skip" and log()[0]["why"] == "timeout"


def test_low_route_confidence_leaves_the_item_for_a_person(ollama):
    ollama.reply = {"pipeline": "bugs", "confidence": 0.4}
    value, meta = local_jobs.run("route", "x")
    assert value is None and meta["for_person"] is True and log()[0]["fallback"] == "person"


def test_a_confident_route_is_used(ollama):
    ollama.reply = {"pipeline": "bugs", "confidence": 0.9}
    assert local_jobs.run("route", "x")[0] == "bugs"


def test_ollama_down_runs_the_fallback(ollama):
    ollama.reply = urllib.error.URLError(ConnectionRefusedError())
    value, meta = local_jobs.run("summarise", "x")
    assert value is None and meta["why"] == "ollama down" and log()[0]["fallback"] == "skip"


def test_a_job_without_a_model_is_skipped_without_a_call(ollama):
    C.MODELS = {"local": {}}
    value, meta = local_jobs.run("summarise", "x")
    assert value is None and ollama == [] and "[models.local] summarise" in meta["note"]


def test_a_cloud_fallback_runs_the_account_headless(ollama, monkeypatch):
    C.MODELS = {"local": {**C.MODELS["local"], "fallback": {"summarise": "main"}}}
    ollama.reply = "not json"
    ran = []

    def fake_run(argv, **kw):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"summary": "From the cloud."}', stderr="")

    monkeypatch.setattr(harnesses, "_run", fake_run)
    value, meta = local_jobs.run("summarise", "x")
    assert value == "From the cloud." and ran and log()[0]["fallback"] == "main"


def test_a_failing_cloud_fallback_skips(ollama, monkeypatch):
    C.MODELS = {"local": {**C.MODELS["local"], "fallback": {"summarise": "main"}}}
    ollama.reply = "not json"
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr=""))
    assert local_jobs.run("summarise", "x")[0] is None


def test_the_metrics_log_holds_no_prompt_text(ollama):
    ollama.reply = {"summary": "SECRET ANSWER"}
    local_jobs.run("summarise", "SECRET PROMPT")
    path = C.STATE_DIR / "local-jobs.jsonl"
    assert "SECRET" not in path.read_text() and oct(path.stat().st_mode & 0o777) == "0o600"
    assert set(log()[0]) == {"ts", "job", "model", "ms", "ok", "fallback", "why"}


def test_the_log_drops_lines_older_than_30_days(ollama):
    path = C.STATE_DIR / "local-jobs.jsonl"
    path.write_text(json.dumps({"ts": "2020-01-01T00:00:00+00:00", "job": "title"}) + "\n")
    local_jobs.run("summarise", "x")
    assert [e["job"] for e in log()] == ["summarise"]


def test_card_summary_never_calls_a_cloud_account_when_local_is_set(ollama, monkeypatch):
    monkeypatch.setattr(harnesses, "_run", lambda *a, **k: pytest.fail("called a cloud account"))
    card = {"title": "Fix login", "description": "idea\n\n# PIPELINE: SPEC\nold\n\n# PIPELINE: PLAN\nthe newest plan\n"}
    assert local_jobs.card_summary(card) == "All calm."
    sent = ollama[0][1]["messages"][1]["content"]
    assert sent.startswith("Fix login") and "the newest plan" in sent and "old" not in sent


def embedder_tags_down(calls):
    return calls.tags is None


@pytest.fixture
def embedder(ollama, monkeypatch):
    """Like ollama, for /api/embed and /api/tags: .reply is the embed body, .tags the pulled model names."""
    C.MODELS = {"local": {**C.MODELS["local"], "embed": "e:1"}}
    ollama.tags = ["s:1", "other:1"]
    ollama.reply = {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}

    def fake(url, payload, timeout):
        ollama.append((url, payload, timeout))
        if url.endswith("/api/tags"):
            if embedder_tags_down(ollama):
                raise OSError("down")
            return json.dumps({"models": [{"name": n} for n in ollama.tags]})
        if isinstance(ollama.reply, BaseException):
            raise ollama.reply
        if url.endswith("/api/chat"):
            return json.dumps({"message": {"content": json.dumps(ollama.reply)}})
        return json.dumps(ollama.reply)

    monkeypatch.setattr(local_model, "_request", fake)
    return ollama


def test_embed_returns_one_vector_per_text(embedder):
    assert local_jobs.embed(["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]
    url, payload, _ = embedder[0]
    assert url == "http://localhost:11434/api/embed" and payload["model"] == "e:1" and payload["input"] == ["a", "b"]
    assert log()[0]["job"] == "embed" and log()[0]["ok"] is True


def test_embed_with_ragged_vectors_is_a_schema_miss(embedder):
    embedder.reply = {"embeddings": [[0.1, 0.2], [0.3]]}
    assert local_jobs.embed(["a", "b"]) is None
    assert log()[0]["ok"] is False and log()[0]["fallback"] == "skip"


def test_models_list_json_shows_pulled_models_and_their_jobs(embedder, capsys):
    embedder.tags = ["s:1:latest", "other:1"]
    with pytest.raises(SystemExit) as e:
        local_jobs.cmd_models(argparse.Namespace(models_cmd="list", json=True))
    out = json.loads(capsys.readouterr().out)
    assert e.value.code == 0 and out["host"] == "http://localhost:11434" and out["lan"] is False
    assert out["jobs"]["summarise"] == "s:1"
    by_name = {m["name"]: m for m in out["models"]}
    assert by_name["other:1"]["jobs"] == [] and by_name["other:1"]["pulled"] is True
    assert by_name["s:1"]["jobs"] == ["summarise"] and by_name["s:1"]["pulled"] is True
    assert by_name["r:1"]["jobs"] == ["route"] and by_name["r:1"]["pulled"] is False


def test_models_list_exits_1_when_ollama_does_not_answer(embedder):
    embedder.tags = None
    with pytest.raises(SystemExit) as e:
        local_jobs.cmd_models(argparse.Namespace(models_cmd="list", json=False))
    assert e.value.code == 1


def test_models_test_prints_answer_schema_result_and_time(embedder, capsys):
    embedder.reply = {"pipeline": "bugs", "confidence": 0.9}
    with pytest.raises(SystemExit) as e:
        local_jobs.cmd_models(argparse.Namespace(models_cmd="test", job="route", json=False))
    out = capsys.readouterr().out
    assert e.value.code == 0 and out.count("schema: pass") == 3 and "bugs" in out and " ms" in out
    assert sum(u.endswith("/api/chat") for u, *_ in embedder) == 3


def test_models_test_exits_1_when_the_schema_fails(embedder, capsys):
    embedder.reply = {"pipeline": 7}
    with pytest.raises(SystemExit) as e:
        local_jobs.cmd_models(argparse.Namespace(models_cmd="test", job="route", json=False))
    assert e.value.code == 1 and "schema: fail" in capsys.readouterr().out


def test_models_test_of_an_unknown_job_exits_2(embedder):
    with pytest.raises(SystemExit) as e:
        local_jobs.cmd_models(argparse.Namespace(models_cmd="test", job="dance", json=False))
    assert e.value.code == 2


def test_models_set_writes_the_job(embedder, tmp_path):
    (tmp_path / ".claude-main").mkdir()
    with pytest.raises(SystemExit) as e:
        local_jobs.cmd_models(argparse.Namespace(models_cmd="set", job="title", model="gemma4:e4b", yes=True))
    assert e.value.code == 0
    import tomlkit
    assert tomlkit.parse((C.CONFIG_DIR / "config.toml").read_text())["models"]["local"]["title"] == "gemma4:e4b"


def test_models_set_of_an_unknown_job_exits_2(embedder):
    with pytest.raises(SystemExit) as e:
        local_jobs.cmd_models(argparse.Namespace(models_cmd="set", job="dance", model="x", yes=True))
    assert e.value.code == 2
