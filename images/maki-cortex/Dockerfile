FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=ghcr.io/astral-sh/uv:latest /uvx /usr/local/bin/uvx

COPY pkgs/maki-common /build/pkgs/maki-common
COPY pkgs/maki-cortex /build/pkgs/maki-cortex
RUN find /build/pkgs -name pyproject.toml -exec sed -i '/\[tool\.uv\.sources\]/,/^$/d' {} + \
    && uv pip install --system "/build/pkgs/maki-common[codegraph,claude,github]" /build/pkgs/maki-cortex \
    && rm -rf /build

RUN useradd -m -s /bin/bash cortex
USER cortex

EXPOSE 8080

CMD ["maki-cortex"]
