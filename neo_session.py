"""Single shared Kotak Neo session, used by both the market feed and the order desk.

Kotak only accepts order calls from a whitelisted static IP *and* a session created from
that same IP -- so the feed and the orders must run on the same machine/egress IP.
"""
import os
import threading

_client = None
_lock = threading.Lock()


def _check_auth_response(step: str, response: dict) -> None:
    error = response.get("error") or response.get("Error") or response.get("Error Message")
    if not error:
        return
    if isinstance(error, list):
        error = "; ".join(
            str(item.get("message", item)) if isinstance(item, dict) else str(item)
            for item in error
        )
    raise RuntimeError(f"Kotak {step} failed: {error}")


def get_client(force_login: bool = False):
    """Return a logged-in NeoAPI client (login is synchronous; call via asyncio.to_thread)."""
    global _client
    with _lock:
        if _client is None or force_login:
            _client = None
            import pyotp
            from neo_api_client import NeoAPI

            c = NeoAPI(consumer_key=os.environ["NEO_CONSUMER_KEY"], environment="prod")
            login = c.totp_login(
                mobile_number=os.environ["NEO_MOBILE_NUMBER"],
                ucc=os.environ["NEO_UCC"],
                totp=pyotp.TOTP(os.environ["NEO_TOTP_SECRET"]).now(),
            )
            _check_auth_response("TOTP login", login)
            if not c.configuration.view_token or not c.configuration.sid:
                raise RuntimeError("Kotak TOTP login did not provide a view token and session ID")

            validation = c.totp_validate(mpin=os.environ["NEO_MPIN"])
            _check_auth_response("TOTP validation", validation)
            if not c.configuration.edit_token or not c.configuration.edit_sid:
                raise RuntimeError("Kotak TOTP validation did not provide a trade token and session ID")
            _client = c
        return _client
