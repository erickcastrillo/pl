import json
import os

import pytest

from pl import worker_budget


@pytest.fixture(autouse=True)
def machine(tmp_path, monkeypatch):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path))
    return tmp_path


def _max(machine, n):
    (machine / "machine.toml").write_text(f"[tests]\nmax_workers = {n}\n")


def _lease(machine, pid, workers):
    d = machine / "test-workers"
    d.mkdir(exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({"pid": pid, "workers": workers, "at": "now"}))
    return d / f"{pid}.json"


@pytest.mark.parametrize("cores,expected", [(12, 6), (1, 1), (None, 1)])
def test_default_is_half_the_cores_at_least_one(monkeypatch, cores, expected):
    monkeypatch.setattr(worker_budget, "_cpus", lambda: cores)
    assert worker_budget.max_workers() == expected


def test_machine_toml_max_workers_wins(machine, monkeypatch):
    monkeypatch.setattr(worker_budget, "_cpus", lambda: 12)
    _max(machine, 3)
    assert worker_budget.max_workers() == 3


@pytest.mark.parametrize("value", ["0", "-2", '"six"', "true"])
def test_bad_max_workers_uses_the_default(machine, monkeypatch, value):
    monkeypatch.setattr(worker_budget, "_cpus", lambda: 12)
    (machine / "machine.toml").write_text(f"[tests]\nmax_workers = {value}\n")
    assert worker_budget.max_workers() == 6


def test_live_lease_counts_and_dead_lease_is_freed(machine, monkeypatch):
    _max(machine, 8)
    live = _lease(machine, os.getpid(), 3)
    dead = _lease(machine, 999999, 4)
    monkeypatch.setattr(worker_budget, "_alive", lambda pid: pid == os.getpid())
    assert worker_budget.in_use() == 3
    assert live.exists() and not dead.exists()


def test_allowed_is_free_workers_under_the_ceiling(machine):
    _max(machine, 6)
    _lease(machine, os.getpid(), 4)
    assert worker_budget.allowed() == 2
    assert worker_budget.allowed(1) == 1
    _lease(machine, os.getpid(), 6)
    assert worker_budget.allowed() == 1


def test_hold_writes_and_removes_its_lease(machine):
    _max(machine, 6)
    path = machine / "test-workers" / f"{os.getpid()}.json"
    with worker_budget.hold(4):
        assert json.loads(path.read_text())["workers"] == 4
        assert worker_budget.in_use() == 4
    assert not path.exists()


def test_hold_removes_its_lease_when_the_body_raises(machine):
    _max(machine, 6)
    with pytest.raises(RuntimeError):
        with worker_budget.hold(2):
            raise RuntimeError("boom")
    assert worker_budget.in_use() == 0


def test_hold_waits_without_running_while_the_budget_is_full(machine, monkeypatch, capsys):
    _max(machine, 6)
    other = _lease(machine, os.getpid() + 1, 6)
    monkeypatch.setattr(worker_budget, "_alive", lambda pid: True)
    ran = []

    def sleep(_):
        assert not ran
        other.unlink()

    monkeypatch.setattr(worker_budget, "_sleep", sleep)
    with worker_budget.hold(2):
        ran.append(True)
    err = capsys.readouterr().err
    assert err.count("pl tests: waiting for test workers (6/6 in use)") == 1
