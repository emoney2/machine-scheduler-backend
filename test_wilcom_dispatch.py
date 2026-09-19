import unittest

from wilcom_dispatch import WilcomDispatch


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class WilcomDispatchTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.dispatch = WilcomDispatch(
            heartbeat_timeout=10,
            lease_timeout=30,
            retention_seconds=60,
            clock=self.clock,
        )

    def test_rejects_command_while_agent_offline(self):
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.dispatch.create("123", "machine1")

    def test_rejects_machine_with_placeholder_mapping(self):
        self.dispatch.heartbeat(["machine1"])
        with self.assertRaisesRegex(RuntimeError, "machine2 needs"):
            self.dispatch.create("123", "machine2")

    def test_command_lifecycle(self):
        self.dispatch.heartbeat(["machine2"], "1.0")
        command = self.dispatch.create("123", "machine2")
        self.assertEqual(command["status"], "queued")

        leased = self.dispatch.lease_next()
        self.assertEqual(leased["id"], command["id"])
        self.assertEqual(leased["status"], "processing")

        finished = self.dispatch.finish(command["id"], ok=True, message="Sent")
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(self.dispatch.get(command["id"])["message"], "Sent")

    def test_stale_heartbeat_is_offline(self):
        self.dispatch.heartbeat(["machine1"])
        self.clock.advance(11)
        self.assertFalse(self.dispatch.agent_status()["online"])

    def test_expired_lease_can_be_retried(self):
        self.dispatch.heartbeat(["machine1"])
        command = self.dispatch.create("123", "machine1")
        self.dispatch.lease_next()
        self.clock.advance(31)
        leased_again = self.dispatch.lease_next()
        self.assertEqual(leased_again["id"], command["id"])


if __name__ == "__main__":
    unittest.main()
