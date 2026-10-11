# Review instructions for `truenas_ce`

Home Assistant custom integration (HACS) for TrueNAS; code in `custom_components/truenas_ce/`, tests in `tests/`.
Use this file as a review checklist. Flag a violation only when the diff actually introduces it.

## Home Assistant version compatibility

- [ ] **Tests for version-gated HA APIs use the same gate as the production code.**
  The integration supports HA releases older than the newest API it feature-detects, and CI runs the tests against an *older* HA: `ci.yml` installs `pytest-homeassistant-custom-component==0.13.348`, which pins its own HA version. Tests that touch a newer-only constant/attribute unconditionally break CI there.
  - Production pattern: `sensor_types.py` imports `UnitOfRatio` (HA >= 2026.7) in `try/except ImportError` with a plain `"%"` fallback (`UNIT_PERCENTAGE`); `entity.py` feature-detects instead of version-comparing: `_is_shared_device` (`hasattr(device_entry, "config_entry_id")`, HA >= 2026.8), `_find_entry_device` (entry-scoped `async_get_devices` vs. global `async_get_device`), `_supports_via_device_id` (`inspect.signature` check for `via_device_id`).
  - Test pattern: `tests/test_sensor_types.py` (`_HAS_UNIT_OF_RATIO = hasattr(ha_const, "UnitOfRatio")` + `pytest.mark.skipif`) and `tests/test_entity_setup.py` (`_HAS_ENTRY_SCOPED_LOOKUP = hasattr(dr.DeviceRegistry, "async_get_devices")` with `_requires_entry_scoped_lookup` / `_requires_shared_devices`). Cover the fallback branch too (e.g. `monkeypatch.delattr` + reloading the module), so both sides are tested on whichever HA is installed.
- [ ] **New feature detection carries a removal note** ("Drop this fallback once the minimum HA version is >= X") like the existing ones.

## Defensive API handling

- [ ] **Never assume a response shape.** `TrueNASAPI.query()` returns `None` when a call fails with an `aiotruenas` `TrueNASError` (other exceptions propagate: inside a coordinator poll job, `_run_job` logs them; direct callers such as `async_run_task`, service handlers and entity actions get them unhandled). Callers must check `None`/types (`isinstance`) before indexing or iterating a result.
  - Pass a possibly-`None` result **directly** to `parse_api`. For keyed data (`key`/`key_search`), `None` means "query failed" and keeps the previous snapshot, while an empty list means "nothing left" and prunes it, so substituting `[]`/`{}` for `None` turns a transient failure into removed entities. Keyless calls never prune: both `None` and an empty source only fill missing defaults into the existing values.
- [ ] **New monitored fields go through `parse_api` specs** (`vals` / `ensure_vals` in `apiparser.py`: `/` paths, `type`, `default`, `convert`), not ad-hoc post-processing in the coordinator.
- [ ] **New sensitive keys** (like the existing passwords/passphrases, `api_key`, serials, host/IP addresses, certificates/private keys) are added to `TO_REDACT` in `const.py` so diagnostics stay redacted. The debug logging in `api.py` masks `TO_REDACT` keys via `_redact` (query params, summarized responses and subscription events); any new log line that prints API params or payloads must go through `_redact`/`_summarize_payload` too (never log raw payloads with `%r`); where `api.py` cannot be imported (e.g. `apiparser.py`, which tests load standalone), log only `type(value).__name__`. Exception texts and error reasons go through `scrub_exception` (free-text secrets such as `password=…`, `--token …` or URL credentials are masked by `scrub_text`); new log lines that print an exception must pass it through `scrub_exception` too, and diagnostics wrap `async_redact_data` output in `scrub_strings`.

## Tests

- [ ] **Tests for log output and error messages pin the exact behaviour, not just a fragment.**
  A substring check (`match="..."`, `"..." in caplog.text`) or a check for the expected log level alone still passes when a message gains wrong or contradictory guidance, or when the same call also logs an ERROR/traceback.
  - Compare an error message with the full text or with the module constant it comes from (e.g. `str(excinfo.value) == coordinator_module._SYSTEM_INFO_PERMISSION_DENIED`); where two branches raise similar errors, also assert that the other branch's wording is absent.
  - When a test expects a quieter level (DEBUG/WARNING instead of ERROR), also assert that no higher-level record and no `exc_info` traceback was logged (`not any(r.levelname == "ERROR" or r.exc_info for r in caplog.records)`), including after state transitions such as a failure cause changing.

## Services and UI strings

- [ ] **Actions are declared in `services.yaml`** (Home Assistant reads it for names/fields); never add an `actions.yaml`.
- [ ] **UI strings change in `strings.json` and `translations/en.json` together** - the integration loads `translations/*.json` at runtime, and `tests/test_translation_parity.py::test_en_json_matches_strings_json` requires identical keys and values; `test_no_locale_contains_key_references` rejects `[%key:...%]` references in every locale (HA does not resolve them for custom integrations).
- [ ] **Locale files stay key-compatible with `en.json`**: `.github/validate_translations.py` (CI) fails on missing/extra keys or differing `{placeholder}` sets in any `translations/<lang>.json`.

## GitHub Actions

- [ ] **External actions are pinned to full commit SHAs** with a `# vX.Y.Z` comment (SonarQube S7637); repository-local `uses: ./...` actions are exempt.
  Exception: `home-assistant/actions/hassfest@master` and `hacs/action@main` intentionally track their branch so validation never goes stale.
- [ ] **Every workflow has least-privilege `permissions:`** (start from `contents: read`), set at workflow level (inherited by all jobs) or per job; a job without its own block is fine when the workflow-level one is already minimal.
- [ ] **Secrets are passed via `env:`**, not expanded inline in `run:` (S7636).
