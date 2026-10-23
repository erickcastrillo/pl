"""Multipass sandbox VM lifecycle for isolated stage execution."""
import re

from pl import config as C

DEFAULTS = {"enabled": False, "cpus": 2, "memory": "4G", "disk": "20G"}
SIZE_RE = re.compile(r"[1-9][0-9]*[KMG]")


def settings() -> dict:
    """[sandbox] over the defaults."""
    return {**DEFAULTS, **(C.SANDBOX or {})}
