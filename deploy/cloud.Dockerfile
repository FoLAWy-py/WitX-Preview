# The cloud image: `witx api`, `witx worker`, `witx migrate` and `witx admin ...`.
# Build from the repository root: docker build -f deploy/cloud.Dockerfile .
FROM ghcr.io/astral-sh/uv:0.10.4 AS uv

FROM python:3.12.15-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/witx
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages/contracts packages/contracts
COPY services/cloud services/cloud
# A workspace member the lock file names; only its metadata is needed.
COPY services/gateway/pyproject.toml services/gateway/LICENSE services/gateway/NOTICE services/gateway/
RUN uv sync --locked --no-dev --no-editable --package witx-cloud

FROM python:3.12.15-slim
# git: `witx plugin` fetches marketplaces (D85).
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home witx \
    && mkdir -p /data/objects /data/plugins && chown witx /data/objects /data/plugins \
    && chmod 700 /data/objects
COPY --from=build /opt/witx /opt/witx
ENV PATH=/opt/witx/bin:$PATH PYTHONUNBUFFERED=1 \
    WITX_OBJECT_STORE=file:///data/objects WITX_PLUGINS_DIR=/data/plugins
USER witx
EXPOSE 8000
ENTRYPOINT ["witx"]
CMD ["api", "--host", "0.0.0.0"]
