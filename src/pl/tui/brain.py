"""The Brain tab: one machine-wide central brain session."""
from pl import brain
from pl.tui.assistant import AssistantView


class BrainView(AssistantView):
    backend = brain
    prefix = "brain"
    title = "Brain"
    placeholder = "ask the brain; enter sends"
