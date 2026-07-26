FROM node:22-alpine AS web-build
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN addgroup --system anistream \
    && adduser --system --ingroup anistream --home /nonexistent --no-create-home anistream

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN python -m pip install --upgrade pip \
    && python -m pip install .

COPY --from=web-build /build/web/dist ./web/dist
RUN mkdir -p /app/data && chown -R anistream:anistream /app

USER anistream
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=3)"

CMD ["anistream-telegram"]
