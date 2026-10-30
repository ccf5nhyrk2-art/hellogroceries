# HelloGroceries

A self-hosted grocery planner and unofficial HelloFresh companion. Browse recipes, select meals, and manage a shopping checklist, with optional US recipe metadata importing.

> **Unofficial project.** HelloGroceries is not affiliated with, endorsed by, or sponsored by HelloFresh. This repository does not include a copied recipe catalog or recipe photos.

## Multi-household roadmap (not enabled yet)

The `app/households.py` module defines an **additive** SQLite schema for per-installation
users, households, household memberships, household state, invitation records and sessions.
It is a foundation only: the current HTTP server still uses its original single-household
state and **does not enforce authentication or household isolation**. Do not expose an
unprotected installation to the public internet.

Each independent installation will have its **own** first-run instance administrator.
No developer email, domain, household name or Cloudflare account is hardcoded. The
administrator should be created through a one-time setup flow secured by a locally
generated bootstrap secret. Only an administrator can issue invitations; invitations
will be single-use, expiring and stored as token hashes. Household ownership is
separate from instance administration. Existing state must be migrated into the
first household with a verified backup and rollback path.

Cloudflare Tunnel and Cloudflare Access are optional deployment choices, not required
dependencies. Until the account APIs, sessions, CSRF defenses, authorization checks
and migration tests are complete, restrict access to trusted users.

## Quick start: prebuilt Docker image

Requirements: Docker Engine and Docker Compose v2. The public image is published automatically to GitHub Container Registry when changes are pushed to `main`.

```bash
git clone https://github.com/ccf5nhyrk2-art/hellogroceries.git
cd hellogroceries
cp .env.example .env
docker compose pull hellogroceries
docker compose up -d hellogroceries
```

Open `http://YOUR-SERVER-IP:8088` on your **trusted local network**. To change the host port, edit `HOST_PORT` in `.env`. Your database and import progress persist in `./data`. The starter catalog is empty.

### OpenMediaVault deployment

In **Services → Compose → Files**, create or edit a stack using the following YAML. This preserves the existing HelloGroceries database at `/opt/hellofresh-grocery/data` and uses port 8088:

```yaml
services:
  hellogroceries:
    image: ghcr.io/ccf5nhyrk2-art/hellogroceries:latest
    container_name: hellogroceries
    restart: unless-stopped
    ports:
      - "8088:8080"
    volumes:
      - /opt/hellofresh-grocery/data:/data

  us-importer:
    image: ghcr.io/ccf5nhyrk2-art/hellogroceries:latest
    profiles: ["us-import"]
    command: ["python", "-u", "import_us_all.py"]
    volumes:
      - /opt/hellofresh-grocery/data:/data
    restart: "no"
```

If you are migrating from the older `hellofresh-grocery` container, stop and remove that **container only** before starting the renamed `hellogroceries` container, or retain the old container name in your existing OMV stack. Do not delete the data folder. Back it up before upgrading.

In OMV, use **Pull** to download the image, then **Up** to start/recreate the service.

## Updating

With the prebuilt image, there is no need to run `git pull` or rebuild locally to update the application:

```bash
docker compose pull hellogroceries
docker compose up -d --no-deps hellogroceries
```

In OpenMediaVault, use **Pull → Up**. Back up `data/` before upgrading. Do not use `docker compose down -v` or delete the data directory.

## Optional US recipe importer

Importing may take hours and depends on public pages, robots.txt, and source-site availability. Start or resume importing from the web interface, or use the optional importer service:

```bash
docker compose --profile us-import run --rm us-importer
```

Do not run two importers against the same data directory. The importer uses a shared lock and saves progress to `data/us_import_progress.json`; logs are written to `data/us_import.log`. A saved checkpoint does not mean an import is currently running. Restarting the web container interrupts an importer started from the webpage; use **Start / Resume** afterward.

## Building from source (optional)

To develop or build locally instead of using the prebuilt image:

```bash
git clone https://github.com/ccf5nhyrk2-art/hellogroceries.git
cd hellogroceries
docker build -t hellogroceries:local .
```

## Security and data limitations

- HelloGroceries has **no authentication**. Do not expose port 8088 to the public internet; use a trusted LAN or authenticated private VPN.
- Imported recipe metadata and image URLs may be incomplete or unavailable.
- Imported ingredient lines are **not normalized into reliable quantities and units**. Check your grocery list before shopping.
- This is a lightweight personal project, not a production-hardened web service.

## Repository layout

- `app/server.py` — HTTP API and SQLite storage
- `app/index.html` — recipe browser and grocery-list UI
- `app/import_us_all.py` — optional US sitemap importer
- `app/recipes.json` — empty starter catalog
- `Dockerfile` — application image build
- `compose.yaml` — prebuilt-image deployment
- `.github/workflows/publish-image.yml` — automated image publishing
- `data/` — private runtime data (Git-ignored)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Do not commit scraped recipe catalogs, copyrighted photos, personal databases, or credentials.

## License

Original application code is offered under the [GPL-3.0 License](LICENSE), subject to the contributors' rights to publish their code. This license does not cover HelloFresh trademarks, recipes, photographs, or other third-party content.
