from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.db.session import get_db
from app.main import app

client = TestClient(app)


def test_health_does_not_need_database():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_hides_database_error():
    class UnavailableDatabase:
        def execute(self, query):
            raise OperationalError("SELECT 1", {}, Exception("private-connection-details"))

    app.dependency_overrides[get_db] = lambda: UnavailableDatabase()
    try:
        response = client.get("/api/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "Database unavailable"}
        assert "private-connection-details" not in response.text
    finally:
        app.dependency_overrides.clear()
