FROM python:3.12-slim AS builder

WORKDIR /app

COPY pyproject.toml .
COPY src ./src

RUN pip install --no-cache-dir ".[tor,vpnsci]"

FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY src ./src
COPY pyproject.toml .

EXPOSE 8000

ENV SCANSCI_PDF_DATA_DIR=/data/paper-fetch
ENV MCP_MODE=streamable_http

CMD ["python", "-m", "scansci_pdf", "run", "--mode", "streamable_http"]
