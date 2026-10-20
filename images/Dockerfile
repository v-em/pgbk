# Requires Docker BuildKit with ADD --checksum support.
# Supply an immutable matching-major postgres:*-bookworm base from compatibility.json.
ARG POSTGRES_BASE
FROM ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 AS uv
FROM ${POSTGRES_BASE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:d6795c663894d836ba20840bffe8970aed81bbe25d3151bd4951f6f9c362def9 https://github.com/wal-g/wal-g/releases/download/v3.0.9/wal-g-pg-22.04-amd64 /usr/local/bin/wal-g
RUN chmod 0755 /usr/local/bin/wal-g
COPY pyproject.toml uv.lock LICENSE NOTICE THIRD-PARTY-NOTICES.txt /build/
COPY images/licenses /usr/local/share/pgbk/licenses
COPY src /build/src
ENV UV_PROJECT_ENVIRONMENT=/opt/pgbk UV_PYTHON_DOWNLOADS=never
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv uv sync --project /build --frozen --no-dev --no-editable --no-cache --python /usr/bin/python3 && mkdir /scratch && chown 999:999 /scratch
ENV PATH="/opt/pgbk/bin:${PATH}" HOME=/scratch TMPDIR=/scratch
USER 999:999
WORKDIR /scratch
ENTRYPOINT ["pgbk"]
CMD ["--help"]
