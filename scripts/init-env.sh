#!/bin/sh
# Create .env with fresh random local secrets. Never overwrites an existing .env.
set -eu
cd "$(dirname "$0")/.."
if [ -f .env ]; then echo ".env already exists - leaving it alone."; exit 0; fi
rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
umask 077
cat > .env <<ENV
# Paste your DeepSeek key here (https://platform.deepseek.com/api_keys), then: docker compose up -d
DEEPSEEK_API_KEY=

# Local-only secrets for the mock platform (generated)
NITX_DEMO_API_KEY=sk-demo-$(rand)
NITX_RIVAL_API_KEY=sk-rival-$(rand)
APPROVER_TOKEN=$(rand)
ADMIN_TOKEN=$(rand)
N8N_ENCRYPTION_KEY=$(rand)
ENV
echo "Created .env - now add DEEPSEEK_API_KEY to it."
