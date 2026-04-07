# Mini Orchestrator Thesis Prototype

Focused academic prototype of a small Docker orchestrator for real-time services.

Current scope:
- 1 controller with FastAPI, scheduler, self-healing, and Redis-backed state
- 2 worker agents with FastAPI and Docker SDK
- deterministic least-load scheduling
- explicit handling for container failure and node unreachable

## Quickstart

Use Python 3.12 for local development.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .[dev]
ruff check .
black --check .
mypy .
pytest -q
```

## Run Controller

Start Redis first, then run controller:

```powershell
docker run --rm -p 6379:6379 redis:7-alpine
python -m uvicorn controller.main:app --host 0.0.0.0 --port 8000 --reload
```

Controller->agent HTTP timeouts are configurable via environment variables:
- `AGENT_DEPLOY_TIMEOUT_SECONDS` for deploy requests inside the controller container
- `AGENT_COMMAND_TIMEOUT_SECONDS` for stop/restart requests inside the controller container
- `AGENT_READ_TIMEOUT_SECONDS` for lighter controller reads if added later inside the controller container
- The `CONTROLLER_AGENT_*` names in [.env.example](.env.example) are compose helper variables that map into those container env vars

## Run Agent

Start an agent node with explicit identity:

```powershell
$env:NODE_ID = "worker-1"
$env:AGENT_PUBLIC_URL = "http://agent-1:8080"
$env:CONTROLLER_BASE_URL = "http://localhost:8000"
python -m uvicorn agent.app.main:app --host 0.0.0.0 --port 8101 --reload
```

## Run Redis

```powershell
docker run --rm -p 6379:6379 redis:7-alpine
```

## Run Compose

```powershell
docker compose up -d
docker compose ps
```

Default public entry point is `http://localhost:18080`.

If host port 18080 is also occupied, set `NGINX_HOST_PORT` in `.env`.

## Deploy Service

```powershell
curl.exe -X POST http://localhost:18080/api/services/deploy `
  -H "Content-Type: application/json" `
   -d '{"service":{"service_id":"sample-app","image":"sample-app:latest","command":[],"env":{},"internal_port":8000,"published_port":28000,"health_endpoint":"/health","min_free_cpu":0.1,"min_free_memory":0.1}}'
```

## Check Nodes

```powershell
curl.exe http://localhost:18080/api/nodes
```

## Check Events

```powershell
curl.exe http://localhost:18080/api/events
```

## Accessing Deployed Services

Control-plane and workload ingress are now separated:
- `http://localhost:18080/api/*` routes to controller API
- `http://localhost:18080/app/*` routes to active workload
- `http://localhost:18080/` also routes to active workload root

Client or k6 should always use the same workload endpoint:
1. Access root endpoint:
   ```powershell
   curl.exe http://localhost:18080/app/health
   ```

2. Control operations stay on `/api`:
   ```powershell
   curl.exe http://localhost:18080/api/services/sample-app
   curl.exe http://localhost:18080/api/nodes
   curl.exe http://localhost:18080/api/events
   ```

3. Direct container IP access is debug-only (not main experiment path):
   ```bash
   # Debug only
   curl http://<container_ip>:<internal_port>/health
   ```

### Routable Endpoint Model (VM-ready)

Ingress target is rendered as `node_address:published_port`:
- `node_address` comes from agent `ADVERTISED_HOST`
- `published_port` is host port published on worker VM

For local compose:
- `ADVERTISED_HOST` defaults to service names (`agent-1`, `agent-2`)
- this is routable from controller/nginx because they share the same compose network
- avoid `localhost` or container IP here; those are not stable cross-container ingress targets

For VM deployment:
- set `ADVERTISED_HOST` to each worker private IP (for example `10.x.x.x`)
- ensure worker private IP is reachable from controller/nginx and published port range is allowed
- keep client URL fixed (`/app/*`), ingress target changes internally during reschedule

### Published Port Collision Behavior

- If deploy requests a `published_port` already in use on the worker host, agent deploy returns HTTP `409`.
- Controller keeps service in pending/failed path according to existing reconciliation flow until a valid routable endpoint is available.
- Fix by choosing another `published_port` or widening `PUBLISHED_PORT_BASE`..`PUBLISHED_PORT_MAX` range.

## Controller→Agent Timeout Configuration

Timeouts are explicitly separated by operation type:
- `AGENT_DEPLOY_TIMEOUT_SECONDS` (default 60s): Image pull + startup time
- `AGENT_COMMAND_TIMEOUT_SECONDS` (default 15s): Stop/restart commands (faster)
- `AGENT_READ_TIMEOUT_SECONDS` (default 5s): Lightweight controller reads (reserved for future)

### Node Failure Detection Latency

Node unreachable detection relies on heartbeat timeout + reconciliation grace window:
- Heartbeat timeout: 30 seconds (node must send heartbeat every 5 seconds)
- Reconciliation grace window: +15 seconds margin for network variance
- **Total detection latency: ~45 seconds**

This is acceptable for academic prototype. Production deployments should tune based on SLA:
- Tighter SLA: reduce `heartbeat_timeout_seconds` to 10-20s
- Loose SLA: current 30s sufficient

## Test-Only Endpoints

Sample app includes POST endpoints for smoke test health failure injection:
- `POST /health/fail`: Simulate health check failure
- `POST /health/recover`: Restore healthy state

These are NOT part of production workload contract. Do not include in final deployment images.

## Final Archive (Clean Zip)

Generate a clean project archive that excludes local cache and virtual environment artifacts:

```powershell
.\scripts\create_clean_zip.ps1
```

For release preparation details, see [docs/release-checklist.md](docs/release-checklist.md).

## Runtime Smoke Test

Use the Docker Compose runtime checklist in [docs/runtime-smoke-test.md](docs/runtime-smoke-test.md) to validate deploy, restart/reschedule, and node failure simulation.

## Notes

- Source packages are top-level: `controller/` and `agent/`.
- Main runtime wiring lives in `docker-compose.yml`.
- Nginx routing config lives in `infra/nginx/nginx.conf`.
