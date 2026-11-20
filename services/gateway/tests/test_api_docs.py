import re

from fastapi.testclient import TestClient

from gateway_service.main import SECURITY_HEADERS, create_app


def test_swagger_scripts_have_fresh_nonces_and_api_keeps_strict_policy():
    with TestClient(create_app()) as client:
        first = client.get("/api/docs")
        second = client.get("/api/docs")
        assert first.status_code == second.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        nonces = re.findall(r'<script nonce="([^"]+)"', first.text)
        assert len(nonces) == first.text.count("<script") == 2
        assert len(set(nonces)) == 1
        policy = first.headers["content-security-policy"]
        assert f"script-src 'nonce-{nonces[0]}'" in policy
        assert nonces[0] not in second.text
        assert "style-src https://cdn.jsdelivr.net 'unsafe-inline'" in policy
        assert "connect-src 'self'" in policy
        assert "frame-ancestors 'none'" in policy
        assert "url: '/api/openapi.json'" in first.text
        schema = client.get("/api/openapi.json")
        assert schema.status_code == 200
        assert "/api/v1/auth/login" in schema.json()["paths"]
        for response in (schema, client.get("/api/health"), client.get("/api/missing")):
            assert (
                response.headers["content-security-policy"]
                == SECURITY_HEADERS["Content-Security-Policy"]
            )
            assert response.headers["x-frame-options"] == "DENY"
