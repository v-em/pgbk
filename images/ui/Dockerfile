ARG PYTHON_BASE=python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39
FROM ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 AS uv
FROM ${PYTHON_BASE}
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock LICENSE NOTICE THIRD-PARTY-NOTICES.txt /build/
COPY docs/en/restore.md /build/docs/en/restore.md
COPY docs/ru/restore.md /build/docs/ru/restore.md
COPY src /build/src
ENV UV_PROJECT_ENVIRONMENT=/opt/pgbk UV_PYTHON_DOWNLOADS=never PYTHONDONTWRITEBYTECODE=1
RUN uv sync --project /build --frozen --no-dev --no-editable --no-cache --python /usr/local/bin/python \
    && mkdir /data && chown 10001:10001 /data
USER 10001:10001
ENV PATH="/opt/pgbk/bin:${PATH}"
EXPOSE 8443
ENTRYPOINT ["pgbk-ui"]
