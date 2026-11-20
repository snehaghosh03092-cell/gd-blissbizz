ARG NODE_IMAGE=node:24-alpine
ARG NGINX_IMAGE=nginx:stable-alpine
FROM ${NODE_IMAGE} AS build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund --fetch-timeout=60000 --fetch-retries=1
COPY frontend/ ./
ARG BASE_DOMAIN=blissbiz.biz
ENV VITE_BASE_DOMAIN=${BASE_DOMAIN}
RUN npm run build

FROM ${NGINX_IMAGE}
COPY --from=build /build/dist /opt/blissbiz/current/frontend/dist
COPY deploy/nginx/vps.conf /etc/nginx/conf.d/default.conf
COPY deploy/nginx/vps-proxy.conf /etc/nginx/blissbiz-proxy.conf
