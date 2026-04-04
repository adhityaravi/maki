FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=ghcr.io/astral-sh/uv:latest /uvx /usr/local/bin/uvx

COPY pkgs/maki-common /build/pkgs/maki-common
COPY pkgs/maki-stem /build/pkgs/maki-stem
RUN find /build/pkgs -name pyproject.toml -exec sed -i '/\[tool\.uv\.sources\]/,/^$/d' {} + \
    && uv pip install --system /build/pkgs/maki-common /build/pkgs/maki-stem \
    && rm -rf /build

RUN useradd -m -s /bin/bash stem
USER stem

EXPOSE 8000

CMD ["maki-stem"]
