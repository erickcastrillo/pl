"""Home tab: overview of all pipelines."""
from rich.text import Text
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Static

from pl import config as C
from pl.tui.details import DetailsScreen


class HomeView(Widget):
    """Home view listing all pipelines."""

    BINDINGS = [Binding("i", "details", "details")]

    def __init__(self, selected_profile=None):
        super().__init__()
        self.selected_profile = selected_profile

    def compose(self):
        yield Static(Text("Home view", style="dim"), id="home-content")

    def action_details(self):
        profile = self.selected_profile or C.PROFILE_NAME or "default"
        self.app.push_screen(DetailsScreen(profile))
