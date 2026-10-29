import subprocess
import unittest

from scripts.registry import transfer


class RegistryTests(unittest.TestCase):
    def transfer(self, results, operation="push"):
        pending = iter(results)
        calls, pauses, output = [], [], []

        def run(arguments, **options):
            calls.append(arguments)
            return next(pending)

        code = transfer(operation, "localhost:3000/student/app:test",
                        run=run, sleep=pauses.append, output=output.append)
        return code, calls, pauses, output

    @staticmethod
    def result(code=0, stderr=""):
        return subprocess.CompletedProcess([], code, stdout="", stderr=stderr)

    def test_eof_is_retried_and_success_is_reported(self):
        code, calls, pauses, output = self.transfer([
            self.result(1, 'failed to do request: Head "https://localhost:3000/v2/blob": EOF'),
            self.result(),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ["docker", "push", "localhost:3000/student/app:test"])
        self.assertEqual(pauses, [2])

    def test_pull_retries_timeouts_with_bounded_backoff(self):
        code, calls, pauses, output = self.transfer([
            self.result(1, "net/http: timeout awaiting response headers"),
            self.result(1, "connection reset by peer"), self.result(),
        ], operation="pull")
        self.assertEqual(code, 0)
        self.assertEqual([call[1] for call in calls], ["pull"] * 3)
        self.assertEqual(pauses, [2, 4])

    def test_persistent_eof_preserves_failure_after_three_attempts(self):
        code, calls, pauses, output = self.transfer([self.result(42, "EOF")] * 3)
        self.assertEqual(code, 42)
        self.assertEqual(len(calls), 3)
        self.assertEqual(pauses, [2, 4])

    def test_authentication_failure_is_not_retried(self):
        code, calls, pauses, output = self.transfer([self.result(1, "unauthorized: access denied")])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(pauses, [])

    def test_missing_image_is_not_retried(self):
        code, calls, pauses, output = self.transfer([self.result(1, "tag does not exist")])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(pauses, [])

    def test_success_does_not_retry_and_keeps_docker_output(self):
        result = self.result()
        result.stdout = "sha256:example\n"
        code, calls, pauses, output = self.transfer([result])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(pauses, [])
        self.assertIn("sha256:example", "\n".join(output))


if __name__ == "__main__":
    unittest.main()
