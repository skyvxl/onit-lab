#!/usr/bin/env python3
"""Small load generator; reports real HTTP responses rather than assuming success."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import time
from urllib import error, request


def fetch(url):
    try:
        with request.urlopen(url, timeout=5) as response:
            body = response.read()
            version = response.headers.get("X-App-Version", "")
            host = response.headers.get("X-Served-By", "")
            return str(response.status), version, host, body
    except error.HTTPError as exc:
        return str(exc.code), "", "", b""
    except (OSError, error.URLError):
        return "connection_error", "", "", b""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["smoke", "load", "rate"])
    parser.add_argument("--url", default="http://localhost")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--rps", type=float, default=4)
    parser.add_argument("--replicas", type=int, default=3)
    args = parser.parse_args()
    if args.seconds <= 0 or args.rps <= 0 or args.replicas <= 0:
        parser.error("seconds, rps and replicas must be positive")
    url = args.url.rstrip("/") + "/version"
    results = []
    if args.mode == "rate":
        time.sleep(1.1)
        with ThreadPoolExecutor(max_workers=20) as workers:
            results = list(workers.map(lambda _: fetch(url), range(40)))
    else:
        count = args.replicas * 5 if args.mode == "smoke" else max(1, int(args.seconds * args.rps))
        started = time.monotonic()
        for i in range(count):
            results.append(fetch(url))
            time.sleep(max(0, started + (i + 1) / args.rps - time.monotonic()))
    codes = Counter(row[0] for row in results)
    hosts = Counter(row[2] for row in results if row[0] == "200")
    versions = Counter(row[1] for row in results if row[0] == "200")
    print("HTTP:", dict(codes))
    print("Versions:", dict(versions))
    print("Replicas:", dict(hosts))
    if args.mode == "rate":
        return 0 if codes["429"] > 0 and not (set(codes) - {"200", "429"}) else 1
    if set(codes) != {"200"}:
        return 1
    if args.mode == "smoke":
        for _, version, host, body in results:
            value = json.loads(body)
            if value != {"version": version, "host": host}:
                raise SystemExit("Body and replica headers disagree")
        if len(hosts) != args.replicas:
            raise SystemExit(f"Expected {args.replicas} replicas, got {len(hosts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
