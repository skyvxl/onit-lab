import copy
import json
from types import SimpleNamespace
import unittest

from scripts.deploy import Deployer, DeploymentError, DockerEngine, pin_upstreams


CONFIG = {"apps": {"http": {"servers": {"main": {"routes": [{"handle": [
    {"handler": "subroute", "routes": [{"handle": [{
        "handler": "reverse_proxy", "dynamic_upstreams": {"source": "a", "name": "my_backend"}
    }]}]}
] }]}}}}}


class FakeEngine:
    """An in-memory daemon: traffic must never reach a stopped or unready replica."""

    def __init__(self, initial=True, failure=None):
        self.failure = failure
        self.containers = {}
        self.caddy = "proxy" if initial else None
        self.proxy_config = copy.deepcopy(CONFIG)
        self.static = None
        self.migrated = False
        self.locked = False
        self.released = False
        self.unsafe_traffic = False
        self.lost_promotion_response = False
        self.time = 0
        self.events = []
        if initial:
            for number in range(3):
                self.containers[f"old{number}"] = self.container(number, "sha256:old", True)

    @staticmethod
    def container(number, image, healthy):
        return {"Image": image, "State": {"Running": True, "Health": {"Status": "healthy" if healthy else "starting"}},
                "NetworkSettings": {"Networks": {"guestbook_frontend": {"IPAddress": f"172.20.0.{number + 10}"}}}}

    def config(self):
        return {"name": "guestbook", "networks": {"frontend": {"name": "guestbook_frontend"}}, "services": {
            "my_backend": {"image": "registry/app:v2"}, "migrate": {"image": "registry/migrate:v2"},
            "caddy": {"image": "guestbook-caddy:local"}, "postgres": {"image": "postgres:18"}}}

    def ensure_image(self, image):
        return "sha256:new" if image == "registry/app:v2" else "sha256:other"

    def acquire_lock(self, image):
        if self.failure == "lock":
            raise DeploymentError("already locked")
        self.locked = True

    def release_lock(self):
        self.released = True
        self.locked = False

    def backend_ids(self):
        return set(self.containers)

    def inspect(self, ids):
        return {identifier: self.containers[identifier] for identifier in ids}

    def caddy_id(self):
        return self.caddy

    def get_proxy_config(self, identifier):
        return copy.deepcopy(self.proxy_config)

    def load_proxy_config(self, identifier, config):
        self.proxy_config = copy.deepcopy(config)
        proxy = config["apps"]["http"]["servers"]["main"]["routes"][0]["handle"][0]["routes"][0]["handle"][0]
        self.static = {entry["dial"] for entry in proxy.get("upstreams", [])} if "dynamic_upstreams" not in proxy else None
        self.events.append(("traffic", copy.deepcopy(self.static)))
        self.assert_traffic_safe()
        if self.failure == "lost promotion response" and not self.lost_promotion_response and self.static == {
                "172.20.0.20:8080", "172.20.0.21:8080", "172.20.0.22:8080"}:
            self.lost_promotion_response = True
            raise DeploymentError("response lost after Caddy applied config")

    def assert_traffic_safe(self):
        if not self.caddy:
            return
        for item in self.containers.values():
            address = item["NetworkSettings"]["Networks"]["guestbook_frontend"]["IPAddress"] + ":8080"
            if self.static is None or address in self.static:
                if not item["State"]["Running"] or item["State"]["Health"]["Status"] != "healthy":
                    self.unsafe_traffic = True

    def migrate(self):
        self.events.append(("migrate", None))
        if self.failure == "migration":
            raise DeploymentError("migration failed")
        if self.failure == "interrupt":
            raise KeyboardInterrupt()
        self.migrated = True

    def scale(self, count):
        self.events.append(("scale", count))
        for number in range(count - len(self.containers)):
            identifier = f"new{number}"
            image = "sha256:unexpected" if self.failure == "wrong image" else "sha256:new"
            self.containers[identifier] = self.container(number + 10, image, self.failure != "unhealthy")
        self.assert_traffic_safe()
        if self.failure == "partial scale":
            raise DeploymentError("scale failed after creating containers")

    def remove(self, identifier):
        if self.failure == "old cleanup" and identifier.startswith("old"):
            raise DeploymentError("old cleanup failed")
        address = self.containers[identifier]["NetworkSettings"]["Networks"]["guestbook_frontend"]["IPAddress"] + ":8080"
        if self.caddy and (self.static is None or address in self.static):
            self.unsafe_traffic = True
        self.events.append(("remove", identifier))
        del self.containers[identifier]

    def reload_dynamic(self, identifier):
        self.events.append(("dynamic", None))
        self.proxy_config = copy.deepcopy(CONFIG)
        self.static = None
        self.assert_traffic_safe()

    def start_postgres(self):
        self.events.append(("postgres", None))

    def postgres_ids(self):
        return {"postgres"}

    def postgres_healthy(self):
        return True

    def start_caddy(self):
        self.events.append(("caddy", None))
        self.caddy = "proxy"
        self.assert_traffic_safe()

    def sleep(self, seconds):
        self.time += seconds


class DeploymentTests(unittest.TestCase):
    def deploy(self, engine, migrations=True):
        Deployer(engine, replicas=3, timeout=1, migrations=migrations,
                 sleep=engine.sleep, now=lambda: engine.time, output=lambda message: None).run()

    def test_rolling_deploy_serves_only_healthy_pool_and_replaces_all_old_replicas(self):
        engine = FakeEngine()
        self.deploy(engine)
        self.assertEqual(set(engine.containers), {"new0", "new1", "new2"})
        self.assertFalse(engine.unsafe_traffic)
        self.assertIsNone(engine.static)
        self.assertLess(engine.events.index(("migrate", None)), engine.events.index(("scale", 6)))
        self.assertTrue(engine.released)

    def test_unhealthy_new_replicas_are_removed_and_old_pool_restored(self):
        engine = FakeEngine(failure="unhealthy")
        with self.assertRaises(DeploymentError):
            self.deploy(engine)
        self.assertEqual(set(engine.containers), {"old0", "old1", "old2"})
        self.assertFalse(engine.unsafe_traffic)
        self.assertEqual(engine.proxy_config, CONFIG)
        self.assertTrue(engine.released)

    def test_migration_failure_does_not_create_or_remove_replicas(self):
        engine = FakeEngine(failure="migration")
        with self.assertRaises(DeploymentError):
            self.deploy(engine)
        self.assertEqual(set(engine.containers), {"old0", "old1", "old2"})
        self.assertNotIn(("scale", 6), engine.events)
        self.assertTrue(engine.released)

    def test_wrong_image_is_rejected_even_when_new_replicas_are_healthy(self):
        engine = FakeEngine(failure="wrong image")
        with self.assertRaises(DeploymentError):
            self.deploy(engine)
        self.assertEqual(set(engine.containers), {"old0", "old1", "old2"})
        self.assertFalse(engine.unsafe_traffic)

    def test_lost_promotion_response_repins_old_before_removing_new(self):
        engine = FakeEngine(failure="lost promotion response")
        with self.assertRaises(DeploymentError):
            self.deploy(engine)
        self.assertEqual(set(engine.containers), {"old0", "old1", "old2"})
        self.assertFalse(engine.unsafe_traffic)
        self.assertEqual(engine.proxy_config, CONFIG)

    def test_interruption_restores_old_pool_and_releases_lock(self):
        engine = FakeEngine(failure="interrupt")
        with self.assertRaises(KeyboardInterrupt):
            self.deploy(engine)
        self.assertEqual(set(engine.containers), {"old0", "old1", "old2"})
        self.assertEqual(engine.proxy_config, CONFIG)
        self.assertTrue(engine.released)

    def test_unhealthy_old_pool_is_not_scaled_or_removed(self):
        engine = FakeEngine()
        engine.containers["old0"]["State"]["Health"]["Status"] = "unhealthy"
        with self.assertRaises(DeploymentError):
            self.deploy(engine)
        self.assertEqual(set(engine.containers), {"old0", "old1", "old2"})
        self.assertFalse(any(event[0] == "scale" for event in engine.events))

    def test_failed_first_start_does_not_start_caddy_or_leave_unhealthy_replicas(self):
        engine = FakeEngine(initial=False, failure="unhealthy")
        with self.assertRaises(DeploymentError):
            self.deploy(engine)
        self.assertEqual(engine.containers, {})
        self.assertIsNone(engine.caddy)
        self.assertTrue(engine.released)

    def test_partial_scale_failure_still_discovers_and_removes_only_new_replicas(self):
        engine = FakeEngine(failure="partial scale")
        with self.assertRaises(DeploymentError):
            self.deploy(engine)
        self.assertEqual(set(engine.containers), {"old0", "old1", "old2"})
        self.assertFalse(engine.unsafe_traffic)

    def test_cleanup_failure_after_promotion_keeps_new_pool_pinned(self):
        engine = FakeEngine(failure="old cleanup")
        with self.assertRaises(DeploymentError):
            self.deploy(engine)
        self.assertEqual(engine.static, {"172.20.0.20:8080", "172.20.0.21:8080", "172.20.0.22:8080"})
        self.assertNotIn(("dynamic", None), engine.events)
        self.assertFalse(engine.unsafe_traffic)
        self.assertTrue(engine.released)

    def test_first_start_migrates_before_backend_and_starts_caddy_after_readiness(self):
        engine = FakeEngine(initial=False)
        self.deploy(engine)
        self.assertEqual(set(engine.containers), {"new0", "new1", "new2"})
        self.assertLess(engine.events.index(("postgres", None)), engine.events.index(("migrate", None)))
        self.assertLess(engine.events.index(("migrate", None)), engine.events.index(("scale", 3)))
        self.assertEqual(engine.events[-1], ("caddy", None))
        self.assertFalse(engine.unsafe_traffic)

    def test_rollback_mode_skips_migrations(self):
        engine = FakeEngine()
        self.deploy(engine, migrations=False)
        self.assertNotIn(("migrate", None), engine.events)
        self.assertEqual(set(engine.containers), {"new0", "new1", "new2"})

    def test_foreign_lock_is_not_removed(self):
        engine = FakeEngine(failure="lock")
        with self.assertRaises(DeploymentError):
            self.deploy(engine)
        self.assertFalse(engine.released)
        self.assertEqual(set(engine.containers), {"old0", "old1", "old2"})

    def test_proxy_must_be_unique_and_input_snapshot_is_unchanged(self):
        original = copy.deepcopy(CONFIG)
        pinned = pin_upstreams(original, ["172.20.0.10:8080"])
        self.assertEqual(original, CONFIG)
        self.assertNotEqual(pinned, original)
        with self.assertRaises(DeploymentError):
            pin_upstreams({"handlers": [{"handler": "reverse_proxy"}, {"handler": "reverse_proxy"}]}, ["a:8080"])

    def test_admin_requests_target_ipv4_loopback_inside_caddy(self):
        class RecordingEngine(DockerEngine):
            def __init__(self):
                self.commands = []

            def command(self, arguments, **options):
                self.commands.append(arguments)
                return SimpleNamespace(stdout=json.dumps(CONFIG))

        engine = RecordingEngine()
        self.assertEqual(engine.get_proxy_config("proxy"), CONFIG)
        engine.load_proxy_config("proxy", CONFIG)
        self.assertEqual(engine.commands[0][-1], "http://127.0.0.1:2019/config/")
        self.assertEqual(engine.commands[1][-1], "http://127.0.0.1:2019/load")


if __name__ == "__main__":
    unittest.main()
