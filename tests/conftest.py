import pytest

from pl import memory


@pytest.fixture(autouse=True)
def no_real_memory_readers(monkeypatch):
    """No test reads the real vm_stat, ps, /proc or tmux, or sends a signal: memory reads as unknown."""
    def no_kill(pid, sig):
        raise AssertionError(f"a test tried to signal pid {pid}")
    monkeypatch.setattr(memory, "_run", lambda argv: None)
    monkeypatch.setattr(memory, "_meminfo", lambda: None)
    monkeypatch.setattr(memory, "_kill", no_kill)
    memory._CACHE.clear()
