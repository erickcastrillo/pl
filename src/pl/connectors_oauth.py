"""OAuth and token audit recording for team connectors."""
from pl import audit
from pl import config as C


def record_sign_in(connector: str, host: str | None = None, **kwargs) -> None:
    """Record an OAuth sign-in event with the client host."""
    audit.append("oauth.sign_in", connector=connector, host=host or C.HOST, **kwargs)


def record_token_use(connector: str, host: str | None = None, **kwargs) -> None:
    """Record a connector token usage event with the client host."""
    audit.append("oauth.token", connector=connector, host=host or C.HOST, **kwargs)
