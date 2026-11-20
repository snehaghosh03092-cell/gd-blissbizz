from sqlalchemy.exc import IntegrityError


def is_unique_violation(error: IntegrityError) -> bool:
    # Isolate driver-specific codes; never parse/return raw SQL or exception messages.
    sqlite_code = getattr(error.orig, "sqlite_errorcode", None)
    if sqlite_code in {1555, 2067}:  # SQLITE_CONSTRAINT_PRIMARYKEY / UNIQUE
        return True
    return bool(error.orig.args and error.orig.args[0] == 1062)  # MySQL duplicate key
