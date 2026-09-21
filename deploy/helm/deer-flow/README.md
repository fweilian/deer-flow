# DeerFlow Helm Chart

Deploy DeerFlow's gateway, frontend, nginx, provisioner, and optional Redis
stream bridge to Kubernetes. The chart does not deploy a relational database.

## Prerequisites

- Kubernetes with Helm 3.8+ and `kubectl`.
- Gateway, frontend, and provisioner images in a registry the cluster can read.
- An externally managed MySQL 8.0.24+ service.
- The frozen `0001_mysql_baseline` and checkpoint migration artifact applied by
  a DBA before the Gateway starts.

The Gateway validates the expected MySQL revisions at startup and performs no
DDL. A mismatch fails startup. SQLite is for local development and tests only;
this chart has no in-place migration path from legacy deployments.
PostgreSQL is not a supported chart persistence backend.

## Install

Create `my-values.yaml` with the image location and external database DSN:

```yaml
image:
  registry: ghcr.io/yourorg
  tag: latest

secrets:
  OPENAI_API_KEY: sk-...
  MYSQL_DATABASE_URL: mysql+asyncmy://user:password@mysql.example.com:3306/deerflow

config: |
  config_version: 45
  database:
    backend: mysql
    mysql_url: $MYSQL_DATABASE_URL
  sandbox:
    use: deerflow.community.aio_sandbox:AioSandboxProvider
    provisioner_url: http://provisioner:8002
```

Use `existingSecret` instead of `secrets` when the database credential is
already managed by the cluster. Keep credential values in a Secret; reference
them from `config` only as `$VAR` values.

Install from a local checkout:

```bash
helm upgrade --install deer-flow deploy/helm/deer-flow \
  --namespace deer-flow --create-namespace \
  -f my-values.yaml
```

Verify the deployment:

```bash
kubectl -n deer-flow get pods
kubectl -n deer-flow port-forward svc/nginx 2026:2026
curl http://localhost:2026/health
```

## Operational notes

- The default Redis StatefulSet supports cross-pod SSE. Set `redis.enabled:
  false` and configure `redis.external` for managed Redis.
- `scheduler.multi_instance: true` requires shared MySQL,
  `run_ownership.heartbeat_enabled: true`, and `run_events.backend: db`.
- Gateway replica count remains `1` by default. Persisted state may be shared,
  but run-control ownership remains a separate operational constraint.
- The home PVC stores runtime state such as SQLite development data, memory,
  user data, and writable extension configuration. It is not a MySQL data
  volume.
