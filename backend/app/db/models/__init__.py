"""All table models, one module per OWNING service (docs/02 section 3: only the owner writes its
tables).

Importing this package registers every table on ``Base.metadata`` (Alembic needs that) and
installs the
append-only/immutability triggers and the ``rpt_*`` read views for ``create_all``.
Existing imports such as ``from app.db.models import Owner, utc_now`` keep working.
"""

from app.db import triggers, views  # noqa: E402,F401  (register create_all listeners after the models)
from app.db.models._common import new_id, utc_now  # noqa: F401
from app.db.models.agent import *  # noqa: F401,F403
from app.db.models.billing import *  # noqa: F401,F403
from app.db.models.catalog import *  # noqa: F401,F403
from app.db.models.identity import *  # noqa: F401,F403
from app.db.models.notification import *  # noqa: F401,F403
from app.db.models.platform import *  # noqa: F401,F403
from app.db.models.sales import *  # noqa: F401,F403
from app.db.models.store import *  # noqa: F401,F403
from app.db.models.supply import *  # noqa: F401,F403

# table -> owning service, used by the import-boundary test and by scripts/grant_roles.sql
OWNERS: dict[str, str] = {}
for _module in (
    "identity",
    "store",
    "catalog",
    "supply",
    "sales",
    "billing",
    "agent",
    "notification",
    "platform",
):
    _mod = __import__(f"app.db.models.{_module}", fromlist=["Base"])
    for _obj in vars(_mod).values():
        _table = getattr(_obj, "__table__", None)
        if _table is not None and getattr(_obj, "__module__", "") == _mod.__name__:
            OWNERS[_table.name] = _module
