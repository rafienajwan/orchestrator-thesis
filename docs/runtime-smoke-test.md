# Runtime Smoke Test (Docker Compose)

This checklist validates runtime behavior that cannot be fully proven by unit tests alone.

## 1. Start stack

```powershell
docker compose up -d --build
docker compose ps
```

Expected:
- redis, controller, agent-1, agent-2, nginx are Up.
- public endpoint available at http://localhost:18080.

## 2. Basic API reachability

```powershell
curl.exe http://localhost:18080/api/
curl.exe http://localhost:18080/api/health
curl.exe http://localhost:18080/api/nodes
```

Expected:
- root endpoint responds (not 404).
- health endpoint returns healthy status.
- nodes list contains worker-1 and worker-2 after telemetry warmup.
- each node reports routable `node_address` (not `unknown`).
- in local compose, `node_address` should be `host.docker.internal` (single-host simulation).

## 3. Deploy sample workload

```powershell
curl.exe -X POST http://localhost:18080/api/services/deploy `
  -H "Content-Type: application/json" `
  -d '{"service":{"service_id":"sample-app","image":"sample-app:latest","command":[],"env":{},"internal_port":8000,"published_port":28000,"health_endpoint":"/health","min_free_cpu":0.1,"min_free_memory":0.1}}'
```

Then inspect:

```powershell
curl.exe http://localhost:18080/api/events
curl.exe http://localhost:18080/api/nodes
```

Expected:
- deploy event recorded.
- service assigned to one node.
- ingress target rendered as `node_address:published_port` (routable from controller).
- in local compose, ingress target should resolve to `host.docker.internal:<published_port>`.
- in VM deployment, ingress target should resolve to `<worker-private-ip>:<published_port>`.
- agent-side health checks use the internal container endpoint, typically `container_ip:internal_port`.

Validate fixed workload endpoint:

```powershell
curl.exe http://localhost:18080/app/health
curl.exe http://localhost:18080/
```

Expected:
- both routes hit active workload.
- endpoint remains the same even after service restart/reschedule.
- do not rely on direct container IP access for primary validation.

Note:
- Docker Compose runtime here is a one-host simulation. Published ports from both workers share the same host network namespace.
- Configure non-overlapping per-agent published-port ranges to avoid collision.

## 4. Restart and reschedule behavior

Restart the service through API (if endpoint exposed by your current routes) or force container restart on owning node and observe events.

Expected:
- service recovers.
- restart counters and events update.
- no cross-node ownership corruption.

## 5. Node failure simulation

Stop one agent:

```powershell
docker compose stop agent-1
```

Wait for heartbeat timeout window, then inspect:

```powershell
curl.exe http://localhost:18080/api/nodes
curl.exe http://localhost:18080/api/events
curl.exe http://localhost:18080/app/health
```

Expected:
- failed node marked unreachable.
- workloads from failed node become pending/rescheduled according to policy.

Bring node back:

```powershell
docker compose start agent-1
```

Expected:
- node becomes healthy again.
- scheduler resumes normal placement.

## 6. Shutdown

```powershell
docker compose down
```

For full cleanup including volumes/images built for this stack:

```powershell
docker compose down --volumes --rmi local
```
