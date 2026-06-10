from __future__ import annotations

from pathlib import Path


def test_nginx_routes_api_and_workload_paths() -> None:
    config_path = Path("infra/nginx/nginx.conf")
    config = config_path.read_text(encoding="utf-8")

    # Controller API routes are proxied to controller_api upstream
    assert "location /api/health" in config
    assert "location /api/services" in config
    assert "proxy_pass http://controller_api" in config

    # All other routes go to active workload
    assert "location /" in config
    assert "proxy_pass http://active_workload" in config

    # Verify nginx timeout tuning
    assert "proxy_connect_timeout 5" in config
    assert "proxy_read_timeout 60" in config
    assert "proxy_next_upstream error timeout" in config
