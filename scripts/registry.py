#!/usr/bin/env python3
"""Retry transient Docker registry transport failures, up to three attempts."""
import argparse
import re
import subprocess
import time


def transfer(operation, image, *, run=subprocess.run, sleep=time.sleep, output=print):
    for attempt in range(1, 4):
        result = run(["docker", operation, image], capture_output=True, text=True)
        for value in (result.stdout, result.stderr):
            if value:
                output(value.rstrip())
        if result.returncode == 0:
            return 0
        transient = re.search(
            r"\bEOF\b|connection reset by peer|connection refused|"
            r"TLS handshake timeout|i/o timeout|net/http: timeout awaiting response headers",
            result.stdout + result.stderr, re.IGNORECASE,
        )
        if not transient or attempt == 3:
            return result.returncode
        delay = 2 ** attempt
        output(f"Временная ошибка registry. Повтор {attempt + 1}/3 через {delay} с.")
        sleep(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["push", "pull"])
    parser.add_argument("image")
    args = parser.parse_args()
    return transfer(args.operation, args.image, output=lambda value: print(value, flush=True))


if __name__ == "__main__":
    raise SystemExit(main())
