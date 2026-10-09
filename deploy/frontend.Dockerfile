ARG NODE_BASE=node:22-alpine
ARG NGINX_BASE=nginxinc/nginx-unprivileged:alpine
FROM ${NODE_BASE} AS build
WORKDIR /app
ENV NODE_OPTIONS=--max-old-space-size=256
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build
FROM ${NGINX_BASE}
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
