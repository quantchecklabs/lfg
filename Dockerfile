FROM oven/bun:1.3.13-debian

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    jq \
    openssh-client \
    tmux \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build from the checked-out source tree (works for any clone/snapshot of
# the repo — no dependency on a published release bundle).
COPY . .

RUN set -eux; \
  bun install; \
  cd web; \
  bun install; \
  bun run build; \
  cd /app; \
  mkdir -p /data/repos /data/lfg; \
  rm -rf /app/data; \
  ln -s /data/lfg /app/data

ENV NODE_ENV=production
ENV LFG_INSTALL_CHANNEL=container
ENV LFG_HOST=0.0.0.0
ENV LFG_PORT=8766
ENV LFG_REPOS_ROOT=/data/repos
EXPOSE 8766

CMD ["sh", "-lc", "LFG_PORT=${PORT:-$LFG_PORT} exec bun run serve"]
