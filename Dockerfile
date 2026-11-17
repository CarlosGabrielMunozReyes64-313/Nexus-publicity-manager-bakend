FROM node:24-bookworm-slim AS frontend
WORKDIR /frontend
COPY Nexus-publicity-manager-front/package*.json ./
RUN npm ci --no-audit --no-fund
COPY Nexus-publicity-manager-front/ ./
ENV VITE_USE_MOCKS=false
ENV VITE_API_URL=/api
RUN npm run build

FROM python:3.12-slim AS wheels
WORKDIR /build
COPY nexus-publicity-manager-backend/pyproject.toml nexus-publicity-manager-backend/requirements.lock ./
COPY nexus-publicity-manager-backend/app ./app
RUN pip wheel --no-cache-dir --wheel-dir /wheels -c requirements.lock .

FROM python:3.12-slim
WORKDIR /srv/nexus
COPY --from=wheels /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels && useradd --uid 10001 --create-home nexus
COPY nexus-publicity-manager-backend/app ./app
COPY nexus-publicity-manager-backend/migraciones ./migraciones
COPY nexus-publicity-manager-backend/scripts ./scripts
COPY nexus-publicity-manager-backend/sql ./sql
COPY nexus-publicity-manager-backend/alembic.ini nexus-publicity-manager-backend/Procfile ./
COPY --from=frontend /frontend/dist ./frontend/dist
USER nexus
EXPOSE 8000
CMD ["honcho", "start"]
