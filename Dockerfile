FROM python:3.12-slim AS builder

WORKDIR /app

COPY pyproject.toml .
COPY src ./src

RUN pip install --no-cache-dir ".[web,tor,cloakbrowser,instsci]"

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
        libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
        libatspi2.0-0 libwayland-client0 tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY src ./src
COPY pyproject.toml .

EXPOSE 8000

ENV SCANSCI_PDF_DATA_DIR=/data/paper-fetch
ENV CLOAKBROWSER_CACHE_DIR=/data/paper-fetch/browser-cache
ENV MCP_MODE=streamable_http
ENV MALLOC_ARENA_MAX=2

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "scansci_pdf", "run", "--mode", "streamable_http"]
