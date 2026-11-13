"""Origin checks and a private, non-redirecting authenticated HTTP transport.

No opener is installed globally. TLS uses urllib's default verification; this
module does not log URLs, headers, response bodies or credentials.
"""
from __future__ import annotations

from ipaddress import ip_address
import re
from urllib import request as urllib_request
from urllib.error import HTTPError
from urllib.parse import urlsplit

__all__ = ["origin", "open_llm", "NoRedirectHandler"]


def origin(url: str) -> tuple[str, str, int]:
    """Return the HTTP origin, rejecting userinfo and malformed authorities.

    Paths, queries and fragments are not part of an origin. Explicit default
    ports compare equal to implicit defaults; a scheme or port change does not.
    """
    if not isinstance(url, str) or not url or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("LLM endpoint must be an HTTP(S) URL without whitespace or controls")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise ValueError("LLM endpoint must have an HTTP(S) origin")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("LLM endpoint must not contain userinfo credentials")
        hostname = parsed.hostname
        if any(char in hostname for char in "\\/%"):
            raise ValueError("Invalid LLM endpoint hostname")
        try:
            hostname = ip_address(hostname).compressed.lower()
        except ValueError:
            hostname = hostname.encode("idna").decode("ascii").lower()
            if not re.fullmatch(r"[a-z0-9._-]+", hostname):
                raise ValueError("Invalid LLM endpoint hostname")
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        if not 1 <= port <= 65535:
            raise ValueError("Invalid LLM endpoint port")
    except (UnicodeError, ValueError) as exc:
        # Keep even validation exceptions free of the supplied endpoint text.
        raise ValueError("Invalid LLM endpoint origin") from None
    return parsed.scheme, hostname, port


class NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    """Never forward an authenticated LLM request, including within its origin."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "LLM redirects are disabled", headers, fp)

    def http_error_302(self, req, fp, code, msg, headers):
        # Reject before parsing/visiting Location, including absent or malformed
        # Location headers. Other non-success codes use urllib's default errors.
        raise HTTPError(req.full_url, code, "LLM redirects are disabled", headers, fp)

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


def open_llm(request: urllib_request.Request, timeout: float):
    """Open only the original validated endpoint with default TLS verification."""
    origin(request.full_url)
    opener = urllib_request.build_opener(NoRedirectHandler())
    return opener.open(request, timeout=timeout)
