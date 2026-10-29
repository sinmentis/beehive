# Containerfile
# One shared image for the web process and every background job. Each Quadlet unit selects its
# role via Exec=, following a "one image, Exec-selected roles" design.
# ENTRYPOINT is bare `python`; each unit supplies the `-m scripts...` module invocation.
FROM python:3.12-slim
# Take CA trust store fixes even when the cached base image predates them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY README.md ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir ".[ai,email]"
# Build-time smoke test: fail loudly here, not at 3am in prod, if an import is unsatisfied.
RUN python -c "import beehive.web.app, scripts.diagnose_partymap_civic, scripts.run_collector, scripts.run_jobs, scripts.run_web, scripts.run_research_worker, scripts.setup_partymap_civic"
ARG BEEHIVE_REVISION=dev
ENV BEEHIVE_REVISION=$BEEHIVE_REVISION
ENTRYPOINT ["python"]
