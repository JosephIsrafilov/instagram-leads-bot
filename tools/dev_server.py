"""Run the bot locally with demo accounts and console sending (no Meta app needed).

    python tools/dev_server.py

Then, in another terminal:

    python tools/simulate.py

Admin panel: http://127.0.0.1:8000/admin (admin / dev-admin-pass).
"""

from __future__ import annotations

import json
import os
import sys
import uvicorn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEMO_ACCOUNTS = {
    "17841400000000001": {"name": "Basic Academy (demo)", "access_token": "dev-token-basic"},
    "17841400000000002": {"name": "Affidavit (demo)", "access_token": "dev-token-affidavit"},
}

DEFAULTS = {
    "DATABASE_PATH": "./dev.sqlite3",
    "IG_ACCOUNTS_JSON": json.dumps(DEMO_ACCOUNTS),
    "META_APP_SECRET": "dev_secret",
    "META_VERIFY_TOKEN": "dev_verify",
    "ADMIN_USER": "admin",
    "ADMIN_PASSWORD": "dev-admin-pass",
    "IG_SEND_MODE": "console",
    "WORKER_POLL_INTERVAL": "0.5",
}


def main() -> None:
    for key, value in DEFAULTS.items():
        os.environ.setdefault(key, value)
    host = os.environ.get("DEV_HOST", "127.0.0.1")
    port = int(os.environ.get("DEV_PORT", "8000"))
    print(f"dev server: http://{host}:{port}  (panel: /admin, admin/{DEFAULTS['ADMIN_PASSWORD']})")
    uvicorn.run("app:app", host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()