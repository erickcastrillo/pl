"""Extend > Pod: the current pipeline's pod first, then the skills and connectors on the machine."""
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from pl import config as C
from pl import connectors, pipeline, pods, skills

MISSING_STYLE = "bold #e0a040"   # the header's warning style


def _roles_of(connector):
    role = connector.get("role", [])
    return [role] if isinstance(role, str) else list(role)


def _configured_connectors():
    try:
        return connectors.load()
    except Exception:  # noqa: BLE001 - a bad connectors file must not blank the pod section
        return {}


def pod_rows():
    """(title, [(name, detail, missing)]) for the current pipeline's pod, or the pl error text for a bad pod."""
    try:
        pod = pods.current()
    except SystemExit as e:
        return "Pod", [(str(e), "", True)]
    stages = (C.PIPELINE or {}).get("stages") or pipeline.engineering()["stages"]

    def stage_names(skill):
        return [n for n, s in stages.items() if s.get("skill") == skill]

    rows = [(s, ", ".join(stage_names(s)) or "no stage", False) for s in pod["skills"]]
    outside = dict.fromkeys(s["skill"] for s in stages.values() if s.get("skill") and s["skill"] not in pod["skills"])
    rows += [(skill, f"{', '.join(stage_names(skill))}: missing", True) for skill in outside]
    available = _configured_connectors()
    for role in pod.get("roles", []):
        filling = [n for n, c in available.items() if role in _roles_of(c)]
        rows.append((role, ", ".join(filling) or "missing", not filling))
    return f"Pod: {pod['name']}", rows


def pod_text(rows):
    text = Text()
    for name, detail, missing in rows:
        text.append(f"{name:<24} {detail}\n", style=MISSING_STYLE if missing else "")
    return text


def library_text(items, connector_names):
    text = Text()
    text.append("Skills: ", style="bold")
    text.append(", ".join(sorted({i["name"] for i in items if i["kind"] == "skill"})) or "none")
    text.append("\nConnectors: ", style="bold")
    text.append(", ".join(sorted(connector_names)) or "none")
    return text


class ExtendView(Widget):
    DEFAULT_CSS = """
    ExtendView { height: 1fr; }
    #extend-pod { height: auto; border: round $primary; padding: 0 1; }
    #extend-library { height: auto; padding: 0 1; }
    """

    def compose(self):
        with VerticalScroll(id="extend-scroll"):
            yield Static(id="extend-pod")
            yield Static("Library", classes="heading")
            yield Static(Text("reading…", style="dim"), id="extend-library")

    def on_mount(self):
        title, rows = pod_rows()
        pod = self.query_one("#extend-pod", Static)
        pod.border_title = title
        pod.update(pod_text(rows))
        self.run_worker(self._read_library, thread=True, group="extend-library", exclusive=True)

    def _read_library(self):
        try:
            items = skills.scan()
        except (SystemExit, Exception):  # noqa: BLE001 - a worker that raises kills the app
            items = []
        text = library_text(items, _configured_connectors())
        self.app.call_from_thread(self.query_one("#extend-library", Static).update, text)
