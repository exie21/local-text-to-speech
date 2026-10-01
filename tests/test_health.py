import asyncio

from httpx import ASGITransport, AsyncClient

from app.main import app


def test_health_does_not_require_runtime_files(monkeypatch, tmp_path):
    for name in ("MODEL_DIR", "TEMP_DIR", "DATABASE_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))

    async def request_health():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                return await client.get("/api/health")

    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert list(tmp_path.iterdir()) == []
