#!/bin/sh
# TypeScript client from the V3 OpenAPI schema (P3.3 exit criterion).
# Regenerates docs/openapi-v3.json, emits docs/opkura-client.d.ts, and proves
# the result compiles — the "generated TypeScript client compiles" check.
set -eu
cd "$(dirname "$0")/.."

python scripts/export_openapi.py docs/openapi-v3.json
npx -y openapi-typescript docs/openapi-v3.json -o docs/opkura-client.d.ts
npx -y -p typescript tsc --noEmit --strict docs/opkura-client.d.ts
echo "ok: docs/opkura-client.d.ts compiles (tsc --strict)"
