from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_returns_something():
    response = client.get("/")
    assert response.status_code < 500


def test_python_still_does_math():
    assert 10 + 5 == 15
