"""Export the V3 API's OpenAPI schema (P3.3 exit criterion).

    python scripts/export_openapi.py [out.json]

Builds the app with dummy credentials and a never-connected engine — schema
generation is pure metadata, so this runs anywhere, no Postgres, no .env.
The companion `scripts/generate_ts_client.sh` turns the JSON into a compiled
TypeScript client; CI runs both so the schema in docs/ can never go stale
without a diff showing it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI

from vjudge.api import install_v3
from vjudge.core.config import Settings
from vjudge.db.engine import sync_engine


def build_schema() -> dict:
    settings = Settings(
        _env_file=None,
        twilio_account_sid="schema-export",
        twilio_auth_token="schema-export",
        twilio_phone_number="+15550000000",
        deepgram_api_key="schema-export",
    )
    app = FastAPI(title="VJudge V3 API", version="3.0.0")
    install_v3(
        app,
        settings=settings,
        engine=sync_engine("postgresql+psycopg://schema:export@localhost:1/none"),
        executor=object(),  # never dispatched during schema generation
    )
    return app.openapi()


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/openapi-v3.json")
    schema = build_schema()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out} ({len(schema.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
