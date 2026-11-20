import re


class SlugPolicy:
    """Shared hostname rules for availability checks and future registration."""

    RESERVED = frozenset(
        {
            "admin",
            "api",
            "assets",
            "auth",
            "cdn",
            "ftp",
            "help",
            "imap",
            "localhost",
            "mail",
            "media",
            "ns1",
            "ns2",
            "pop",
            "smtp",
            "static",
            "status",
            "support",
            "www",
        }
    )
    PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

    @classmethod
    def normalize(cls, value: str) -> str:
        if not isinstance(value, str) or not value.isascii():
            raise ValueError("Use ASCII letters, numbers, and single hyphens.")
        slug = value.strip().lower()
        if not 1 <= len(slug) <= 63 or cls.PATTERN.fullmatch(slug) is None:
            raise ValueError(
                "Use 1–63 letters or numbers, separated by single hyphens; "
                "start and end with a letter or number."
            )
        return slug

    @classmethod
    def is_reserved(cls, slug: str) -> bool:
        return slug in cls.RESERVED
