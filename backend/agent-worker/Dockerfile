FROM node:22.22.3-bookworm-slim@sha256:e21fc383b50d5347dc7a9f1cae45b8f4e2f0d39f7ade28e4eef7d2934522b752
WORKDIR /opt/puppyone-agent
COPY package.json package-lock.json patch-dependencies.mjs ./
RUN npm ci --omit=dev --ignore-scripts && node patch-dependencies.mjs && npm cache clean --force
COPY worker.mjs capture.mjs ./
RUN mkdir -p /workspace /home/node/.pi && chown node:node /workspace /home/node/.pi
USER node
WORKDIR /workspace
ENV HOME=/home/node NODE_ENV=production
ENTRYPOINT ["node", "/opt/puppyone-agent/worker.mjs"]
