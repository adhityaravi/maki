FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc libc6-dev && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pkgs/maki-common /build/pkgs/maki-common
COPY pkgs/maki-recall /build/pkgs/maki-recall
RUN find /build/pkgs -name pyproject.toml -exec sed -i '/\[tool\.uv\.sources\]/,/^$/d' {} + \
    && uv pip install --system /build/pkgs/maki-common /build/pkgs/maki-recall \
    && rm -rf /build

RUN apt-get purge -y gcc libc6-dev && apt-get autoremove -y

RUN useradd -m -s /bin/bash recall
RUN mkdir -p /data && chown recall:recall /data
USER recall

EXPOSE 8000

CMD ["maki-recall"]
