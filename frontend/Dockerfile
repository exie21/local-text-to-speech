FROM node:24-bookworm-slim

WORKDIR /app/frontend

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY index.html tsconfig.json vite.config.ts ./
COPY src ./src

EXPOSE 5173

# Phase 1 development server; production serving is handled in Phase 13.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
