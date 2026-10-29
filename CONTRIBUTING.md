# Contributing

Please open an issue describing the bug or proposed change. Keep pull requests small and include steps to reproduce. Do not commit personal data, imported recipe databases, downloaded photos, credentials, or material you lack permission to redistribute.

Local smoke test:

```bash
python3 -m py_compile app/server.py app/import_us_all.py
docker compose config
```

The application has no automated test suite yet; manual UI and import testing is recommended.