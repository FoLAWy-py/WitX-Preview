# Nginx with the built Web app: the one public entry point (see nginx.conf).
# Build from the repository root: docker build -f deploy/web.Dockerfile .
FROM node:22.23.3-alpine AS build
RUN corepack enable
WORKDIR /src
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/package.json
RUN pnpm install --frozen-lockfile --filter @witx/web
COPY apps/web apps/web
RUN pnpm --filter @witx/web build
# Compressed once here, so Nginx sends the .gz beside each file (gzip_static) without
# compressing on every request. Fonts in woff/woff2 and images are compressed already.
RUN find apps/web/dist -type f \( -name '*.js' -o -name '*.css' -o -name '*.html' \
      -o -name '*.svg' -o -name '*.json' -o -name '*.ttf' \) -exec gzip -9 -k {} +

FROM nginx:1.28.3-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/nginx-locations.inc /etc/nginx/conf.d/witx-locations.inc
COPY --from=build /src/apps/web/dist /usr/share/nginx/html
