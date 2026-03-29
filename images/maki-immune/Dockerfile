FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml /build/pyproject.toml
COPY pkgs/maki-common /build/pkgs/maki-common
COPY pkgs/maki-immune /build/pkgs/maki-immune
RUN cd /build && uv pip install --system ./pkgs/maki-common ./pkgs/maki-immune \
    && rm -rf /build

RUN useradd -m -s /bin/bash immune
USER immune

EXPOSE 8080

CMD ["maki-immune"]
