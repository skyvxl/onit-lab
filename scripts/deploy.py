#!/usr/bin/env python3
"""Deploy a healthy replacement pool without exposing containers during startup."""

import copy
import ipaddress
import json
import os
import signal
import subprocess
import sys
import time


class DeploymentError(RuntimeError):
    pass


def pin_upstreams(config, addresses):
    result = copy.deepcopy(config)
    proxies = []

    def visit(value):
        if isinstance(value, dict):
            if value.get("handler") == "reverse_proxy":
                proxies.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    if len(proxies) != 1 or not addresses:
        raise DeploymentError("Ожидался ровно один reverse_proxy и непустой пул реплик.")
    proxies[0].pop("dynamic_upstreams", None)
    proxies[0]["upstreams"] = [{"dial": address} for address in sorted(addresses)]
    return result


class DockerEngine:
    LOCK_NAME = "guestbook-deploy-lock"

    def command(self, arguments, *, data=None, check=True):
        try:
            result = subprocess.run(["docker", *arguments], input=data, text=True,
                                    capture_output=True, check=False)
        except OSError as error:
            raise DeploymentError("Не удалось запустить Docker CLI.") from error
        if check and result.returncode:
            # Docker/Compose output can contain interpolated database credentials.
            raise DeploymentError(f"Команда Docker завершилась с кодом {result.returncode}.")
        return result

    def compose(self, *arguments):
        return self.command(["compose", *arguments]).stdout

    def config(self):
        return json.loads(self.compose("config", "--format", "json"))

    def ensure_image(self, image):
        result = self.command(["image", "inspect", "--format", "{{.Id}}", image], check=False)
        if result.returncode:
            self.command(["pull", image])
            result = self.command(["image", "inspect", "--format", "{{.Id}}", image])
        return result.stdout.strip()

    def acquire_lock(self, image):
        try:
            self.command(["create", "--name", self.LOCK_NAME, "--entrypoint", "/bin/true", image])
        except DeploymentError as error:
            raise DeploymentError("Не удалось получить блокировку деплоя; проверьте guestbook-deploy-lock.") from error

    def release_lock(self):
        self.command(["rm", self.LOCK_NAME])

    def backend_ids(self):
        return set(self.compose("ps", "-a", "-q", "my_backend").split())

    def inspect(self, ids):
        if not ids:
            return {}
        records = json.loads(self.command(["inspect", *sorted(ids)]).stdout)
        return {record["Id"]: record for record in records}

    def caddy_id(self):
        ids = self.compose("ps", "-q", "caddy").split()
        if len(ids) > 1:
            raise DeploymentError("Ожидался один контейнер Caddy.")
        return ids[0] if ids else None

    def get_proxy_config(self, identifier):
        result = self.command(["exec", identifier, "wget", "-qO-", "-T", "10", "http://127.0.0.1:2019/config/"])
        return json.loads(result.stdout)

    def load_proxy_config(self, identifier, config):
        self.command(["exec", "-i", identifier, "wget", "-qO-", "-T", "10",
                      "--post-file=/dev/stdin", "--header=Content-Type:application/json",
                      "http://127.0.0.1:2019/load"], data=json.dumps(config))

    def migrate(self):
        self.compose("run", "--rm", "--no-deps", "migrate", "up")

    def scale(self, count):
        self.compose("up", "-d", "--no-deps", "--no-recreate", "--no-build",
                     "--scale", f"my_backend={count}", "my_backend")

    def remove(self, identifier):
        self.command(["stop", "--time", "10", identifier])
        self.command(["rm", identifier])

    def reload_dynamic(self, identifier):
        self.command(["exec", identifier, "caddy", "reload", "--config", "/etc/caddy/Caddyfile",
                      "--adapter", "caddyfile"])

    def start_postgres(self):
        self.compose("up", "-d", "--no-deps", "--no-build", "postgres")

    def postgres_healthy(self):
        ids = set(self.compose("ps", "-a", "-q", "postgres").split())
        return len(ids) == 1 and all(healthy(record) for record in self.inspect(ids).values())

    def start_caddy(self):
        self.compose("up", "-d", "--no-deps", "--no-build", "caddy")


def healthy(record):
    state = record.get("State", {})
    return state.get("Running") is True and state.get("Health", {}).get("Status") == "healthy"


class Deployer:
    def __init__(self, engine, *, replicas=3, timeout=60, migrations=True,
                 sleep=time.sleep, now=time.monotonic, output=print):
        self.engine = engine
        self.replicas = replicas
        self.timeout = timeout
        self.migrations = migrations
        self.sleep = sleep
        self.now = now
        self.output = output
        self.old = None
        self.proxy = None
        self.saved_config = None
        self.old_config = None
        self.scaled = False
        self.promoted = False

    def wait(self, predicate, description):
        deadline = self.now() + self.timeout
        while True:
            if predicate():
                return
            if self.now() >= deadline:
                raise DeploymentError(f"Истекло время ожидания: {description}.")
            self.sleep(min(1, max(0, deadline - self.now())))

    def wait_replicas(self, ids, image):
        def ready():
            records = self.engine.inspect(ids)
            return set(records) == ids and all(healthy(item) and item.get("Image") == image
                                               for item in records.values())
        self.wait(ready, "новые реплики должны стать healthy с нужным образом")

    def addresses(self, ids, network):
        result = []
        for record in self.engine.inspect(ids).values():
            address = record.get("NetworkSettings", {}).get("Networks", {}).get(network, {}).get("IPAddress", "")
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError as error:
                raise DeploymentError("У реплики нет адреса в сети frontend.") from error
            result.append(f"[{parsed}]:8080" if parsed.version == 6 else f"{parsed}:8080")
        return result

    def migrate(self):
        if self.migrations:
            self.output("Применяю миграции базы данных…")
            self.engine.migrate()
        else:
            self.output("Миграции пропущены (RUN_MIGRATIONS=0).")

    def rollback(self):
        if self.old is None:
            return
        # A /load response can be lost after Caddy accepted the config. Pin old again
        # before removing any new replica, even if promotion appeared to fail.
        if self.old_config is not None:
            self.engine.load_proxy_config(self.proxy, self.old_config)
        if self.scaled:
            for identifier in sorted(self.engine.backend_ids() - self.old):
                self.engine.remove(identifier)
        if self.saved_config is not None:
            self.sleep(3)
            self.engine.load_proxy_config(self.proxy, self.saved_config)
        self.output("Деплой отменён; прежние реплики сохранены.")

    def run(self):
        config = self.engine.config()
        services = config["services"]
        app_image = services["my_backend"]["image"]
        target_image = self.engine.ensure_image(app_image)
        self.engine.acquire_lock(app_image)
        try:
            self.output("Блокировка получена. Проверяю текущие реплики…")
            self.old = self.engine.backend_ids()
            self.proxy = self.engine.caddy_id()
            network = config["networks"]["frontend"]["name"]
            if self.migrations:
                self.engine.ensure_image(services["migrate"]["image"])
            if self.old:
                records = self.engine.inspect(self.old)
                if set(records) != self.old or not all(healthy(item) for item in records.values()):
                    raise DeploymentError("Прежние реплики должны быть running и healthy перед деплоем.")
                if not self.proxy:
                    raise DeploymentError("Caddy не запущен при существующих репликах.")
                self.saved_config = self.engine.get_proxy_config(self.proxy)
                self.old_config = pin_upstreams(self.saved_config, self.addresses(self.old, network))
                self.output("Закрепляю трафик за прежними репликами…")
                self.engine.load_proxy_config(self.proxy, self.old_config)
            else:
                if self.proxy:
                    raise DeploymentError("Caddy запущен без backend; сначала восстановите согласованное состояние.")
                self.engine.ensure_image(services["postgres"]["image"])
                self.engine.ensure_image(services["caddy"]["image"])
                self.output("Первый запуск: поднимаю PostgreSQL…")
                self.engine.start_postgres()
                self.wait(self.engine.postgres_healthy, "PostgreSQL должен стать healthy")

            self.migrate()
            self.output(f"Запускаю {self.replicas} новых реплик…")
            self.scaled = True
            self.engine.scale(len(self.old) + self.replicas)
            new = self.engine.backend_ids() - self.old
            if len(new) != self.replicas:
                raise DeploymentError("Число созданных реплик не совпало с REPLICAS.")
            self.wait_replicas(new, target_image)
            if self.old:
                self.output("Новые реплики healthy. Переключаю трафик…")
                self.engine.load_proxy_config(self.proxy, pin_upstreams(self.saved_config, self.addresses(new, network)))
                self.promoted = True
                for identifier in sorted(self.old):
                    self.engine.remove(identifier)
                self.sleep(3)
                self.engine.reload_dynamic(self.proxy)
            else:
                # Starting Caddy may succeed even if the CLI response is interrupted.
                # The healthy new pool must remain available in that case.
                self.promoted = True
                self.engine.start_caddy()
            self.output(f"Деплой завершён: {self.replicas} здоровых реплик обслуживают запросы.")
        except BaseException as error:
            if self.promoted:
                self.output("Трафик уже переключён на новые реплики; завершающая очистка не удалась. Проверьте состояние Docker.")
            else:
                try:
                    self.rollback()
                except BaseException as cleanup_error:
                    self.output("Очистка не завершена. Прежний пул не удалён; проверьте Docker и конфигурацию Caddy.")
                    raise DeploymentError("Деплой и восстановление завершились ошибкой.") from cleanup_error
            raise error
        finally:
            self.engine.release_lock()


def main():
    def interrupted(signum, frame):
        raise DeploymentError(f"Деплой прерван сигналом {signum}.")

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupted)
    try:
        if not os.environ.get("APP_IMAGE", "").strip():
            raise DeploymentError("Задайте APP_IMAGE с образом приложения для деплоя.")
        replicas = int(os.environ.get("REPLICAS", "3"))
        timeout = float(os.environ.get("DEPLOY_TIMEOUT", "60"))
        if replicas < 1 or timeout <= 0:
            raise DeploymentError("REPLICAS и DEPLOY_TIMEOUT должны быть положительными.")
        Deployer(DockerEngine(), replicas=replicas, timeout=timeout,
                 migrations=os.environ.get("RUN_MIGRATIONS", "1") != "0").run()
    except (DeploymentError, ValueError, KeyError, TypeError) as error:
        # Parse errors contain only locations; external command output is suppressed.
        print(f"Ошибка деплоя: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
