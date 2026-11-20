from urllib.parse import urlsplit


def normalize_origin(value: str, *, allow_path: bool = False) -> str:
    """Return an exact origin, never a suffix or wildcard match."""
    if any(character.isspace() for character in value):
        raise ValueError("Origins must not contain whitespace.")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (not allow_path and (parsed.path not in {"", "/"} or parsed.query))
    ):
        raise ValueError("Use an explicit http(s) origin without credentials or a path.")
    hostname = parsed.hostname.lower()
    if "*" in hostname or parsed.netloc.endswith(":"):
        raise ValueError("Wildcard or incomplete origins are not allowed.")
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    if port is not None and (parsed.scheme, port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    return f"{parsed.scheme}://{host}"
