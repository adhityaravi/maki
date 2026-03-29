FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml /build/pyproject.toml
COPY pkgs/maki-common /build/pkgs/maki-common
COPY pkgs/maki-synapse /build/pkgs/maki-synapse
RUN cd /build && uv pip install --system ./pkgs/maki-common ./pkgs/maki-synapse \
    && rm -rf /build

RUN useradd -m -s /bin/bash synapse
USER synapse

EXPOSE 8080

CMD ["maki-synapse"]
