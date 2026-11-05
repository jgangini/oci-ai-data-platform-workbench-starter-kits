FROM python:3.12-slim-bookworm AS upstream-source
WORKDIR /source
COPY modules/ai-gods-eye-view/prepare_upstream.py modules/ai-gods-eye-view/upstream.json ./
RUN python prepare_upstream.py --output /prepared

FROM node:24.14.0-bookworm-slim AS viewer-build
WORKDIR /app/.upstream
COPY --from=upstream-source /prepared ./
RUN npm ci --ignore-scripts
COPY modules/ai-gods-eye-view/native /app/native
COPY modules/ai-gods-eye-view/package.json modules/ai-gods-eye-view/index.html /app/
COPY modules/ai-gods-eye-view/src /app/src
COPY modules/ai-gods-eye-view/vendor /app/vendor
COPY modules/ai-gods-eye-view/public /app/public
COPY modules/ai-gods-eye-view/tests /app/tests
RUN node --test /app/tests/*.test.mjs && node /app/native/runtime.mjs --build

FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GODS_EYE_NATIVE_ENABLED=true
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=viewer-build /usr/local/bin/node /usr/local/bin/node
COPY modules/ai-gods-eye-view/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=viewer-build /app/.upstream ./.upstream
COPY --from=viewer-build /app/native ./native
COPY modules/ai-gods-eye-view/server.py modules/ai-gods-eye-view/context_layers.py modules/ai-gods-eye-view/provider_probes.py ./
COPY apps/backend/app/modules/ai_gods_eye_view/runtime/area.py ./app/modules/ai_gods_eye_view/runtime/area.py
COPY apps/backend/app/modules/ai_gods_eye_view/runtime/native_proxy.py ./app/modules/ai_gods_eye_view/runtime/native_proxy.py
COPY apps/backend/app/api/viewer_identity.py ./app/api/viewer_identity.py
RUN mkdir -p /app/.upstream/.gev-cache && chown 65534:65534 /app/.upstream/.gev-cache \
    && ln -s /tmp/gev-logs /app/.upstream/.gev-logs
USER 65534:65534
EXPOSE 8081
CMD ["python", "native/entrypoint.py"]
