"""The one way a command prints --json: a single masked document. SHAPES names each shape in docs/cli-json.md."""
import json

from pl.util import mask

SHAPES = {"list": "board", "card": "card", "section": "section", "alerts": "alerts",
          "usage": "usage", "standup": "standup", "accounts": "accounts", "profiles": "profiles", "intent": "intent"}


def emit(data):
    print(mask(json.dumps(data, ensure_ascii=False, default=str)))
