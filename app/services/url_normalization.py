from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote, urlsplit, urlunsplit

from app.data_hub.contracts import ProviderUnavailableError


_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_DNS_LABEL = re.compile(r"^[A-Za-z0-9-]{1,63}$")
_PATH_SAFE = "/:@-._~!$&'()*+,;=%"
_QUERY_SAFE = "/?:@-._~!$&'()*+,;=%"


def _invalid(reason: str) -> ProviderUnavailableError:
    return ProviderUnavailableError(f"invalid announcement URL: {reason}")


def _ascii_host(host: str) -> tuple[str, bool]:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        trailing_dot = host.endswith(".")
        raw_labels = host[:-1].split(".") if trailing_dot else host.split(".")
        if not raw_labels or any(not label for label in raw_labels):
            raise _invalid("host is malformed")
        try:
            labels = [label.encode("idna").decode("ascii").lower() for label in raw_labels]
        except UnicodeError as exc:
            raise _invalid("host cannot be encoded with IDNA") from exc
        if any(
            not _DNS_LABEL.fullmatch(label)
            or label.startswith("-")
            or label.endswith("-")
            for label in labels
        ):
            raise _invalid("host is malformed")
        return ".".join(labels) + ("." if trailing_dot else ""), False
    return address.compressed, address.version == 6


def normalize_announcement_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid("value must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _invalid("control characters are not allowed")
    raw = value.strip()
    if _INVALID_PERCENT_ESCAPE.search(raw):
        raise _invalid("percent escape is malformed")
    try:
        parts = urlsplit(raw)
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise _invalid("authority or port is malformed") from exc
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise _invalid("scheme must be http or https")
    if not parts.netloc or not host:
        raise _invalid("host is required")
    if port is not None and port < 1:
        raise _invalid("port is malformed")
    ascii_host, is_ipv6 = _ascii_host(host)
    authority_host = f"[{ascii_host}]" if is_ipv6 else ascii_host
    userinfo = ""
    if parts.username is not None:
        userinfo = quote(parts.username, safe="%!-._~")
        if parts.password is not None:
            userinfo += ":" + quote(parts.password, safe="%!-._~")
        userinfo += "@"
    netloc = userinfo + authority_host + (f":{port}" if port is not None else "")
    normalized = urlunsplit(
        (
            scheme,
            netloc,
            quote(parts.path, safe=_PATH_SAFE),
            quote(parts.query, safe=_QUERY_SAFE),
            quote(parts.fragment, safe=_QUERY_SAFE),
        )
    )
    try:
        normalized.encode("ascii")
    except UnicodeEncodeError as exc:
        raise _invalid("value cannot be represented as ASCII") from exc
    if len(normalized) > 1000:
        raise _invalid("normalized value exceeds 1000 characters")
    return normalized


__all__ = ["normalize_announcement_url"]
