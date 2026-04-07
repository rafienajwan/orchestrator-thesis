from __future__ import annotations

from pathlib import Path


def test_nginx_routes_api_and_workload_paths() -> None:
    config_path = Path("infra/nginx/nginx.conf")
    config = config_path.read_text(encoding="utf-8")

    assert "location /api/" in config
    assert "proxy_pass http://controller:8000;" in config
    assert "location /app/" in config
    assert "location = /" in config
    assert "proxy_pass http://active_workload" in config
