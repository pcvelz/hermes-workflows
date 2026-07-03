# Hindsight (semantic memory — bare pgvector substrate)

> **POC STATUS:** This container runs a plain `pgvector/pgvector` Postgres
> database as the storage substrate for the proposed Hindsight memory layer.
> There is **no ingest API wired** in this scaffold: the cron job that would
> sync a knowledge vault into Hindsight is a dry-run stub. Semantic retrieval
> against this store is **proposed design, not yet implemented**.
> If an official upstream Hindsight Docker image becomes available, set
> `HINDSIGHT_IMAGE` to that tag.

**Purpose (design target):** Long-term semantic memory store for the Hermes
agent. In the intended architecture an external knowledge vault (e.g. an
Obsidian vault) would be synced into Hindsight and the agent would query it for
retrieval-augmented recall — surfacing relevant notes, decisions, and past
session context without re-reading the entire knowledge base.

This corresponds to the outer tier of Hermes's three-layer memory architecture:
`MEMORY.md` (active session) → structured notes → **Hindsight** (vector search
over the full corpus). See [docs/memory.md](../../docs/memory.md) for the full
architecture.

**Image:** `pgvector/pgvector:pg16` — override via `HINDSIGHT_IMAGE`.

**Port:** Internal Postgres port `5432` → published on `127.0.0.1:8889` by
default. Override the host port with `HINDSIGHT_PORT`.

**Configuration:**

Set via environment variables (`HINDSIGHT_DB_USER`, `HINDSIGHT_DB_PASSWORD`,
`HINDSIGHT_DB_NAME`). Change the default password before any deployment.

Enable the pgvector extension once after the database first initializes:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Run this against the `hindsight` database (`psql -U hindsight -d hindsight`
inside the container or via `docker compose exec hindsight psql ...`).

**Persistence:** `hindsight-data` named volume at `/var/lib/postgresql/data`.
Back up with `pg_dump` before any upgrade.

**Agent wiring:** `http://hindsight:8889` from the agent container (bridge-network
DNS by service name). Inside the bridge network, the database is reachable at
host `hindsight`, port `5432`. Exposed to the agent via the `HINDSIGHT_URL`
environment variable.

**Healthcheck:** `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB` — ensures
Postgres is accepting connections before the agent depends on it.

**Backup:**

Back up with `pg_dump` for full portability:

```bash
docker compose exec hindsight pg_dump -U hindsight hindsight > hindsight-backup.sql
```

Restore with:

```bash
docker compose exec -T hindsight psql -U hindsight hindsight < hindsight-backup.sql
```

**Notes:**

- If you do not need semantic memory, omit the `hindsight` service and remove
  it from the agent's `depends_on` in `docker-compose.yml`.
- The cron job that would sync a vault to Hindsight is described in
  [cron/README.md](../../cron/README.md) — it is currently a stub.
