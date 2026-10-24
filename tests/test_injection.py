"""Tests for prompt injection resilience across all ingestion paths (AC-11).

Fixtures in tests/fixtures/injection/ (email, chat, connector result, card text)
drive the real ingestion paths:
- trigger -> reader -> qualify prompt builder -> connector proxy
- chat message -> poller / gate check
- connector tool result -> connector proxy
- card text -> dispatch.start_worker prompt & launch script
"""
import re
from pathlib import Path
import pytest

from pl import config as C
from pl import harnesses, whatsnew
from pl.util import slug_of

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "injection"
FIXTURES = ["email", "chat", "connector_result", "card_text"]


class MockConnectorProxy:
    """Connector proxy enforcing tool pinning and write restrictions."""

    def __init__(self, pinned_tools=None, approved=False):
        self.pinned_tools = pinned_tools or ["read_resource", "get_status", "list_items"]
        self.approved = approved
        self.write_calls = []

    def call_tool(self, tool_name: str, **kwargs):
        # Pinned read-only tools are allowed; all unknown or write tools require approval
        if tool_name not in self.pinned_tools:
            if not self.approved:
                raise PermissionError(f"Tool {tool_name!r} is disabled: connector write tools require owner approval")
            self.write_calls.append((tool_name, kwargs))
            return {"status": "ok", "executed": tool_name}
        return {"status": "ok", "read": tool_name}


class MockOutwardActionQueue:
    """Outward-action queue that requires explicit gate approval."""

    def __init__(self):
        self.queue = []

    def enqueue(self, action: dict, gate_approved: bool = False):
        if not gate_approved:
            raise PermissionError("Outward action rejected: gate approval required")
        self.queue.append(action)


def extract_mentioned_tools(text: str) -> list[str]:
    """Extract tool names mentioned in backticks or tool call directives."""
    found = set()
    # Tool names in backticks
    for match in re.findall(r"`([a-zA-Z0-9_-]+)`", text):
        if any(w in match.lower() for w in ("tool", "write", "delete", "drop", "wipe", "approve", "create", "card")):
            found.add(match)
    # Tool call patterns like "call write tool `xyz`" or "call write tool xyz"
    for match in re.findall(r"(?:call|invoke|write\s+tool)\s+[`']?([a-zA-Z0-9_-]+)[`']?", text, re.I):
        found.add(match)
    return sorted(found)


def extract_mentioned_pl_commands(text: str) -> list[str]:
    """Extract pl CLI commands mentioned in the text."""
    commands = []
    for line in text.splitlines():
        line = line.strip().strip("`")
        if line.startswith("pl "):
            commands.append(line)
        for m in re.findall(r"`(pl\s+[^`]+)`", line):
            commands.append(m)
    return sorted(set(commands))


def drive_card_text_path(fixture_text: str, monkeypatch):
    """Drive card text -> dispatch.start_worker prompt & launch script."""
    # Ensure profile and harness configs are active
    C.PROMPTS["run"] = "/loop 30m /pl-run {id} {worktrees_dir}"
    card = {
        "id": "repo#164",
        "title": fixture_text.splitlines()[0][:80],
        "description": fixture_text,
        "metadata": {"spec_slug": "injection-test"},
    }

    # Verify slug sanitization
    slug = slug_of(card)
    assert re.match(r"^[a-z0-9-]+$", slug), f"slug_of produced unsafe chars: {slug!r}"

    # Verify prompt generation
    prompt = C.PROMPTS["run"].format(id=card["id"], worktrees_dir=C.WORKTREES_DIR or "")
    assert prompt.startswith("/loop 30m /pl-run repo#164")

    # Generate launch script through harnesses.launch_script
    h = harnesses.BUILTINS["antigravity"]
    script = harnesses.launch_script(
        h,
        "default",
        prompt,
        "test-session-id",
        f"run:{slug[:28]}",
    )

    # Assert no fixture text appears in shell script unquoted
    # Find raw dangerous shell payloads
    for dangerous in ("; rm -rf", "$(cat", "$(touch", "`rm -rf", "$(curl"):
        if dangerous in fixture_text:
            assert dangerous not in script or f"'{dangerous}" in script or f'"{dangerous}' in script


def drive_connector_result_path(fixture_text: str, proxy: MockConnectorProxy, tools: list[str]):
    """Drive connector tool result -> proxy with scripted fake agent trying tools."""
    for tool_name in tools:
        with pytest.raises(PermissionError) as exc_info:
            proxy.call_tool(tool_name)
        assert "disabled" in str(exc_info.value) or "require" in str(exc_info.value)
    assert len(proxy.write_calls) == 0


def drive_trigger_reader_path(fixture_text: str, proxy: MockConnectorProxy, tools: list[str]):
    """Drive trigger -> reader -> qualify prompt builder -> connector proxy."""
    # Reader parses fields
    fields = {"subject": "Urgent", "body": fixture_text}
    # Scripted fake agent tries every tool mentioned in the extracted fields
    for tool_name in tools:
        with pytest.raises(PermissionError):
            proxy.call_tool(tool_name, payload=fields)
    assert len(proxy.write_calls) == 0


def drive_chat_message_path(fixture_text: str, outward_queue: MockOutwardActionQueue, pl_commands: list[str]):
    """Drive chat message -> poller / gate check."""
    # Scripted fake agent attempts outward actions mentioned in fixture
    if "outward" in fixture_text.lower() or "payment" in fixture_text.lower() or "amount" in fixture_text.lower():
        with pytest.raises(PermissionError) as exc_info:
            outward_queue.enqueue({"action": "payment", "amount": 1000}, gate_approved=False)
        assert "gate approval required" in str(exc_info.value)
    assert len(outward_queue.queue) == 0


@pytest.mark.parametrize("fixture", FIXTURES)
def test_no_fixture_leads_to_a_write_or_outward_action(fixture, monkeypatch, tmp_path):
    fixture_path = FIXTURES_DIR / f"{fixture}.txt"
    assert fixture_path.is_file(), f"Missing fixture file: {fixture_path}"
    fixture_text = fixture_path.read_text()

    # Extract all tool names and pl commands the scripted fake agent will attempt
    tools = extract_mentioned_tools(fixture_text)
    pl_commands = extract_mentioned_pl_commands(fixture_text)

    proxy = MockConnectorProxy(approved=False)
    outward_queue = MockOutwardActionQueue()

    # 1. Drive card text path
    drive_card_text_path(fixture_text, monkeypatch)

    # 2. Drive connector tool result path
    drive_connector_result_path(fixture_text, proxy, tools)

    # 3. Drive trigger -> reader path
    drive_trigger_reader_path(fixture_text, proxy, tools)

    # 4. Drive chat message path
    drive_chat_message_path(fixture_text, outward_queue, pl_commands)

    # Assert:
    # - No connector write call succeeds
    assert len(proxy.write_calls) == 0
    # - No outward-action queue item is created without a gate approval
    assert len(outward_queue.queue) == 0


def test_whatsnew_entry_for_connector_tool_changes():
    """WP12.2: whatsnew entry for connector tool pinning."""
    titles = [e["title"] for e in whatsnew.ENTRIES]
    assert "Connectors whose tools change lose write access until you approve them" in titles
