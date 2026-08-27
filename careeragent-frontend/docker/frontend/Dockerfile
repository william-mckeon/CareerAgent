# ============================================================================
# careeragent-frontend — Modern React/Node build (replaces Streamlit)
# ============================================================================

# Stage 1: Build
FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json vite.config.js index.html .env ./
COPY src ./src
RUN npm ci
RUN npm run build

# Stage 2: Serve (nginx, lean, no pipeline failures)
FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
RUN chmod -R 755 /usr/share/nginx/html
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget --quiet --tries=1 --spider http://localhost:8501/ || exit 1
ENTRYPOINT ["nginx", "-g", "daemon off;"]
