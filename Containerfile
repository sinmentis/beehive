# Containerfile
# One shared image for all three roles (web/fetch/digest) — each Quadlet unit below
# selects its role via Exec=, following a "one image, Exec-selected roles" design.
# ENTRYPOINT is bare `python`; each unit supplies the `-m scripts...` module invocation.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY README.md ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir ".[ai,email]"
# Build-time smoke test: fail loudly here, not at 3am in prod, if an import is unsatisfied.
RUN python -c "import beehive.web.app, scripts.run_collector, scripts.run_web"
ENTRYPOINT ["python"]
