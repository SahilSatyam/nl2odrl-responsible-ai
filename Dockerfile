# Self-host image: builds the React studio, then serves it with the FastAPI
# app on Uvicorn. Mirrors the verified clone path (pip install, npm ci, build).
# Needs ui-react/ (source) committed; built ui/ is regenerated, not copied.
FROM node:22-slim AS ui
WORKDIR /build
COPY ui-react/package.json ui-react/package-lock.json ui-react/
RUN cd ui-react && npm ci --no-audit --no-fund
COPY ui-react/ ui-react/
RUN cd ui-react && npm run build
# build output lands in /build/ui via vite outDir ../ui

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
WORKDIR /app
COPY pyproject.toml README.md ./
COPY odrl_agent/ odrl_agent/
COPY policies/ policies/
COPY prompts/ prompts/
COPY schemas/ schemas/
COPY scenarios/ scenarios/
COPY tests/fixtures/odrl_nl.yaml tests/fixtures/odrl_nl.yaml
RUN pip install --no-cache-dir .
COPY --from=ui /build/ui/ ui/
EXPOSE 8901
# Liveness: the API answers without credentials on /api/healthz only.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8901/api/healthz', timeout=4).status == 200 else 1)"
CMD ["odrl-host", "serve", "--port", "8901", "--bind", "0.0.0.0"]
