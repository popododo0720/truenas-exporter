"""Small Prometheus exporter for the TrueNAS SCALE JSON-RPC (WebSocket) API.

It covers what TrueNAS's Netdata/Graphite reporting does not export:
pool capacity and health, scrub results, disk temperatures, dataset usage and
TrueNAS alerts. CPU, memory, ARC, disk I/O, network and NFS come from Graphite.

Each Prometheus scrape opens one WebSocket connection, logs in with a read-only
API key, runs a handful of queries and closes the connection.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
from typing import Any, Iterable

from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import GaugeMetricFamily
from websocket import create_connection

LOG = logging.getLogger("truenas_exporter")


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    path = os.environ.get(name + "_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return default


class RpcError(RuntimeError):
    pass


class Client:
    """Minimal JSON-RPC 2.0 over WebSocket client."""

    def __init__(self, url: str, timeout: float, insecure: bool, ca_file: str | None):
        sslopt: dict[str, Any] = {}
        if url.startswith("wss://"):
            if insecure:
                sslopt = {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
            elif ca_file:
                sslopt = {"cert_reqs": ssl.CERT_REQUIRED, "ca_certs": ca_file}
        self.ws = create_connection(url, timeout=timeout, sslopt=sslopt)
        self.next_id = 0

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        self.next_id += 1
        req_id = self.next_id
        self.ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or []}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") != req_id:
                continue  # event notifications and other replies
            if "error" in msg:
                raise RpcError(f"{method}: {msg['error']}")
            return msg.get("result")


def as_float(value: Any) -> float | None:
    if isinstance(value, dict):  # dataset properties: {"parsed": ..., "rawvalue": ...}
        value = value.get("parsed", value.get("rawvalue"))
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_timestamp(value: Any) -> float | None:
    """TrueNAS returns datetimes as {"$date": <milliseconds>}."""
    if isinstance(value, dict) and "$date" in value:
        ms = as_float(value["$date"])
        return ms / 1000.0 if ms is not None else None
    return as_float(value)


def walk_vdevs(vdevs: Iterable[Any]) -> Iterable[dict]:
    for vdev in vdevs or []:
        if isinstance(vdev, dict):
            yield vdev
            yield from walk_vdevs(vdev.get("children"))


class TrueNASCollector:
    def __init__(self) -> None:
        self.url = env("TRUENAS_URL", "wss://truenas.home.arpa/api/current")
        self.api_key = env("TRUENAS_API_KEY")
        self.timeout = float(env("TRUENAS_TIMEOUT", "20"))
        self.insecure = env("TRUENAS_INSECURE", "false").lower() == "true"
        self.ca_file = env("TRUENAS_CA_FILE")
        # Datasets up to this many path levels below the pool (pool/a = 1, pool/a/b = 2).
        self.dataset_depth = int(env("TRUENAS_DATASET_DEPTH", "2"))
        self.lock = threading.Lock()
        if not self.api_key:
            raise SystemExit("TRUENAS_API_KEY (or TRUENAS_API_KEY_FILE) is required")

    def describe(self) -> list:
        return []  # metrics are dynamic; skip the describe-time collect

    def collect(self) -> Iterable[GaugeMetricFamily]:
        with self.lock:
            yield from self._collect()

    def _collect(self) -> Iterable[GaugeMetricFamily]:
        start = time.monotonic()
        up = GaugeMetricFamily("truenas_up", "1 if the last scrape could log in to the TrueNAS API")
        errors = GaugeMetricFamily(
            "truenas_collector_error", "1 if this collector failed during the last scrape", labels=["collector"]
        )
        families: list[GaugeMetricFamily] = []
        client = None
        try:
            client = Client(self.url, self.timeout, self.insecure, self.ca_file)
            if not client.call("auth.login_with_api_key", [self.api_key]):
                raise RpcError("auth.login_with_api_key returned false")
            up.add_metric([], 1)
            for name, fn in (
                ("pools", self._pools),
                ("disks", self._disks),
                ("datasets", self._datasets),
                ("alerts", self._alerts),
            ):
                try:
                    families.extend(fn(client))
                    errors.add_metric([name], 0)
                except Exception:  # noqa: BLE001 - one failing collector must not hide the others
                    LOG.exception("collector %s failed", name)
                    errors.add_metric([name], 1)
        except Exception:  # noqa: BLE001
            LOG.exception("cannot reach or log in to %s", self.url)
            up.add_metric([], 0)
        finally:
            if client:
                client.close()

        duration = GaugeMetricFamily("truenas_scrape_duration_seconds", "Time spent querying the TrueNAS API")
        duration.add_metric([], time.monotonic() - start)
        yield from families
        yield up
        yield errors
        yield duration

    def _pools(self, client: Client) -> list[GaugeMetricFamily]:
        pools = client.call(
            "pool.query",
            [[], {"select": ["name", "status", "healthy", "size", "allocated", "free", "scan", "topology"]}],
        )
        size = GaugeMetricFamily("truenas_pool_size_bytes", "Pool size", labels=["pool"])
        allocated = GaugeMetricFamily("truenas_pool_allocated_bytes", "Pool space in use", labels=["pool"])
        free = GaugeMetricFamily("truenas_pool_free_bytes", "Pool free space", labels=["pool"])
        healthy = GaugeMetricFamily("truenas_pool_healthy", "1 if TrueNAS reports the pool healthy", labels=["pool"])
        status = GaugeMetricFamily(
            "truenas_pool_status", "Pool status (ONLINE, DEGRADED, FAULTED, ...) as a label", labels=["pool", "status"]
        )
        vdev_errors = GaugeMetricFamily(
            "truenas_pool_vdev_errors", "Read/write/checksum errors summed over all vdevs", labels=["pool", "type"]
        )
        scan_end = GaugeMetricFamily(
            "truenas_pool_scan_end_timestamp_seconds", "End of the last scrub or resilver", labels=["pool", "function"]
        )
        scan_errors = GaugeMetricFamily(
            "truenas_pool_scan_errors", "Errors found by the last scrub or resilver", labels=["pool", "function"]
        )
        scan_state = GaugeMetricFamily(
            "truenas_pool_scan_state", "State of the last scrub or resilver as a label", labels=["pool", "function", "state"]
        )
        for pool in pools or []:
            name = str(pool.get("name"))
            for family, key in ((size, "size"), (allocated, "allocated"), (free, "free")):
                value = as_float(pool.get(key))
                if value is not None:
                    family.add_metric([name], value)
            healthy.add_metric([name], 1 if pool.get("healthy") else 0)
            status.add_metric([name, str(pool.get("status", "UNKNOWN"))], 1)

            totals = {"read": 0.0, "write": 0.0, "checksum": 0.0}
            topology = pool.get("topology") or {}
            for vdev_class in ("data", "cache", "log", "spare", "special", "dedup"):
                for vdev in walk_vdevs(topology.get(vdev_class)):
                    stats = vdev.get("stats") or {}
                    if vdev.get("children"):
                        continue  # count leaf devices only, parents repeat the sum
                    for kind in totals:
                        totals[kind] += as_float(stats.get(f"{kind}_errors")) or 0.0
            for kind, value in totals.items():
                vdev_errors.add_metric([name, kind], value)

            scan = pool.get("scan") or {}
            function = str(scan.get("function") or "NONE")
            end = as_timestamp(scan.get("end_time"))
            if end is not None:
                scan_end.add_metric([name, function], end)
            scan_err = as_float(scan.get("errors"))
            if scan_err is not None:
                scan_errors.add_metric([name, function], scan_err)
            scan_state.add_metric([name, function, str(scan.get("state") or "NONE")], 1)
        return [size, allocated, free, healthy, status, vdev_errors, scan_end, scan_errors, scan_state]

    def _disks(self, client: Client) -> list[GaugeMetricFamily]:
        disks = client.call(
            "disk.query",
            [[], {"extra": {"pools": True}, "select": ["name", "serial", "model", "size", "pool"]}],
        )
        temps = client.call("disk.temperatures") or {}
        info = GaugeMetricFamily(
            "truenas_disk_info", "Disk identity, always 1", labels=["disk", "serial", "model", "pool"]
        )
        size = GaugeMetricFamily("truenas_disk_size_bytes", "Disk size", labels=["disk", "serial"])
        temperature = GaugeMetricFamily(
            "truenas_disk_temperature_celsius", "Current disk temperature", labels=["disk", "serial"]
        )
        for disk in disks or []:
            name = str(disk.get("name"))
            serial = str(disk.get("serial") or "")
            info.add_metric([name, serial, str(disk.get("model") or ""), str(disk.get("pool") or "")], 1)
            value = as_float(disk.get("size"))
            if value is not None:
                size.add_metric([name, serial], value)
            temp = as_float(temps.get(name)) if isinstance(temps, dict) else None
            if temp is not None:
                temperature.add_metric([name, serial], temp)
        return [info, size, temperature]

    def _datasets(self, client: Client) -> list[GaugeMetricFamily]:
        datasets = client.call(
            "pool.dataset.query",
            [[], {"select": ["name", "pool", "used", "available"], "extra": {"flat": True, "properties": ["used", "available"]}}],
        )
        used = GaugeMetricFamily("truenas_dataset_used_bytes", "Space used by the dataset and its children", labels=["dataset", "pool"])
        available = GaugeMetricFamily("truenas_dataset_available_bytes", "Space available to the dataset", labels=["dataset", "pool"])
        for ds in datasets or []:
            name = str(ds.get("name"))
            if name.count("/") > self.dataset_depth or name.startswith(("boot-pool", "freenas-boot")):
                continue
            if "/.ix-" in name or "/ix-apps" in name or "/.system" in name:
                continue  # TrueNAS internal datasets
            pool = str(ds.get("pool") or name.split("/")[0])
            for family, key in ((used, "used"), (available, "available")):
                value = as_float(ds.get(key))
                if value is not None:
                    family.add_metric([name, pool], value)
        return [used, available]

    def _alerts(self, client: Client) -> list[GaugeMetricFamily]:
        alerts = client.call("alert.list") or []
        counts: dict[str, int] = {}
        for alert in alerts:
            if alert.get("dismissed"):
                continue
            level = str(alert.get("level") or "UNKNOWN")
            counts[level] = counts.get(level, 0) + 1
        family = GaugeMetricFamily("truenas_alerts", "Active (not dismissed) TrueNAS alerts", labels=["level"])
        for level in ("INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY"):
            family.add_metric([level], counts.pop(level, 0))
        for level, count in counts.items():
            family.add_metric([level], count)
        return [family]


def main() -> None:
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    REGISTRY.register(TrueNASCollector())
    port = int(env("PORT", "9814"))
    start_http_server(port)
    LOG.info("listening on :%d", port)
    threading.Event().wait()


if __name__ == "__main__":
    main()
