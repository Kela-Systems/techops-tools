# Build context must be the techops-tools root (so ../hub-admin is reachable),
# plus an additional named context for the internal hub-client-py package:
#
#   docker build -f hub-admin-web/Dockerfile \
#     --build-context hub-client-py=/path/to/kela/hub/client/hub-client-py \
#     -t hub-admin-web .
#
# (docker-compose.yaml wires all of this up.)

# ── frontend build ───────────────────────────────────────────────────────
FROM node:22-alpine AS frontend
WORKDIR /build
COPY hub-admin-web/frontend/package.json hub-admin-web/frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY hub-admin-web/frontend/ ./
RUN npm run build

# ── backend runtime ──────────────────────────────────────────────────────
FROM python:3.13-slim
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.31.4

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
        -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && apt-get purge -y curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Internal dependency (hub_client + kela protos), from the named build context.
COPY --from=hub-client-py . /app/hub-client-py
COPY hub-admin /app/hub-admin
COPY hub-admin-web/backend /app/backend

RUN pip install --no-cache-dir /app/hub-client-py /app/hub-admin /app/backend

COPY --from=frontend /build/dist /app/frontend/dist
ENV HUB_ADMIN_WEB_STATIC=/app/frontend/dist

EXPOSE 8000
CMD ["uvicorn", "hub_admin_web.main:app", "--host", "0.0.0.0", "--port", "8000"]
