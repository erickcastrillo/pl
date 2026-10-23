"""The Brain tab: central brain session and memory."""
import re
from textual.binding import Binding
from pl import brain
from pl.tui.assistant import AssistantView


class BrainView(AssistantView):
    backend = brain
    BINDINGS = [*AssistantView.BINDINGS, Binding("u", "undo", "undo saved")]

    def action_restart(self):
        conv = getattr(self, "conversation_id", None) or "brain"
        res = brain.end(conv)
        m = re.search(r"saved\s+(\d+)", res, re.I)
        count = int(m.group(1)) if m else 0
        self.last_ended_conv = conv
        app = getattr(self, "_app", None)
        try:
            app = app or self.app
        except Exception:
            pass
        if count > 0 and app:
            app.notify(f"saved {count} · u undo", markup=False)
        try:
            super().action_restart()
        except Exception:
            pass

    def action_undo(self):
        conv = getattr(self, "last_ended_conv", None) or getattr(self, "conversation_id", None) or "brain"
        brain.undo(conv)
        app = getattr(self, "_app", None)
        try:
            app = app or self.app
        except Exception:
            pass
        if app:
            app.notify("undone", markup=False)
