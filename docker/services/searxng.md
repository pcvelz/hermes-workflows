# SearXNG (web search)

**Purpose:** Private metasearch engine the Hermes agent uses for web search
instead of a paid external search API. SearXNG queries multiple upstream
engines simultaneously and returns aggregated, privacy-preserving results.

**Image:** `searxng/searxng:latest` — override via `SEARXNG_IMAGE` in `.env`.

**Port:** Internal port `8080` → published on `127.0.0.1:8888` by default.
Override the host port with `SEARXNG_PORT`.

**Configuration:**

`settings.yml` lives in the `searxng-config` named volume mounted at
`/etc/searxng`. Key settings to review on first run:

- `secret_key` — must be set (pass via `SEARXNG_SECRET`; generate with
  `openssl rand -hex 32`).
- `search.formats` — enable `json` if the agent's search tool expects JSON
  responses (`search.formats: [html, json]` in `settings.yml`).
- Rate-limit settings in `settings.yml` prevent upstream engine bans under
  heavy agent usage.

**Persistence:** `searxng-config` named volume. SearXNG itself is stateless
beyond config; the container can be recreated freely without data loss as long
as the volume is preserved.

**Agent wiring:** Reachable from the agent container as `http://searxng:8080`
via bridge-network DNS. Exposed to the agent via the `SEARXNG_URL` environment
variable.

**Healthcheck:** `GET /healthz` — returns `200` when SearXNG is ready to
serve search requests.

**Notes:**

- SearXNG is stateless; it is safe to delete and recreate the container. Only
  the `searxng-config` volume needs to be preserved.
- If you do not need web search, omit the `searxng` service and remove it from
  the agent's `depends_on` in `docker-compose.yml`.
