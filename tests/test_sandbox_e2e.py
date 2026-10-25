"""Opt-in end-to-end test for sandbox egress filtering.

Skipped unless PL_E2E=1.
"""
import os
import pytest

from pl import sandbox

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(os.environ.get("PL_E2E") != "1", reason="set PL_E2E=1 to run")]


def test_a_host_outside_the_allow_list_cannot_be_reached():
    vm = sandbox.vm_name()
    sandbox.up(vm=vm)
    # A host outside the allow list should fail to connect (connection timeout/refused)
    r = sandbox._run([sandbox.MULTIPASS, "exec", vm, "--", "curl", "-s", "--connect-timeout", "2", "http://1.1.1.1"])
    assert r.returncode != 0
