"""Generate new secrets without printing them or overwriting existing configuration."""

import argparse
import hashlib
import json
import os
import secrets
import shlex
from pathlib import Path

from cryptography.fernet import Fernet


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", action="append", required=True)
    parser.add_argument("--development", action="store_true")
    args = parser.parse_args()
    if os.getuid() == 0:
        parser.error("Run as the non-root deployment owner, not root")
    root = Path(__file__).resolve().parents[1]
    directory = root / ".secrets"
    if directory.exists() or (root / ".env").exists():
        parser.error("Existing .secrets or .env found; refusing to overwrite configuration")
    directory.mkdir(mode=0o700)

    def save(name, value):
        with open(directory / name, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
            file.write(value + "\n")

    clients = []
    for role in ("sender", "operator", "reconciler", "metrics"):
        token = secrets.token_urlsafe(32)
        save(role + "-token", token)
        clients.append({
            "name": role, "tenant": "system" if role == "metrics" else "default",
            "role": role, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        })
    save("auth.json", json.dumps(clients, indent=2))
    save("keys.json", json.dumps({
        "encryption_keys": [Fernet.generate_key().decode()],
        "fingerprint_key": secrets.token_hex(32),
    }, indent=2))
    save("credentials.json", "{}")
    if args.development:
        password = secrets.token_urlsafe(32)
        save("postgres-password", password)
        save("database-url", f"postgresql://notifier:{password}@db:5432/notifier")
    (root / ".data").mkdir(mode=0o700, exist_ok=True)
    for name in ("prometheus", "alertmanager"):
        (root / ".data" / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {
        "NOTIFY_ENV": "development" if args.development else "production",
        "NOTIFY_DATABASE_URL_FILE": str(directory / "database-url"),
        "NOTIFY_AUTH_FILE": str(directory / "auth.json"),
        "NOTIFY_KEYS_FILE": str(directory / "keys.json"),
        "NOTIFY_CREDENTIALS_FILE": str(directory / "credentials.json"),
        "NOTIFY_ALLOWED_ORIGINS": ",".join(args.origin),
        "NOTIFY_UID": str(os.getuid()),
        "NOTIFY_GID": str(os.getgid()),
    }
    with open(root / ".env", "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
        file.write("\n".join(f"{key}={shlex.quote(value)}" for key, value in env.items()) + "\n")
    print("Created .secrets/ and .env; token values were not printed.")
    if not args.development:
        print("Provision .secrets/database-url with a verify-full PostgreSQL DSN before starting.")


if __name__ == "__main__":
    main()
