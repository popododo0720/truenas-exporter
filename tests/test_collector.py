import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRUENAS_API_KEY", "test")

import truenas_exporter as te  # noqa: E402


class FakeClient:
    def __init__(self, responses):
        self.responses = responses

    def call(self, method, params=None):
        return self.responses[method]


POOLS = [
    {
        "name": "tank",
        "status": "ONLINE",
        "healthy": True,
        "size": 1000,
        "allocated": 250,
        "free": 750,
        "scan": {"function": "SCRUB", "state": "FINISHED", "errors": 0, "end_time": {"$date": 1762700000000}},
        "topology": {
            "data": [
                {
                    "name": "mirror-0",
                    "stats": {"read_errors": 3, "write_errors": 0, "checksum_errors": 1},
                    "children": [
                        {"name": "sda", "stats": {"read_errors": 2, "write_errors": 0, "checksum_errors": 1}, "children": []},
                        {"name": "sdb", "stats": {"read_errors": 1, "write_errors": 0, "checksum_errors": 0}, "children": []},
                    ],
                }
            ]
        },
    }
]


def samples(families):
    out = {}
    for fam in families:
        for s in fam.samples:
            out[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return out


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.c = te.TrueNASCollector()

    def test_pools(self):
        s = samples(self.c._pools(FakeClient({"pool.query": POOLS})))
        self.assertEqual(s[("truenas_pool_free_bytes", (("pool", "tank"),))], 750)
        self.assertEqual(s[("truenas_pool_healthy", (("pool", "tank"),))], 1)
        # Leaf devices only: 2 + 1 read errors, not 3 more from the mirror itself.
        self.assertEqual(s[("truenas_pool_vdev_errors", (("pool", "tank"), ("type", "read")))], 3)
        self.assertEqual(
            s[("truenas_pool_scan_end_timestamp_seconds", (("function", "SCRUB"), ("pool", "tank")))], 1762700000.0
        )

    def test_disks(self):
        s = samples(
            self.c._disks(
                FakeClient(
                    {
                        "disk.query": [{"name": "sda", "serial": "S1", "model": "M", "size": 10, "pool": "tank"}],
                        "disk.temperatures": {"sda": 34.0},
                    }
                )
            )
        )
        self.assertEqual(s[("truenas_disk_temperature_celsius", (("disk", "sda"), ("serial", "S1")))], 34.0)

    def test_datasets_depth_and_internal(self):
        self.c.dataset_depth = 2
        s = samples(
            self.c._datasets(
                FakeClient(
                    {
                        "pool.dataset.query": [
                            {"name": "tank", "pool": "tank", "used": {"parsed": 5}, "available": {"parsed": 6}},
                            {"name": "tank/a/b", "pool": "tank", "used": {"parsed": 1}, "available": {"parsed": 6}},
                            {"name": "tank/a/b/c", "pool": "tank", "used": {"parsed": 1}, "available": {"parsed": 6}},
                            {"name": "tank/.system", "pool": "tank", "used": {"parsed": 1}, "available": {"parsed": 6}},
                        ]
                    }
                )
            )
        )
        names = {dict(k[1])["dataset"] for k in s}
        self.assertEqual(names, {"tank", "tank/a/b"})

    def test_alerts(self):
        s = samples(
            self.c._alerts(FakeClient({"alert.list": [{"level": "WARNING"}, {"level": "WARNING", "dismissed": True}]}))
        )
        self.assertEqual(s[("truenas_alerts", (("level", "WARNING"),))], 1)
        self.assertEqual(s[("truenas_alerts", (("level", "CRITICAL"),))], 0)

    def test_alert_classes(self):
        s = samples(
            self.c._alerts(
                FakeClient(
                    {
                        "alert.list": [
                            {"level": "CRITICAL", "klass": "SMART"},
                            {"level": "CRITICAL", "klass": "SMART"},
                            {"level": "WARNING", "klass": "ApiKeyRevoked", "dismissed": True},
                        ]
                    }
                )
            )
        )
        self.assertEqual(s[("truenas_alert_class", (("klass", "SMART"), ("level", "CRITICAL")))], 2)
        self.assertNotIn(("truenas_alert_class", (("klass", "ApiKeyRevoked"), ("level", "WARNING"))), s)


if __name__ == "__main__":
    unittest.main()
