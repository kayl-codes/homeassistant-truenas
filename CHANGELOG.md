# Changelog

This integration is a **maintained fork** of the original
[tomaae/homeassistant-truenas](https://github.com/tomaae/homeassistant-truenas).
This file summarizes everything that has changed under the current maintainer
([kayl-codes](https://github.com/kayl-codes)) since taking over the fork, newest first.

The full, curated notes for each version live on the
[Releases page](https://github.com/kayl-codes/homeassistant-truenas/releases).
Items reference the related issue/PR as `(#NN)` where applicable.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).
Minimum requirements throughout this fork: **Home Assistant 2025.8.0**, **TrueNAS 25.04**.

## [Unreleased]

## [2.7.0] — App Update Progress & TrueNAS 26 Container Support

### Added
- **App update progress tracking:** the app update entity now supports HA's progress feature.
  After starting an `app.upgrade`, the entity polls the TrueNAS job every 2 s, exposes its
  percent as `update_percentage`, mirrors `update_state`/`update_description` in the app data
  and reports a `FAILED`/`ABORTED` job as an error instead of silently finishing. The
  coordinator poll keeps tracking (and clearing) jobs as a safety net, e.g. after an HA restart.
  (#75)

### Fixed
- **TrueNAS 26.0+ containers (#81):** TrueNAS 26 removed the Incus-based `virt.*` API, so every
  poll logged `API error: Method does not exist` and the Containers group stayed empty.
  Containers are now read from `container.query` on 26.0+ (LXC / libvirt) with `container.start`
  / `container.stop` for the actions (restart = stop job + start); TrueNAS 25.x keeps using
  `virt.instance.*`. The 26.x entries carry no memory, image or IP information, so those
  attributes read `0` / description / `unknown` there. Contributed by @cedricziel — many thanks!
  Thanks also to @mmattel for reporting the underlying issue. (#77)
- **App network sensors deleted on every app stop:** a stopped app reports no interfaces in
  `app.stats`, so its per-interface RX/TX sensors are removed from the entity registry (losing
  history and customisations) and re-created on the next start. The last known interfaces are now
  kept as stale entries and the sensors simply become `unavailable` while the app is stopped.
  (#76)
- **Cross-entry app-stats sensor discovery with multiple TrueNAS config entries:** the app-stats
  discovery callback reacted to every config entry's coordinator refresh, not just its own,
  occasionally producing entities for another entry's coordinator and a `Platform truenas does
  not generate unique IDs ... already exists` error. Discovery now ignores refreshes whose
  coordinator instance doesn't match the platform's own. (#78)

### Changed
- **API error-log diagnostics:** TrueNAS API error logs now include which service, event or
  subscription call actually failed, not just the host and error text — makes recurring errors
  traceable back to a specific method from the log alone. (#83)
- **Hardened against malformed API responses:** several code paths that process TrueNAS API
  payloads (keymap generation, app-network sensor resolution, new-entity discovery) now guard
  against unexpected non-dict data instead of risking an `AttributeError`; also dropped a
  redundant duplicate log line for an expected unsubscribe-during-shutdown failure and removed an
  unused sensor attribute. No user-facing behavior change under normal operation. (#79, #80, #82,
  #84)

## [2.6.2] — TrueNAS 25.10+ update fix & device-registry hardening

### Fixed
- **"Unable to start TrueNAS update" on TrueNAS 25.10+ (#72):** TrueNAS 25.10 split the legacy
  `update.update` API method in two — it's now settings-only, and the actual update installer
  moved to a new `update.run` method. Calling the old method still worked for checking updates, but
  clicking **Install** on the system Update entity now failed server-side with
  `[EINVAL] data.reboot: Extra inputs are not permitted`. The integration now detects the running
  TrueNAS version and calls `update.run` on 25.10+, while still using `update.update` on 25.04–25.09
  installs. Thanks to @Regnator for reporting and confirming the fix.

### Changed
- **Device-registry & coordinator internals hardened against upcoming Home Assistant Core
  deprecations:** `DataUpdateCoordinator` now receives `config_entry` explicitly instead of relying
  on its deprecated `ContextVar` fallback, and device linking prefers the newer `via_device_id` over
  `via_device` where the running Home Assistant Core version supports it (with automatic runtime
  feature-detection, so installs on older Core versions are unaffected). No user-facing behavior
  change on any currently-supported Home Assistant version.

## [2.6.1] — Certificate orphaned-statistics & app-stats name fix

### Fixed
- **Orphaned statistics after a certificate update (#61):** certificate entities
  (`certificate_expiry`, `certificate_expiration_time`, `certificate_expired`) are now keyed by
  the certificate's stable `name` instead of TrueNAS's internal `id`, which changes on a
  content-replacing renew/reissue (as opposed to TrueNAS's own passive scheduled auto-renewal).
  Detection is also now scoped to each config entry's own device, so installs with multiple
  TrueNAS instances no longer raise a duplicate Repairs issue for another entry's orphaned
  statistics. Thanks to @janusn for reporting and verifying both release candidates.
- **`UndefinedType._singleton` leaking into app-stats sensor names (#66):** app CPU/memory/
  network/block-I/O sensors could briefly show a literal `UndefinedType._singleton` fragment in
  their name before translations loaded, because a custom `name` override bypassed the safe
  translation-lookup helper used by every other entity. Thanks to @testuser7 for the fix.

## [2.5.2] — Read-only API key log noise fix

### Fixed
- **Repeated ERROR-level log spam with a Read-Only Admin API key (#46):** calls like
  `smb.status` return an `EACCES` permission error under a read-only-scoped key. That's an
  expected, permanent condition for that key, not an integration bug, so every ~60s poll no
  longer logs it at ERROR with a full traceback — it's now logged at DEBUG instead. Other call
  errors are unaffected and still log at ERROR. Thanks to @jkadin for reporting.

## [2.5.1] — Sensor platform hotfix

### Fixed
- **Sensor platform failed to load entirely (regression from 2.5.0):** the `snapshottask`
  sensor description referenced `TrueNASSnapshotTaskSensor`, but the class was missing from
  `sensor.py`'s dispatcher map. Any config entry monitoring the Snapshots group hit a
  `KeyError` during platform setup, aborting entity creation for **all** sensors (not just
  snapshot tasks) and leaving them "Unavailable". Restored the missing dispatcher entry and
  added a real-`hass` regression test that exercises the dispatcher end to end.

## [2.5.0] — App Monitoring & Automatic Reauthentication

### Added
- **App resource monitoring (#38):** each running TrueNAS app now exposes live CPU, memory,
  network RX/TX and block I/O sensors, updated event-driven via TrueNAS's `app.stats`
  subscription. Toggle the group under *Settings → TrueNAS → Configure → Monitored groups →
  Apps*.
- **Automatic reauthentication (#28):** if the stored TrueNAS API key becomes invalid or is
  revoked, the integration now raises a guided Repair flow to enter a new key — no more silent
  "unavailable" state and no need to remove/re-add the integration.

### Changed
- **Async API client — now powered by `aiotruenas` (#27):** the integration's TrueNAS API layer
  was rewritten from a synchronous, thread-based WebSocket client to a native `asyncio` client
  built on the new [`aiotruenas`](https://github.com/kayl-codes/aiotruenas) library (a standalone,
  independently maintained TrueNAS client). This also moves the integration onto TrueNAS's modern
  JSON-RPC 2.0 **`/api/current`** endpoint, replacing the legacy `/websocket` endpoint the old
  client spoke. Purely an internal architecture change — connection behavior, error messages,
  sensors and actions all work exactly as before. If you run TrueNAS behind a reverse proxy with a
  path-specific bypass rule, update it from `/websocket` to `/api/current` (see
  [Remote access, reverse proxies & Cloudflare](README.md#remote-access-reverse-proxies--cloudflare)).
- **Action error reporting (#40):** entity actions (VM/container/app control, service control,
  cloudsync, snapshots, reboot/shutdown, scrub, alerts) now raise a proper error when the
  underlying TrueNAS call actually fails, instead of failing silently.
- **Quality & test hardening:** completed the Home Assistant **Bronze** and **Silver**
  quality-scale tiers (`mypy --strict` typing, 99% CI test coverage, proper action-error
  handling).

### Fixed
- **Scrub timestamp crash on never-scrubbed pools (#35, #39):** timestamp sensors no longer
  error out for pools that have never been scrubbed (or while a scrub is actively running) —
  both report cleanly as *unknown* until a real timestamp exists.

### Requirements
- Dependency bump: `aiotruenas>=1.1.0`.

## [2.4.1] — Deprecated update-listener fix

### Fixed
- **HA 2026.12 deprecation — config entry update listener (#24):** the options flow now
  reloads the config entry via `OptionsFlowWithReload` instead of a manual
  `add_update_listener` callback, removing the `custom integration 'truenas' has an update
  listener and should use it for scheduling a reload` warning logged on every options save
  (would have stopped working in Home Assistant 2026.12.0). No functional change — saving
  options or reconfiguring the integration still reloads it exactly as before.

### Changed
- **Minimum Home Assistant version raised to 2025.8.0** (from 2024.8.0): required by the
  `OptionsFlowWithReload` API used in the fix above.

## [2.4.0] — Cron job controls & HA 2026.7 compatibility

### Added
- **Cron job switch & run button (#22):** each TrueNAS cron job is now exposed as a dedicated
  device under **TrueNAS Cron jobs**. Two entities per job: an **Enabled** switch (enable/disable
  the job via `cronjob.update`) and a **Run** button that triggers the job immediately on demand
  (`cronjob.run`). The group can be toggled under *Settings → TrueNAS → Configure → Monitored
  groups → Cron Jobs*. On an existing install, add **Cron Jobs** once after upgrading.

### Fixed
- **HA 2026.7 deprecation — `UnitOfRatio.PERCENTAGE`:** percentage-based sensors (CPU usage,
  memory usage, pool fragmentation, UPS charge/load, scrub progress, ARC hit rate) now declare
  `native_unit_of_measurement=UnitOfRatio.PERCENTAGE` instead of the deprecated `PERCENTAGE`
  string constant, as required by Home Assistant 2026.7+.

## [2.3.0] — Scrub controls, disk icons & dataset passphrase store

### Added
- **Disk-type icons:** disk temperature sensors now show a type-specific icon based on
  the storage medium reported by TrueNAS (`disk.query` → `type`): a platter icon
  (`mdi:harddisk`) for HDDs, a chip icon (`mdi:chip`) for SSDs and SEDs, and a PCIe
  card icon (`mdi:expansion-card-variant`) for NVMe drives.
- **Stored dataset passphrases (#11):** passphrases for encrypted datasets can now be
  stored securely in the HA config entry. Enter `DatasetName#Passphrase` pairs (one per
  line) under *Settings → Devices & Services → TrueNAS → Configure* → **Add/update
  dataset passphrases**. Stored passphrases are used automatically by `dataset_unlock`
  — no need to type the passphrase each time. Stored passphrases survive HA restarts
  and can be removed individually via the new `passphrase_remove` action.
- **Certificate expiry in years:** the "time until expiry" sensor now displays the
  value in **years** (unit `a`) when 365 or more days remain, making long-lived
  self-signed certificates readable at a glance. The unit switches back to days
  automatically as the expiry date approaches.

### Fixed
- **Pool scrub button now reliably starts a scrub (#9):** the previous implementation
  called `pool.scrub.run`, which silently skips a scrub when the last run is within
  the task's configured threshold (default 35 days). Replaced with `pool.scrub.scrub`
  (`action=START`), which bypasses the threshold check entirely and starts the scrub
  immediately.

## [2.2.0] — Alert actions & disk temperature history fix

### Added
- **Alert dismiss / restore (#8):** new domain-level actions `alert_dismiss`, `alert_restore`
  and `alert_list`. Dismiss an active alert by UUID, restore a dismissed one, or retrieve a
  structured list of all active alerts (with selectable properties) directly from Developer
  Tools → Actions. All three actions accept an optional `config_entry` selector when multiple
  TrueNAS instances are configured.

### Fixed
- **Disk temperature history no longer frozen (#16):** when the netdata temperature source was
  unavailable, the fallback query was only triggered for disks that already had `null` in the
  previous poll, not for all disks. Fixed so the disk-temperatures API is queried whenever
  netdata returns nothing. Additionally, disk temperature sensors now use `force_update=True`
  so each poll result is written to the HA recorder even when the value is unchanged — history
  graphs no longer show gaps or a frozen last-known value.
- **Removed deprecated `pytz` dependency (SonarCloud S6890):** `apiparser.py` now uses the
  stdlib `datetime.UTC` constant (Python 3.11+, our target is 3.13) instead of
  `from pytz import utc`. No user-visible change; `pytz` was never listed as a runtime
  requirement.

## [2.1.0] — Certificate expiry, ARC hit ratio & system refresh

### Added
- **Certificate expiry monitoring (#6):** each TrueNAS certificate is exposed as a sensor
  showing the expiry datetime, a companion sensor with the days remaining, and a binary
  sensor that turns `on` when the certificate has expired. All three carry the certificate
  name, subject, issuer and valid-from date as attributes.
- **ARC hit ratio (#7):** a new `arc_hit_ratio` sensor (unit `%`) shows the ZFS ARC cache
  effectiveness, complementing the existing ARC size sensor.
- **System Refresh action (#10):** a new `system_refresh` domain-level action triggers an
  immediate out-of-schedule coordinator update — useful in automations or when you need
  fresh data right after a TrueNAS change. A dedicated **System Refresh** button also
  appears on the system device page.

---

## [2.0.0] — Independent project: rename to `truenas_ce`, dataset lock/unlock

> **Breaking change.** The integration **domain** changed from `truenas` to
> `truenas_ce` as the project became an independent, standalone integration.
> Updating is safe: the old code stays in place, and when you add **TrueNAS CE**
> your existing entities, **history and long-term statistics are migrated
> automatically** (one-click config takeover, with a Repairs-based rollback while
> the old integration is still installed). See [docs/migration.md](docs/migration.md).

### Added
- **Lock / unlock encrypted datasets (#53):** new `dataset_lock` and `dataset_unlock`
  actions (target a dataset sensor) for passphrase-encrypted datasets, plus new
  dataset attributes **Encrypted**, **Locked** and **Encryption key format**
  (passphrase/key). Unlock failures (e.g. a wrong passphrase) surface as a clear
  error, and locking/unlocking a non-encrypted dataset is rejected up front.
- **Community-Edition migration (config takeover):** adding **TrueNAS CE** detects an
  existing `truenas` integration and imports its host, API key and options in one
  click; existing entity IDs, history and statistics are adopted, and a
  post-migration notification summarizes what was migrated.
- **Rollback safety net:** a diagnostic button opens a **Repairs** confirmation flow
  that undoes the migration (re-enables the old integration) while it is still
  installed; a `.storage` backup snapshot is written before any change.

### Changed
- **The project is now independent** (de-forked) and renamed to `truenas_ce`. The
  display name stays **TrueNAS** and entity IDs are preserved across the migration.
- CI: the release upload action was bumped to a Node 24 version (SHA-pinned) and
  curated release notes are now preserved on publish (#52).

### Notes
- ⭐ **The repository was re-created** to become a standalone (non-fork) project. If
  you had **starred** the old repository, please **re-star** the new one — the old
  stars stay on the legacy repository and don't carry over.

## [1.9.1] — Orphaned statistics cleanup, reverse-proxy detection & translations

### Added
- **German translation + completed locales (#47):** added a full German (`de`) translation and
  brought the existing Spanish, Russian, Slovak and Brazilian-Portuguese files back to full
  parity with English (they were missing ~43% of strings, including the whole options flow and
  the Repairs texts). A new CI check now validates every locale against `en.json` (keys +
  `{count}`/`{port}` placeholders) so they can't drift again.
- **Reverse-proxy / SSO detection (#45, #46):** when the WebSocket handshake is intercepted by a
  reverse proxy or SSO portal (e.g. Cloudflare Access), setup now shows a clear
  *"intercepted by a reverse proxy or SSO portal"* message instead of a misleading
  *"invalid API key"* / *"unknown error"*.

### Fixed
- **Orphaned long-term statistics can now be cleaned up (#44):** after an entity-id rename the
  recorder can leave the old `sensor.truenas_*` statistics behind (they show as "no state
  available" in *Developer Tools → Statistics*). The integration now detects these each poll
  and surfaces a **Repairs** issue (Fix → delete, or Ignore) plus a diagnostic
  **"Clean up orphaned statistics"** button that is available whenever orphans exist.
- **Orphan detection now covers custom instance names (#48):** statistics from an instance whose
  name slug merges the domain into a longer token (e.g. `sensor.truenasviacfnoauth_*`) are now
  recognised too, so the cleanup button no longer stays greyed out for them.
- **Host field accepts pasted URLs (#45, #46):** a leading `https://`, a path or a trailing slash
  in the *Host* field are now stripped automatically instead of failing the setup.

### Documentation
- **Reverse proxies & required permissions (#45):** README now documents that the integration
  must reach the TrueNAS host directly (LAN/VPN) — an auth gateway in front of it cannot be
  bypassed — and that the API key's user needs the appropriate role. The Translation section was
  updated to reflect that translations are maintained directly in this repository.

## [1.9.0] — Run buttons, action descriptions & robustness

### Added
- **Run buttons (new `button` platform):** one-tap **Run** buttons for snapshot, rsync,
  replication and cloudsync tasks appear on each task's device page — no need to call an
  action from Developer Tools or build a button card.
- **`snapshottask_run` action:** run a periodic snapshot task on demand (target the
  snapshot task sensor), mirroring `rsync_run` / `replication_run`.
- **Instant run feedback:** triggering a snapshot / rsync / replication / cloudsync run
  (via button or action) now optimistically sets the sensor to `RUNNING` right away, so a
  fast task that finishes between two polls still shows the trigger worked; the next regular
  poll re-syncs to the real TrueNAS state.

### Fixed
- **Action descriptions now show up in Home Assistant:** the descriptions lived in
  `actions.yaml`, which Home Assistant does not read — it reads `services.yaml` (which was
  empty since the 1.4.1 rename). All action names, descriptions and fields are now defined in
  `services.yaml`, so they appear in *Developer Tools → Actions*. Dead `jail_*` entries were
  dropped and `dataset_snapshot` got a clearer display name ("Create dataset snapshot").
- **Clear error for unsupported actions:** targeting an action at an entity type that does not
  support it (e.g. `service_restart` on an app) now raises a descriptive error instead of a
  bare "Unknown error".
- **No more `KeyError` crashes on a transient API hiccup:** when a query times out / the
  WebSocket changes mid-query and a data group is briefly emptied, entities now degrade to an
  unknown state instead of raising `KeyError` while writing their state.

### Documentation
- New **Actions** reference table in the README. Added notes that a fast replication/snapshot
  run may finish within the poll interval and therefore not surface the transient `RUNNING`
  state (the persistent state always matches the WebUI).

## [1.8.1] — Multi-instance unique-ID fix

### Fixed
- **Unique-ID error spam with multiple TrueNAS instances (#33):** With more than one
  TrueNAS config entry, the global entity-discovery dispatcher signal made every
  instance's platform also try to create the *other* instance's entities, flooding the
  log with `Platform truenas does not generate unique IDs … already exists` (endlessly,
  since the rejected entities never enter the platform and are retried each refresh).
  Each platform now ignores refreshes coming from other config entries.
  Single-instance setups were unaffected.

## [1.8.0] — Directory Services

### Added
- **Directory Services (#22):** Monitor the TrueNAS Directory Services connection
  (Active Directory / LDAP / IPA) via the unified `directoryservices.*` API
  (TrueNAS 25.04+). A connectivity binary sensor reports whether the service is
  healthy, and a companion status sensor exposes the raw state (`HEALTHY`,
  `FAULTED`, …). Domain, Kerberos realm, site, account-cache and DNS-update
  settings are exposed as attributes. The entity only appears when a directory
  service is actually configured and enabled. New monitored group
  **Directory Services** (enabled by default).

---

## [1.7.0] — TrueNAS Containers + Restart Actions

### Added
- **Containers (#26):** Each TrueNAS Container (Incus instance) is a binary sensor
  (running on/off) with type, status, CPU, memory, autostart, image and IP address
  attributes, grouped under their own device.
- **Start / Stop / Restart** actions for containers (`container_start/stop/restart`,
  `virt.instance.*`); the live state is checked before start/stop.
- **`vm_restart`** action — VMs and containers now share the same start/stop/restart trio.

### Changed
- Robust entity discovery: the "seen" set is derived from the platform's live
  entities (recreate on startup, no re-add spam, runtime-removed objects reappear).
- Monitored-group checks use shared constants; container CPU normalized to a number;
  virt stop options centralized; mis-shaped API responses are logged.

## [1.6.1] — Stability: Entity Spam, Blocking Call & Replication State

### Fixed
- Entities no longer stuck `unavailable` on startup (late fix to the #33 discovery rework).
- No more "non-unique ID" log spam (#33): discovery adds only genuinely new entities.
- No blocking call in the event loop (#33): the WebSocket SSL context is built lazily.
- Replication task state (#34): read from the task's persistent `state` object
  (matching the WebUI), with the last job's state as a fallback.

## [1.6.0] — Options Flow, Live Interface Values & Masked Credentials

### Added
- **Options flow (#14):** Configure under *Settings → Devices & Services → TrueNAS →
  Configure*, applied immediately via reload — poll interval (5/10/30/60/120/300 s),
  monitored groups, behaviour toggles (skip disabled cronjobs, hide RX/TX for
  disconnected NICs) and the GB/GiB data-size unit.

### Changed
- Live interface throughput averaged over a window matching the poll interval.
- Empty devices are cleaned up when a group/interface is removed.
- Disabling a group skips its API query entirely.

### Security
- API key field is now a masked password input (setup + reconfigure).

## [1.5.5] — Hotfix: Phantom App Image Update

### Fixed
- Phantom "Update available" for catalog apps (#31): the `image_updates_available`
  fallback is now correctly gated on `custom_app`, so catalog apps rely solely on
  `upgrade_available` (matching TrueNAS' own state).

## [1.5.4] — Network Group, Boot-Pool & Task Actions

### Added
- Dedicated **"TrueNAS Network"** device for per-interface RX/TX sensors (#25).
- Per-interface link connectivity binary sensor (up/down), even for down interfaces.
- Boot-pool exposed as a regular pool via `boot.get_state` (#23).
- Status sensors for each rsync task in a dedicated **"Rsync tasks"** group (#16).
- `rsync_run` and `replication_run` on-demand actions, guarded against running jobs (#16).

### Changed
- Automatic orphan cleanup of entities whose TrueNAS object no longer exists
  (transient empty fetches never wipe a group; skipped unless the last update succeeded).
- `get_systeminfo` runs before the concurrent jobs, so the first poll has correct values.
- All GitHub Actions pinned to commit SHAs, least-privilege permissions, off Node.js 20.

### Notes
- Closed as not feasible: UPS power/energy + full NUT variables (#17) and SMART test
  results (#15) are not exposed by the TrueNAS JSON-RPC API.

## [1.5.3] — ARC & raidz Fixes, Live Entity Discovery

### Added
- Live entity discovery: new objects appear automatically without a reload.

### Fixed
- ARC sensor no longer stuck at 0; corrected size/allocated for raidz pools.
- Traffic sensors hidden for interfaces whose link is down; null-safe pool totals.

## [1.5.2] — Pool Capacity, UPS Sensors & Auto-Scaled Units

### Added
- Pool capacity sensors (free / size / allocated).
- UPS monitoring: charge, runtime, load, voltage, current, frequency, temperature.
- Auto-scaled data-size units (MB/GB/TB/PB or MiB/GiB/TiB/PiB) per GB/GiB preference.

### Changed
- Pool figures derived from the root dataset, matching the UI for raidz layouts.
- Modern SSL context defaults; generic default host (removed hardcoded IP).
- `system_uptime` uses the `UPTIME` device class; CI moved to Node.js 24.

## [1.5.1] — Cloudsync Control, Disk Health & Stability

### Added
- Cloudsync start/stop switch entities.
- Per-disk health sensor.

### Changed
- Hardened WebSocket layer; fixed leaked connections; more defensive API parsing.

## [1.5.0] — Massive Core Overhaul & New Features

### Added
- TrueNAS Alerts diagnostic sensor (messages + severity).
- SMB connections diagnostic sensor.
- Setup auto-discovery of the TrueNAS IP via local DNS.

### Changed
- Migrated to the modern TrueNAS DDP WebSocket protocol (`auth.login_with_api_key`).
- Pre-flight TCP port check (IPv6-ready) and smarter handshake/query timeouts.
- Dual-lock thread-safety (`_lock` + `_io_lock`); exception-type-based error handling.

### Fixed
- Service controls use modern `service.start/stop/restart/reload`.
- Robust version parsing; reduced stat-graph log spam; entity naming via
  `_attr_has_entity_name`; redundant read-only service binary sensors disabled by default.

### Notes
- Minimum TrueNAS version raised to **25.04** (modernized WebSocket API).

## [1.4.2] — Logic Fixes & API Stability

### Fixed
- RPC errors now preserve detailed backend context in the logs.
- Snapshot fallback (`pool.snapshot.create` → `zfs.snapshot.create`) triggers correctly.
- `cronjob_skip_disabled` is respected during updates.
- Fixed a mutable default-argument bug in the API query method.

## [1.4.1] — TrueNAS SCALE 25.10 & HA 2024.8+ Compatibility

### Changed
- Migrated `services.yaml` → `actions.yaml` (HA "Services" → "Actions").
- Minimum Home Assistant version: **2024.8.0**.
- Replaced Flake8/Black with **Ruff**; native Bandit security checks; cleaned CI.

### Fixed
- Handle JSON-RPC parsing errors to prevent crashes on unexpected API formats.
- Modern type hints and `.get()` fallbacks to avoid `KeyError` crashes.

[2.5.1]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/v2.5.1
[2.5.0]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/v2.5.0
[2.4.1]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/v2.4.1
[2.4.0]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/v2.4.0
[2.3.0]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/v2.3.0
[2.2.0]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/v2.2.0
[2.1.0]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/v2.1.0
[2.0.0]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/2.0.0
[1.9.1]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.9.1
[1.9.0]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.9.0
[1.8.1]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.8.1
[1.8.0]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.8.0
[1.7.0]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.7.0
[1.6.1]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.6.1
[1.6.0]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.6.0
[1.5.5]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.5.5
[1.5.4]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.5.4
[1.5.3]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.5.3
[1.5.2]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.5.2
[1.5.1]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.5.1
[1.5.0]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.5.0
[1.4.2]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.4.2
[1.4.1]: https://github.com/kayl-codes/homeassistant-truenas/releases/tag/1.4.1
