"""Microservice rule (docs/02 section 3): a service may import only the tables it owns, and
never another service's code. Platform tables are shared by design. Checked from the source,
so a violation fails CI."""

import ast
from pathlib import Path

import pytest

import app.db.models as models
from app.db.session import Base

SERVICES = Path(__file__).resolve().parents[2] / "services"
ALLOWED = {  # service directory -> owners whose tables it may import
    "identity": {"identity", "platform"},
    "catalog": {"catalog", "platform"},
    "supply": {"supply", "platform"},
    "store": {"store", "platform"},
    "sales": {"sales", "platform"},
    "billing": {"billing", "platform"},
    "agent": {"agent", "platform"},
    "gateway": set(),  # the gateway has no database at all
}
CLASS_OWNER = {m.class_.__name__: models.OWNERS[m.local_table.name] for m in Base.registry.mappers}


def imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module, [a.name for a in node.names]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, []


@pytest.mark.parametrize("service", sorted(ALLOWED))
def test_service_imports_only_its_own_tables_and_no_other_service(service):
    allowed = ALLOWED[service]
    files = list((SERVICES / service / f"{service}_service").rglob("*.py"))
    assert files, f"no code found for {service}"
    problems = []
    for file in files:
        for module, names in imports(file):
            top = module.split(".")[0]
            if top.endswith("_service") and top != f"{service}_service":
                problems.append(f"{file.name}: imports another service ({module})")
            if module == "app.db.models":
                problems += [f"{file.name}: {n} is owned by {CLASS_OWNER[n]}" for n in names
                             if n in CLASS_OWNER and CLASS_OWNER[n] not in allowed]  # fmt: skip
            elif module.startswith("app.db.models."):
                owner = module.rsplit(".", 1)[1]
                if owner not in allowed and owner != "_common":
                    problems.append(f"{file.name}: imports {module}")
    assert not problems, problems


def test_every_service_directory_is_covered_by_the_rule():
    on_disk = {p.name for p in SERVICES.iterdir() if (p / "pyproject.toml").exists()}
    assert on_disk <= set(ALLOWED), f"add boundary rules for {sorted(on_disk - set(ALLOWED))}"
