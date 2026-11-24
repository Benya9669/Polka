FROM node:22-bookworm-slim AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS app
LABEL org.opencontainers.image.source="https://github.com/Benya9669/Polka"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 QT_QPA_PLATFORM=offscreen
RUN apt-get update \
 && apt-get install -y --no-install-recommends calibre ca-certificates fonts-dejavu-core djvulibre-bin poppler-utils \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --create-home ebook \
 && mkdir -p /data /inbox /app \
 && chown 10001:10001 /data
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
COPY --from=frontend /src/dist /app/web
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

