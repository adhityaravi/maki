FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pkgs/maki-common /build/pkgs/maki-common
COPY pkgs/maki-immune /build/pkgs/maki-immune
RUN find /build/pkgs -name pyproject.toml -exec sed -i '/\[tool\.uv\.sources\]/,/^$/d' {} + \
    && uv pip install --system "/build/pkgs/maki-common[codegraph,claude,k8s]" /build/pkgs/maki-immune \
    && rm -rf /build

RUN useradd -m -s /bin/bash immune
USER immune

EXPOSE 8080

CMD ["maki-immune"]
