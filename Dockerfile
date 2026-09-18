# BrewChat: one small container running the web app plus the daily catalog sync.
#
# Secrets are provided at runtime, never baked into the image:
#   BREWCHAT_SUPPLIER_TOML   full contents of config/supplier.local.toml
#   BREWCHAT_PASSPHRASE      shared passphrase for the access gate
#   BREWCHAT_SECRET_KEY      cookie signing key (optional, derived from passphrase if unset)
#   ANTHROPIC_API_KEY
# Mount a volume at /app/data and /app/logs so the cache and logs survive restarts.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock .python-version README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY brewchat ./brewchat
COPY scripts ./scripts
COPY config/supplier.example.toml ./config/
COPY system-prompt.md ./
COPY docker/entrypoint.sh ./docker/
RUN uv sync --frozen --no-dev && chmod +x docker/entrypoint.sh

ENV PORT=8000
EXPOSE 8000
ENTRYPOINT ["./docker/entrypoint.sh"]
