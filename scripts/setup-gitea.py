#!/usr/bin/env python3
"""Initialize this lab's local Gitea, repository, secrets and runner."""
import json
import base64
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
URL = "http://localhost:3000"
USER = "student"
REPO = "guestbook"
CREDENTIALS = INFRA / ".credentials.json"


def command(args, capture=False):
    return subprocess.run(args, cwd=ROOT, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def api(path, method="GET", body=None, token=None, password=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "token " + token
    elif password:
        headers["Authorization"] = "Basic " + base64.b64encode((USER + ":" + password).encode()).decode()
    req = request.Request(URL + "/api/v1" + path, method=method, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
    with request.urlopen(req, timeout=10) as response:
        data = response.read()
        return json.loads(data) if data else None


def private_write(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(content)


def main():
    compose = ["docker", "compose", "-f", str(INFRA / "docker-compose.yaml")]
    command(compose + ["up", "-d", "gitea"])
    for _ in range(90):
        try:
            api("/version")
            break
        except (OSError, error.URLError):
            time.sleep(1)
    else:
        raise SystemExit("Gitea did not become ready; inspect docker compose -f infra/docker-compose.yaml logs gitea")

    credentials = json.loads(CREDENTIALS.read_text()) if CREDENTIALS.exists() else {}
    try:
        api("/users/" + USER)
    except error.HTTPError as exc:
        if exc.code != 404:
            raise
        password = secrets.token_urlsafe(24)
        # Password is passed to the container through stdin, never printed in logs.
        subprocess.run(compose + ["exec", "-T", "-u", "git", "gitea", "sh", "-c",
                       'read -r password; exec gitea admin user create --admin --username student '
                       '--password "$password" --email student@localhost.test --must-change-password=false'],
                       input=password + "\n", text=True, check=True, stdout=subprocess.DEVNULL)
        credentials["password"] = password

    token = credentials.get("token")
    if token:
        try:
            api(f"/repos/{USER}/{REPO}/actions/secrets", token=token)
        except error.HTTPError as exc:
            if exc.code == 401:
                token = None
            elif exc.code != 404:
                raise
    if not token:
        token = command(compose + ["exec", "-T", "-u", "git", "gitea", "gitea", "admin", "user",
                        "generate-access-token", "--username", USER, "--token-name",
                        "lab-" + secrets.token_hex(4), "--scopes", "write:package,write:repository", "--raw"],
                        capture=True).strip()
    credentials.update(username=USER, token=token, url=URL)
    private_write(CREDENTIALS, json.dumps(credentials, indent=2) + "\n")

    try:
        api(f"/repos/{USER}/{REPO}", token=token)
    except error.HTTPError as exc:
        if exc.code != 404:
            raise
        if not credentials.get("password"):
            raise SystemExit("Existing Gitea user: create guestbook repository manually, then rerun setup.")
        api("/user/repos", "POST", {"name": REPO, "private": False,
            "auto_init": False, "default_branch": "main", "description": "ONIT Lab 2: Go, Caddy, Goose, CI/CD"},
            password=credentials["password"])

    config = json.loads(command(["docker", "compose", "config", "--format", "json"], capture=True))
    db = config["services"]["postgres"]["environment"]
    values = {"REGISTRY_USER": USER, "REGISTRY_TOKEN": token,
              "DB_USER": db["POSTGRES_USER"], "DB_PASSWORD": db["POSTGRES_PASSWORD"],
              "DB_NAME": db["POSTGRES_DB"],
              "HTTP_PORT": str(config["services"]["caddy"]["ports"][0]["published"])}
    for name, value in values.items():
        api(f"/repos/{USER}/{REPO}/actions/secrets/{name}", "PUT", {"data": value}, token)

    runner_token = command(compose + ["exec", "-T", "-u", "git", "gitea", "gitea", "actions",
                                     "generate-runner-token"], capture=True).strip()
    private_write(INFRA / ".env", "RUNNER_TOKEN=" + runner_token + "\n")
    command(compose + ["--profile", "ci", "up", "-d"])
    print(f"Gitea: {URL}/{USER}/{REPO}")
    print(f"Login and registry token are stored locally in {CREDENTIALS} (mode 600).")
    print("Repository and CI secrets are ready. Push your changes to run CI/CD.")


if __name__ == "__main__":
    main()
