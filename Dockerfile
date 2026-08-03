FROM python:3.12-slim

# Litestream binary (pinned) — streams the SQLite DB off-site to R2.
COPY --from=litestream/litestream:0.3.13 /usr/local/bin/litestream /usr/local/bin/litestream

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Backup wiring: config lives at /etc, entrypoint restores-then-replicates.
COPY litestream.yml /etc/litestream.yml
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# SECRET_KEY (required) must be supplied at runtime — see .env.example. Not
# baked into the image.
ENV FLASK_ENV=production
EXPOSE 5053

# The entrypoint starts waitress via `litestream replicate -exec`.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
