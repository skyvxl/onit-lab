#!/usr/bin/env python3
"""Create a local lab config without replacing an existing one."""
import os
from pathlib import Path
import secrets

path = Path(__file__).resolve().parents[1] / ".env"
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print(".env already exists; kept its values.")
else:
    with os.fdopen(fd, "w") as stream:
        stream.write("HTTP_PORT=80\nDB_USER=guestbook\nDB_NAME=guestbook\nDB_PASSWORD=" + secrets.token_hex(24) + "\n")
    print("Created .env with a generated database password (mode 600).")
