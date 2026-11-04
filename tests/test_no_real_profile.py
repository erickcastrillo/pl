"""Tests never see the real pl profile or worktrees folder, even when run by a pl agent that has them in its env.

2026-10-26: agents run the suite with PL_CONFIG_DIR (and PL_WORKTREES_DIR) set to the live profile, and agents
reported their own worktree folder deleted during a full run; HOME was already temporary, these were not."""
import os

from pl import config as C


def test_the_agent_env_that_points_at_the_real_profile_is_cleared():
    for var in ("PL_CONFIG_DIR", "PL_WORKTREES_DIR", "PL_PROFILE"):
        assert var not in os.environ, var


def test_config_loads_under_the_test_home_not_the_real_one():
    C.load()
    home = os.environ["HOME"]
    for path in (C.CONFIG_DIR, C.WORKTREES_DIR, C.WORK_DIR):
        assert path is None or str(path).startswith(home), path
