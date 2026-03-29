FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pkgs/maki-common /build/pkgs/maki-common
COPY pkgs/maki-ears /build/pkgs/maki-ears
RUN find /build/pkgs -name pyproject.toml -exec sed -i '/\[tool\.uv\.sources\]/,/^$/d' {} + \
    && uv pip install --system /build/pkgs/maki-common /build/pkgs/maki-ears \
    && rm -rf /build

RUN useradd -m -s /bin/bash ears
USER ears

CMD ["maki-ears"]
