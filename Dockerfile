FROM node:22-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1
RUN apt-get update && apt-get install -y --no-install-recommends nginx \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 novel \
    && useradd --uid 10001 --gid novel --create-home novel
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt && python -m pip check
COPY --chown=novel:novel backend/ /app/backend/
COPY --chown=novel:novel deploy/ /app/deploy/
COPY --from=frontend --chown=novel:novel /build/frontend/dist/ /app/frontend/dist/
RUN mkdir -p /app/backend/data && chown novel:novel /app/backend/data
USER novel
EXPOSE 10000
CMD ["python", "/app/deploy/launch.py", "serve"]
