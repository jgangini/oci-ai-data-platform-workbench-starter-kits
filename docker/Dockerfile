FROM node:22-bookworm-slim AS frontend
WORKDIR /src/apps/frontend
COPY apps/frontend/package*.json ./
RUN npm ci
COPY apps/frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
ARG APP_RELEASE_TAG=development
ARG APP_RELEASE_SHA=development
ARG APP_REPOSITORY=https://github.com/jgangini/oci-ai-data-platform-workbench-starter-kits
ENV APP_RELEASE_TAG=$APP_RELEASE_TAG \
    APP_RELEASE_SHA=$APP_RELEASE_SHA \
    APP_REPOSITORY=$APP_REPOSITORY
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx openssl curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/aidp-lab
COPY apps/backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/backend/app ./app
COPY --from=frontend /src/apps/frontend/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/nginx.conf
COPY docker/entrypoint.sh /usr/local/bin/aidp-lab-entrypoint
RUN chmod 0755 /usr/local/bin/aidp-lab-entrypoint \
    && mkdir -p /var/lib/aidp-lab /etc/aidp-lab/tls \
    && chown -R www-data:www-data /var/lib/aidp-lab
EXPOSE 80 443
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD curl -fk https://127.0.0.1/api/health || exit 1
ENTRYPOINT ["/usr/local/bin/aidp-lab-entrypoint"]
