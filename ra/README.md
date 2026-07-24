# World Model Database

```bash
# start podman (if not running)
podman machine start

# start postgres (no auth, port 5433)
podman run -d --name world-model-pg \
  -p 127.0.0.1:5433:5432 \
  -e POSTGRES_DB=openshift_cluster \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  docker.io/library/postgres:17

# load schema
podman cp ra/world_model_db_schema.sql world-model-pg:/tmp/schema.sql
podman exec world-model-pg psql -U postgres -d openshift_cluster -f /tmp/schema.sql

# connect
psql -h 127.0.0.1 -p 5433 -U postgres -d openshift_cluster
```
