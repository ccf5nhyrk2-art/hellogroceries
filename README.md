# Community Grocery Planner (unofficial HelloFresh companion)

A small self-hosted grocery planner with recipe search, categories, meal-only filtering, shopping checklists, recipe URL imports and optional US sitemap importing. Built with Python standard library, SQLite and Docker Compose.

> **Unofficial project.** Not affiliated with, endorsed by, or sponsored by HelloFresh. Recipe names and linked photos belong to their respective owners. This repository does not ship a copied recipe catalog or recipe photos.

## Quick start

Requirements: Docker Engine with Compose v2.

```bash
git clone https://github.com/ccf5nhyrk2-art/hellogroceries.git
cd hellogroceries
cp .env.example .env
docker compose up -d --build hellofresh
```

Visit `http://YOUR-SERVER-IP:8088` on your **trusted local network**. Change `HOST_PORT` in `.env` if needed. Data is persisted in `./data/shopping.db` and survives image rebuilds. The bundled starter recipe list is empty; import recipes through the web interface or optional importer.

## Optional US catalog importer

This can run for hours and depends on public pages, robots.txt and source-site availability. It makes network requests and may fail or skip recipes.

```bash
docker compose --profile us-import run -d --name hf-us-importer us-importer
docker logs -f hf-us-importer
```

Before starting another import, check `docker ps -a --filter name=hf-us-importer`. If an old run has finished, remove only that stopped container (`docker rm hf-us-importer`) before rerunning. **Never run two importers against the same data folder.** The website displays saved progress from `data/us_import_progress.json`; a saved checkpoint does not prove a job is still running.

**Start / Resume:** Use the button on the web page to launch the importer within the web container. It resumes by skipping previously imported and attempted URLs. Progress and status update automatically; logs are stored at `data/us_import.log`. The optional Compose importer uses the same shared lock, preventing overlapping runs when both use this updated code. A web-container restart interrupts an importer launched from the webpage; click Start / Resume again afterward. The smaller bulk importer in the web interface is separate. **Do not run this against an older standalone importer version that lacks the shared lock.**

## Updating

```bash
git pull
docker compose up -d --build --no-deps hellofresh
```

Back up `./data` before upgrading. Never run `docker compose down -v` unless you intend to remove data.

## Security

- The application has **no authentication** or per-user accounts. Do not publish port 8088 to the internet or put it behind a public tunnel. Use a trusted LAN or authenticated private VPN.
- Imported pages are fetched from public HTTPS HelloFresh URLs; do not assume all metadata or photo URLs are available or accurate.
- Imported ingredient lines are **not normalized into accurate quantities/units**; review grocery lists before shopping.
- The HTTP server is a lightweight personal project, **not production-hardened**. Review authentication, request validation, rate limiting, and outbound URL restrictions before public deployment.

## Repository layout

- `app/server.py` — HTTP API and SQLite storage
- `app/index.html` — recipe browser and grocery-list UI
- `app/import_us_all.py` — optional US sitemap importer
- `app/recipes.json` — empty starter catalog (no proprietary data shipped)
- `compose.yaml` — web and optional importer services
- `data/` — local private runtime state (Git-ignored)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please do not submit scraped recipe catalogs, copyrighted photos, personal databases, or credentials.

## License

The original application code is offered under the GPL-3.0 License in [LICENSE](LICENSE), subject to confirmation that you have rights to publish all contributed code. The license does not cover HelloFresh trademarks, recipes, photographs, or other third-party content.