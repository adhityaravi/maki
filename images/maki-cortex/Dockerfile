FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml /build/pyproject.toml
COPY pkgs/maki-common /build/pkgs/maki-common
COPY pkgs/maki-cortex /build/pkgs/maki-cortex
RUN cd /build && uv pip install --system ./pkgs/maki-common ./pkgs/maki-cortex \
    && rm -rf /build

RUN useradd -m -s /bin/bash cortex
USER cortex

EXPOSE 8080

CMD ["maki-cortex"]
