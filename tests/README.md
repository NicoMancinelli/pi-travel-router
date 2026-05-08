# Tests

## Unit tests

Located in `tests/unit/`. Run locally with:

```bash
# Python tests (requires pytest)
pip install pytest
pytest tests/unit/test_server.py -v

# Bash tests (requires bats-core)
# macOS: brew install bats-core
# Debian/Ubuntu: apt install bats
bats tests/unit/
```

## What is covered

| File | What it tests |
|------|---------------|
| `test_captive_check.bats` | `form_action` extraction pipeline in `captive-check.sh` |
| `test_ups_monitor.bats` | Threshold logic, empty/non-numeric battery level guards |
| `test_failover_watchdog.bats` | Interface label mapping, `get_metric` awk logic |
| `test_server.py` | Input validation, Content-Length guard, MAC/key/country checks |
