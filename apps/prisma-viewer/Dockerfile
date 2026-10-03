FROM node:24.14.0-bookworm-slim AS viewer-build
WORKDIR /build
COPY apps/prisma-viewer/package*.json ./
RUN npm ci --ignore-scripts
COPY apps/prisma-viewer/index.html apps/prisma-viewer/vite.config.js ./
COPY apps/prisma-viewer/src ./src
COPY apps/prisma-viewer/public ./public
COPY apps/prisma-viewer/scripts ./scripts
COPY apps/prisma-viewer/tests ./tests
COPY apps/prisma-viewer/vendor ./vendor
RUN npm test && npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY apps/prisma-viewer/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/prisma-viewer/server.py apps/prisma-viewer/context_layers.py ./
COPY apps/backend/app/prisma/area.py ./prisma/area.py
COPY --from=viewer-build /build/dist ./static
USER 65534:65534
EXPOSE 8081
CMD ["python", "server.py"]
