# gitport — AI-native pre-merge gatekeeper.
# Multi-stage build: wheels are compiled in the builder, the runtime image
# carries only the installed package, git, and a non-root user.
#
#   docker build -t gitport .
#   git diff main... | docker run --rm -i -e COHERE_API_KEY=... gitport check --diff-file -
#   docker run -p 8400:8400 -e COHERE_API_KEY=... gitport   # serves the API

# ---------------------------------------------------------------------------
# Builder: compile the package and its dependencies into an install prefix.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Build isolation off: hatchling is installed explicitly so the pip install
# below resolves against the local source tree only.
RUN pip install --no-cache-dir --upgrade pip hatchling

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

# Install into /install so the runtime stage can copy a clean prefix.
RUN pip install --no-cache-dir --prefix=/install .

# ---------------------------------------------------------------------------
# Runtime: slim image, non-root user, no build tooling.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# git is required for `gitport check --base/--head` inside repositories and
# for the pre-receive hook deployment; diff-text checks don't need it but the
# extra ~30MB keeps every surface working in one image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

RUN useradd --create-home --uid 10001 gitport
USER gitport
WORKDIR /home/gitport

# Runtime state (vector index, reports db) lives under GITPORT_* paths —
# mount a volume at /data and point GITPORT_INDEX_PATH / GITPORT_REPORTS_DB
# at it to persist across containers. See docker-compose.yml.
EXPOSE 8400

ENTRYPOINT ["gitport"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8400"]
