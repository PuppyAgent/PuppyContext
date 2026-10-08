FROM node:22.22.3-bookworm-slim@sha256:e21fc383b50d5347dc7a9f1cae45b8f4e2f0d39f7ade28e4eef7d2934522b752
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates fd-find ripgrep rsync && ln -s /usr/bin/fdfind /usr/local/bin/fd && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/puppyone-agent
COPY package.json ./
COPY package-lock.json ./
COPY patch-dependencies.mjs ./
RUN npm ci --omit=dev --ignore-scripts && node patch-dependencies.mjs && npm cache clean --force
COPY controller.mjs protocol.mjs git.mjs git-relay.mjs worker.mjs ./
RUN mkdir -p /workspace /home/node/.pi && chown node:node /workspace /home/node/.pi
USER root
WORKDIR /workspace
ENV HOME=/home/node NODE_ENV=production
ENTRYPOINT ["sleep", "infinity"]
