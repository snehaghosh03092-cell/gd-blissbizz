"""Release tooling checks. Run with backend/.venv/bin/python3 -m pytest scripts/test_release.py."""

import importlib.util
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_init_is_private_unique_and_refuses_to_overwrite(tmp_path):
    root = tmp_path / "release"
    (root / "deploy").mkdir(parents=True)
    tls = tmp_path / "certs"
    tls.mkdir()
    for name in ("fullchain.pem", "privkey.pem"):
        (tls / name).write_text("test only")
    init = load("release-init")
    result = init.initialize(root, tls, "test-01")
    assert stat.S_IMODE(result.stat().st_mode) == 0o600
    data = dict(line.split("=", 1) for line in result.read_text().splitlines())
    assert (
        len(
            {
                data["MYSQL_PASSWORD"],
                data["MYSQL_ROOT_PASSWORD"],
                data["INTERNAL_SIGNING_SECRET"],
            }
        )
        == 3
    )
    assert data["BIND_ADDRESS"] == "127.0.0.1"
    assert data["ANTHROPIC_API_KEY"] == ""
    with pytest.raises(FileExistsError):
        init.initialize(root, tls, "test-02")
    assert "RELEASE_TAG=test-01" in result.read_text()


def test_init_requires_real_certificate_files_before_writing(tmp_path):
    (tmp_path / "deploy").mkdir()
    init = load("release-init")
    with pytest.raises(ValueError, match="certificate"):
        init.initialize(tmp_path, tmp_path, "test-01")
    assert not (tmp_path / "deploy/release.env").exists()


def test_bundle_contains_source_and_no_secrets_or_runtimes(tmp_path, monkeypatch):
    bundle = load("release-bundle")
    monkeypatch.setattr(bundle, "ROOT", tmp_path)
    for name in (
        "README.md",
        "Makefile",
        ".dockerignore",
        ".gitignore",
        "frontend/src/main.js",
        "backend/.env.example",
        "backend/app/main.py",
    ):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("source")
    for name in (
        "backend/.env",
        "backend/.env.backup",
        "deploy/release.env",
        "deploy/demo-private/credentials.json",
        "deploy/tls/privkey.pem",
        "frontend/node_modules/private/token.txt",
        "backend/.venv/bin/python3",
        "backend/app/__pycache__/a.pyc",
    ):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("SECRET-SENTINEL")
    bundle.create_bundle()
    target = tmp_path / "var/releases/blissbiz-next-20261120-platform-01.tar.gz"
    with tarfile.open(target) as archive:
        names = archive.getnames()
        assert "blissbiz-next/frontend/src/main.js" in names
        assert "blissbiz-next/backend/.env.example" in names
        assert "blissbiz-next/RELEASE-MANIFEST.json" in names
        for item in archive.getmembers():
            assert not item.issym() and not item.islnk()
            assert b"SECRET-SENTINEL" not in archive.extractfile(item).read()
    original = target.read_bytes()
    with pytest.raises(FileExistsError):
        bundle.create_bundle()
    assert target.read_bytes() == original
    bundle.create_bundle("test-update-02")
    with tarfile.open(
        tmp_path / "var/releases/blissbiz-next-test-update-02.tar.gz"
    ) as archive:
        manifest = json.load(archive.extractfile("blissbiz-next/RELEASE-MANIFEST.json"))
        assert manifest["release"] == "test-update-02"
    assert target.read_bytes() == original
    with pytest.raises(ValueError, match="Invalid release"):
        bundle.create_bundle("../../unsafe")


def test_compose_defaults_keep_legacy_volumes_and_public_ports_untouched(tmp_path):
    envfile = tmp_path / "release.env"
    envfile.write_text(
        "RELEASE_TAG=test-01\nMYSQL_PASSWORD=test-only-password\n"
        "MYSQL_ROOT_PASSWORD=test-only-root-password\n"
        "INTERNAL_SIGNING_SECRET=" + "t" * 64 + "\nTLS_DIR=/nonexistent/test-only-tls\n"
    )
    # Override host environment so an unrelated local project cannot affect this check.
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "BIND_ADDRESS",
            "HTTP_PORT",
            "HTTPS_PORT",
            "ANTHROPIC_API_KEY",
            "CHAT_MODEL",
            "GUIDE_MODEL",
            "COMPOSE_PROFILES",
            "RELEASE_TAG",
            "MYSQL_PASSWORD",
            "MYSQL_ROOT_PASSWORD",
            "INTERNAL_SIGNING_SECRET",
            "TLS_DIR",
        }
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            "blissbiz-next",
            "--profile",
            "tools",
            "--env-file",
            str(envfile),
            "-f",
            str(ROOT / "deploy/compose.release.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    data = json.loads(result.stdout)
    services = data["services"]
    assert set(services) == {
        "db",
        "migrate",
        "gateway",
        "identity",
        "store",
        "catalog",
        "supply",
        "sales",
        "billing",
        "agent",
        "proxy",
    }
    for name, service in services.items():
        if name == "proxy":
            assert {str(p["published"]) for p in service["ports"]} == {"18080", "18443"}
            assert all(p["host_ip"] == "127.0.0.1" for p in service["ports"])
        else:
            assert not service.get("ports")
    assert all(v["name"].startswith("blissbiz-next_") for v in data["volumes"].values())
    assert services["store"]["environment"]["BASE_DOMAIN"] == "blissbiz.biz"
    assert services["gateway"]["environment"]["APP_ENV"] == "production"
    assert services["sales"]["environment"]["SWEEP_INTERVAL_SECONDS"] == "0"
    assert services["agent"]["environment"]["CHAT_MODEL"] == "claude-haiku-5-5"
    assert services["agent"]["environment"]["GUIDE_MODEL"] == "claude-sonnet-5-5"
    assert all(
        "ANTHROPIC_API_KEY" not in service.get("environment", {})
        for name, service in services.items()
        if name != "agent"
    )
    assert services["gateway"]["environment"]["AGENT_URL"] == "http://agent:8000"
