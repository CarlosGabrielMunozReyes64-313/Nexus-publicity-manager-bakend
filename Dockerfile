# Imagen única: frontend compilado + API + trabajador.
#
# El frontend llega como contexto adicional llamado «frontend» (docker-compose.yml lo
# toma de FRONTEND_DIR). Así funciona con las carpetas tal como se extraen los ZIP:
#   Nexus-publicity-manager-bakend-main/   ← este backend (contexto principal)
#   Nexus-publicity-manager-main/          ← frontend (contexto «frontend»)
# Sin Compose:
#   docker build --build-context frontend=../Nexus-publicity-manager-main -t nexus .

FROM node:24-bookworm-slim AS web
WORKDIR /web
COPY --from=frontend package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY --from=frontend index.html tsconfig.json vite.config.ts ./
COPY --from=frontend src ./src
COPY --from=frontend public ./public
# Mismo origen: la API vive en /api del mismo dominio; sin datos simulados.
ENV VITE_USE_MOCKS=false
ENV VITE_API_URL=/api
RUN npm run build

FROM python:3.12-slim AS wheels
WORKDIR /build
COPY pyproject.toml requirements.lock ./
COPY app ./app
RUN pip wheel --no-cache-dir --wheel-dir /wheels -c requirements.lock .

FROM python:3.12-slim
WORKDIR /srv/nexus
COPY --from=wheels /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels && useradd --uid 10001 --create-home nexus
COPY app ./app
COPY migraciones ./migraciones
COPY scripts ./scripts
COPY sql ./sql
COPY alembic.ini Procfile ./
COPY --from=web /web/dist ./frontend/dist
USER nexus
EXPOSE 8000
CMD ["honcho", "start"]
