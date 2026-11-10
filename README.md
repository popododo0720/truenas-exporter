# truenas-exporter

Small Prometheus exporter for the TrueNAS SCALE JSON-RPC WebSocket API (tested against TrueNAS 25.10).

It exports what TrueNAS's built-in Netdata → Graphite reporting does not:

| Metric | Labels | Source |
|---|---|---|
| `truenas_pool_size_bytes`, `truenas_pool_allocated_bytes`, `truenas_pool_free_bytes` | `pool` | `pool.query` |
| `truenas_pool_healthy` (1/0), `truenas_pool_status` (always 1) | `pool`, `status` | `pool.query` |
| `truenas_pool_vdev_errors` (summed over leaf devices) | `pool`, `type` = read/write/checksum | `pool.query` topology |
| `truenas_pool_scan_end_timestamp_seconds`, `truenas_pool_scan_errors`, `truenas_pool_scan_state` | `pool`, `function` (SCRUB/RESILVER), `state` | `pool.query` scan |
| `truenas_disk_temperature_celsius`, `truenas_disk_size_bytes` | `disk`, `serial` | `disk.temperatures`, `disk.query` |
| `truenas_disk_info` (always 1) | `disk`, `serial`, `model`, `pool` | `disk.query` |
| `truenas_dataset_used_bytes`, `truenas_dataset_available_bytes` | `dataset`, `pool` | `pool.dataset.query` (depth ≤ `TRUENAS_DATASET_DEPTH`) |
| `truenas_alerts` (not dismissed) | `level` | `alert.list` |
| `truenas_up`, `truenas_scrape_duration_seconds`, `truenas_collector_error` | `collector` | exporter |

Each scrape opens one WebSocket connection, logs in with `auth.login_with_api_key`, runs the queries and disconnects. A failing collector sets `truenas_collector_error{collector=...}` to 1 and does not hide the others.

## Configuration

| Variable | Default | |
|---|---|---|
| `TRUENAS_URL` | `wss://truenas.home.arpa/api/current` | JSON-RPC WebSocket endpoint |
| `TRUENAS_API_KEY` / `TRUENAS_API_KEY_FILE` | — | API key of a user with the **READONLY_ADMIN** privilege |
| `TRUENAS_INSECURE` | `false` | `true` skips TLS verification (TrueNAS self-signed certificate) |
| `TRUENAS_CA_FILE` | — | CA bundle to verify the TrueNAS certificate |
| `TRUENAS_TIMEOUT` | `20` | Seconds per connection and per call |
| `TRUENAS_DATASET_DEPTH` | `2` | Export datasets up to this many levels below the pool |
| `PORT` | `9814` | Listen port for `/metrics` |
| `LOG_LEVEL` | `INFO` | |

## Image

`ghcr.io/popododo0720/truenas-exporter`, built by `.github/workflows/image.yaml`: `X.Y.Z` and `X.Y` from `vX.Y.Z` tags, `main` and `sha-<short>` from the main branch.

## Development

```
pip install -r requirements.txt
python -m unittest discover -s tests -v
```
