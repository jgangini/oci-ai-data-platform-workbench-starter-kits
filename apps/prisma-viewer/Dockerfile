FROM python:3.12-slim-bookworm AS upstream-source
WORKDIR /source
COPY apps/prisma-viewer/prepare_upstream.py apps/prisma-viewer/upstream.json ./
RUN python prepare_upstream.py --output /prepared

FROM node:24.14.0-bookworm-slim AS viewer-build
WORKDIR /app/.upstream
COPY --from=upstream-source /prepared ./
RUN npm ci --ignore-scripts
COPY apps/prisma-viewer/native /app/native
COPY apps/prisma-viewer/package.json apps/prisma-viewer/index.html /app/
COPY apps/prisma-viewer/src /app/src
COPY apps/prisma-viewer/vendor /app/vendor
COPY apps/prisma-viewer/public /app/public
COPY apps/prisma-viewer/tests /app/tests
RUN node --test /app/tests/*.test.mjs && node /app/native/runtime.mjs --build

FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GODS_EYE_NATIVE_ENABLED=true
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=viewer-build /usr/local/bin/node /usr/local/bin/node
COPY apps/prisma-viewer/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=viewer-build /app/.upstream ./.upstream
COPY --from=viewer-build /app/native ./native
COPY apps/prisma-viewer/server.py apps/prisma-viewer/context_layers.py apps/prisma-viewer/provider_probes.py ./
COPY apps/backend/app/prisma/area.py ./prisma/area.py
COPY apps/backend/app/prisma/native_proxy.py ./prisma/native_proxy.py
RUN mkdir -p /app/.upstream/.gev-cache && chown 65534:65534 /app/.upstream/.gev-cache \
    && ln -s /tmp/gev-logs /app/.upstream/.gev-logs
USER 65534:65534
EXPOSE 8081
CMD ["python", "native/entrypoint.py"]
