FROM node:24-bookworm-slim AS build
WORKDIR /build
COPY apps/frontend/package.json apps/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/frontend/ ./
RUN npm run build && test -s dist/wallpaper.html

FROM caddy:2.10-alpine
COPY --from=build /build/dist /srv
COPY apps/desktop/Caddyfile /etc/caddy/Caddyfile
