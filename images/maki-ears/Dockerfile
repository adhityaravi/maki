FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml /build/pyproject.toml
COPY pkgs/maki-common /build/pkgs/maki-common
COPY pkgs/maki-ears /build/pkgs/maki-ears
RUN cd /build && uv pip install --system ./pkgs/maki-common ./pkgs/maki-ears \
    && rm -rf /build

RUN useradd -m -s /bin/bash ears
USER ears

CMD ["maki-ears"]
