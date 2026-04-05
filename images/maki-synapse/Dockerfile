FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=ghcr.io/astral-sh/uv:latest /uvx /usr/local/bin/uvx

COPY pkgs/maki-common /build/pkgs/maki-common
COPY pkgs/maki-synapse /build/pkgs/maki-synapse
RUN find /build/pkgs -name pyproject.toml -exec sed -i '/\[tool\.uv\.sources\]/,/^$/d' {} + \
    && uv pip install --system /build/pkgs/maki-common /build/pkgs/maki-synapse \
    && rm -rf /build

RUN useradd -m -s /bin/bash synapse
USER synapse

EXPOSE 8080

CMD ["maki-synapse"]
