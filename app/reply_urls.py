from urllib.parse import urlsplit


def origin(url: str) -> tuple[str, str, int]:
    if "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ValueError("Ambiguous URL")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("HTTP(S) URL required")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("Credentials/fragments are not allowed")
    return (
        parsed.scheme,
        parsed.hostname.lower(),
        parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80),
    )


def reply_url_allowed(url: str, allowed_origins: list[str]) -> bool:
    try:
        return origin(url) in {origin(item) for item in allowed_origins}
    except ValueError:
        return False
