# TrueNAS Integration
[![GitHub release (latest by date)](https://img.shields.io/github/v/release/kayl-codes/homeassistant-truenas?style=plastic)](https://github.com/kayl-codes/homeassistant-truenas/releases)
[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=plastic)](https://github.com/hacs/integration)
[![Project Stage](https://img.shields.io/badge/project%20stage-development-yellow.svg?style=plastic)](#)
[![GitHub all releases](https://img.shields.io/github/downloads/kayl-codes/homeassistant-truenas/total?style=plastic)](https://github.com/kayl-codes/homeassistant-truenas/releases)

[![GitHub commits since latest release](https://img.shields.io/github/commits-since/kayl-codes/homeassistant-truenas/latest?style=plastic)](https://github.com/kayl-codes/homeassistant-truenas/commits/master)
[![GitHub commit activity](https://img.shields.io/github/commit-activity/m/kayl-codes/homeassistant-truenas?style=plastic)](https://github.com/kayl-codes/homeassistant-truenas/graphs/commit-activity)
[![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/kayl-codes/homeassistant-truenas/ci.yml?style=plastic)](https://github.com/kayl-codes/homeassistant-truenas/actions)

![English](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/flags/us.png)

![TrueNAS Community Edition](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/header-ce.png)

> **Note:** This is an independent integration, originally forked from the
> [TrueNAS integration by tomaae](https://github.com/tomaae/homeassistant-truenas)
> and now maintained as a standalone project. See the **[Changelog](CHANGELOG.md)**
> for a summary of everything that has changed per version.

> ### ⚠️ Version 2.0.0 — breaking change & independent project
> The Home Assistant **domain changed from `truenas` to `truenas_ce`** as this
> project became independent. Your data is safe: after updating, add **“TrueNAS CE”**
> once — host, API key, options, **entities, history and long-term statistics are
> migrated automatically** (one-click config takeover, with a Repairs-based rollback
> while the old integration is still installed). Full guide:
> **[docs/migration.md](docs/migration.md)**.
>
> ⭐ The repository was **re-created** as a standalone (non-fork) project. If you
> starred the old repo, please **re-star this one** — old stars stay on the legacy
> repository and don't carry over.

Monitor and control your TrueNAS device from Home Assistant.
 * Monitor System (CPU, Load, Memory, Temperature, **ARC Hit Ratio**, Uptime)
 * Monitor Network interfaces in a dedicated device group (RX/TX traffic + link connectivity per NIC)
 * Monitor Disks
 * Monitor Pools (including the boot-pool)
 * Monitor Datasets
 * Monitor and run Replication Tasks
 * Monitor and run Rsync Tasks
 * Monitor Snapshot Tasks
 * Control and Monitor Services
 * Control and Monitor Virtual Machines (start / stop / restart)
 * Control and Monitor Containers (Incus instances: start / stop / restart)
 * **Monitor Apps** (CPU, RAM, Network RX/TX, Block I/O — live event-based statistics per running app)
 * Control and Monitor Cloudsync
 * Monitor Directory Services (Active Directory / LDAP / IPA status)
 * **Monitor Certificate Expiry** (expiration time, days remaining, expired status)
 * **Monitor, dismiss and restore Active Alerts** (list all alerts, dismiss by UUID, restore dismissed)
 * Create a Dataset Snapshot
 * Lock / unlock encrypted Datasets and store passphrases for automated unlock
 * **Refresh coordinator data on demand** (System Refresh action)
 * Update Sensor
 * Reboot and Shutdown TrueNAS system
 * Configurable poll interval, data unit, behaviour and per-group sensor toggles (Options)


# Features
## Pools
Monitor status for each TrueNAS pool.

![Pools Health](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/pool_healthy.png)
![Pools Free Space](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/pool_free.png)

Each pool also exposes **scrub controls** (start, pause, resume, stop) as buttons
and **scrub diagnostics** (state, progress, start/end timestamps, errors) as sensors.

![Pool Controls](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/pool_controls.png)
![Pool Diagnostics](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/pool_diag.png)

## Datasets
Monitor usage and attributes for each TrueNAS dataset.

![Datasets](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/dataset.png)

> **Datasets** is a monitored group (enabled by default). You can disable it under
> *Settings → Devices & Services → TrueNAS → Configure → Monitored groups*.

## Disks
Monitor temperature and attributes for each TrueNAS disk.
Disk icons reflect the storage medium at a glance: a platter icon for **HDDs**,
a chip icon for **SSDs / SEDs** and a PCIe card icon for **NVMe** drives.

![Disks](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/disk.png)

## Network
Each network interface is grouped under its own dedicated device. The integration
exposes RX/TX traffic sensors and a link connectivity binary sensor per interface.
Traffic sensors are created for active interfaces; the link sensor is always
available so disconnected interfaces can be monitored too. Sensors for interfaces
that no longer exist are cleaned up automatically on startup.

![Network](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/network.png)

## Virtual Machines
Control and monitor status and attributes for each TrueNAS virtual machine.
Start, stop and restart are available through the `vm_start`, `vm_stop` and `vm_restart` actions.

![Virtual Machines](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/vm.png)

> **Virtual Machines** is a monitored group (enabled by default). You can disable it under
> *Settings → Devices & Services → TrueNAS → Configure → Monitored groups*.

## Containers
Monitor each TrueNAS **Container** (Incus instance, TrueNAS 25.04+) as a binary sensor,
with type, status, CPU, memory, autostart, image and IP address as attributes.
Start, stop and restart are available through the `container_start`, `container_stop`
and `container_restart` actions (target the container's binary sensor).

> **Containers** is a monitored group (enabled by default). You can disable it under
> *Settings → Devices & Services → TrueNAS → Configure → Monitored groups*.
> On an existing install, enable **Containers** once after upgrading.
>
> Note: a restart is a background job, so the brief down-state may not be sampled by the
> poll — the steady state is always reported correctly.

## Apps
Monitor each running TrueNAS **app** (Kubernetes workload) with live, event-based statistics:
- **CPU usage** (%)
- **Memory** (bytes)
- **Network RX/TX** per interface (bytes/sec)
- **Block I/O Read/Write** (bytes)

Sensors are created and removed automatically as apps are deployed or removed. Each app
gets its own device, and network metrics are exposed per network interface. Stats are
updated via TrueNAS `app.stats` event subscriptions rather than polling, so they reflect
the current state without waiting for the next poll interval.

> **Apps** is a monitored group (enabled by default). You can disable it under
> *Settings → Devices & Services → TrueNAS → Configure → Monitored groups*.

## Cloudsync
Control and monitor status and attributes for each TrueNAS cloudsync task.
Cloudsync control is available through actions.

![Cloudsync](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/cloudsync.png)

> **Cloudsync** is a monitored group (enabled by default). You can disable it under
> *Settings → Devices & Services → TrueNAS → Configure → Monitored groups*.

## Replication Tasks
Monitor status and attributes for each TrueNAS replication task.
Replication tasks can be started on demand through the `replication_run` action.

![Replication Tasks](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/replication.png)

> **Replication** is a monitored group (enabled by default). You can disable it under
> *Settings → Devices & Services → TrueNAS → Configure → Monitored groups*.
>
> Note: triggering a run on demand (the **Run** button or `replication_run`) shows
> `RUNNING` immediately and re-syncs to the real state on the next poll. A **scheduled**
> run that finishes between two polls may only be sampled in its final state (e.g.
> `FINISHED`) — the persistent state always matches the TrueNAS WebUI.

## Rsync Tasks
Monitor status and attributes for each TrueNAS rsync task.
Rsync tasks can be started on demand through the `rsync_run` action.

> **Rsync Tasks** is a monitored group (enabled by default). You can disable it under
> *Settings → Devices & Services → TrueNAS → Configure → Monitored groups*.

## Snapshot Tasks
Monitor status and attributes for each TrueNAS snapshot task.
Periodic snapshot tasks can be started on demand through the `snapshottask_run` action
(target the snapshot task sensor).

![Snapshot Tasks](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/snapshottask.png)

> **Snapshot Tasks** is a monitored group (enabled by default). You can disable it under
> *Settings → Devices & Services → TrueNAS → Configure → Monitored groups*.
>
> Note: triggering a run on demand (the **Run** button or `snapshottask_run`) shows
> `RUNNING` immediately and re-syncs to the real state on the next poll. A **scheduled**
> run that finishes between two polls may only be sampled in its final `state` (e.g.
> `FINISHED`); the task's `datetime` / last snapshot is the reliable run evidence —
> TrueNAS itself shows no live "running" feedback for these tasks either.

## Cron Jobs
Monitor and control each TrueNAS cron job. Each job is exposed as a dedicated device with two entities:
- **Enabled switch** — enable or disable the job on TrueNAS directly from HA
- **Run button** — trigger the job immediately on demand, independent of its schedule

> **Cron Jobs** is a monitored group (enabled by default). You can disable it under
> *Settings → Devices & Services → TrueNAS → Configure → Monitored groups*.
> On an existing install, add **Cron Jobs** once after upgrading.

## Dataset Snapshot
Create an **on-demand** ZFS snapshot of a dataset through the `dataset_snapshot` action
(target a dataset sensor) — taken immediately, independent of any periodic snapshot task.
The snapshot name is generated automatically in ISO datetime format with microseconds and
a `custom-` prefix.

![Snapshot UI](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/snapshot_ui.png)
![Snapshot YAML](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/snapshot_yaml.png)

## Stored Dataset Passphrases
Passphrases for encrypted datasets can be stored securely inside the HA config entry so
that `dataset_unlock` works without prompting for a passphrase every time.

**How to store a passphrase:**
Go to *Settings → Devices & Services → TrueNAS → Configure* and enter one or more
`DatasetName#Passphrase` pairs in the **Add/update dataset passphrases** field (one per
line). Example:

```
tank/encrypted#MySecret
tank/backup#AnotherSecret
```

- Stored passphrases survive HA restarts and integration reloads.
- The dataset name must match the full path as reported by TrueNAS (e.g. `tank/encrypted`).
- To remove a stored passphrase, call the `passphrase_remove` domain action with the
  dataset path — no need to re-open the config flow.
- Passphrases are stored in the HA `.storage` entry for this integration (encrypted at rest
  by HA's config-entry storage).

## Services
Control and monitor status and attributes for each TrueNAS service.
Service control is available through actions.

![Services](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/service_1.png)
![Services Control](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/service_2.png)

## Directory Services
Monitor the TrueNAS **Directory Services** connection (Active Directory, LDAP or IPA;
TrueNAS 25.04+ unified API). A connectivity binary sensor reports whether the directory
service is **healthy**, and a companion status sensor exposes the raw state
(`HEALTHY`, `FAULTED`, …). Domain, Kerberos realm, site, account-cache and DNS-update
settings are available as attributes.

The entity only appears when a directory service is actually configured and enabled,
so systems without AD/LDAP get no entity.

> **Directory Services** is a monitored group (enabled by default). You can disable it under
> *Settings → Devices & Services → TrueNAS → Configure → Monitored groups*.

## Certificates
Monitor **TrueNAS certificate expiry** and status. Each certificate is exposed as:
- **Expiration time sensor** — Timestamp when the certificate expires
- **Time until expiry sensor** — Days remaining until the certificate expires (switches to **years** automatically when 365+ days remain, so long-lived self-signed certificates are readable at a glance)
- **Expired status binary sensor** — Problem-class indicator when a certificate is already expired or no longer valid

Use these sensors in automations to send notifications before certificates expire, or to trigger renewal workflows.

## System Refresh
Force an immediate refresh of all TrueNAS data without waiting for the regular poll interval (default: 60 seconds).
Available through:
- **`system_refresh` action** — Target the System uptime sensor to trigger a refresh from an automation
- **Diagnostic "Refresh data" button** — One-tap refresh on the TrueNAS device page

Useful when you run an action (e.g., dataset lock/unlock, service restart) and need to capture the updated state immediately in an automation.

## Alerts
Dismiss and restore TrueNAS alerts, or list all active alerts with customizable properties.

All alert actions are **domain-level services** (no target entity needed) and include an optional `config_entry` selector when multiple TrueNAS instances are configured.

**Available actions:**
- **`alert_list`** — List all active TrueNAS alerts with selectable properties (response shown in *Developer Tools → Actions* response panel)
  - Optional `config_entry` — Select which TrueNAS instance (defaults to the only/first instance if not specified)
  - Optional `properties` — Comma-separated list of properties to include (e.g., `uuid,formatted,level`), or `*` for all properties. Default: `uuid,formatted`
- **`alert_dismiss`** — Dismiss a TrueNAS alert by UUID
  - Optional `config_entry` — Select which TrueNAS instance
  - Required `uuid` — UUID of the alert (visible in `alert_list` response or as state attributes)
- **`alert_restore`** — Restore (un-dismiss) a previously dismissed TrueNAS alert by UUID
  - Optional `config_entry` — Select which TrueNAS instance
  - Required `uuid` — UUID of the alert

Example:
```yaml
# List all active alerts with all properties
action: truenas_ce.alert_list
data:
  properties: "*"
```

## Diagnostics
Monitor overall system health and active alerts directly from the device page. The integration provides a dedicated diagnostic sensor that automatically detects any disk or pool issues.

![Diagnostics](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/diagnostics.png)

## Reboot and Shutdown
Reboot or shut down a TrueNAS system.
Power control is available through actions.
Target system uptime sensor.

![image](https://user-images.githubusercontent.com/36953052/221521930-f8f789e6-deec-4cc2-b11e-740caa056e44.png)

## Actions
All actions are prefixed with `truenas.` and **target a specific entity** (the one whose
TrueNAS object they act on). Each action has a name and description in
*Developer Tools → Actions*.

| Action | Target entity | What it does |
| --- | --- | --- |
| `vm_start` · `vm_stop` · `vm_restart` | VM binary sensor | Start / stop / restart a virtual machine (`vm_start` has an optional `overcommit` field) |
| `container_start` · `container_stop` · `container_restart` | Container binary sensor | Start / stop / restart a container (Incus instance) |
| `app_start` · `app_stop` | App binary sensor | Start / stop an app |
| `service_start` · `service_stop` · `service_restart` · `service_reload` | Service binary sensor | Control a TrueNAS service |
| `cloudsync_run` · `cloudsync_abort` | Cloudsync sensor | Start / abort a cloudsync job |
| `replication_run` | Replication sensor | Start a replication task on demand |
| `rsync_run` | Rsync task sensor | Start an rsync task on demand |
| `snapshottask_run` | Snapshot task sensor | Run a periodic snapshot task now |
| `dataset_snapshot` | Dataset sensor | Create an immediate `custom-<timestamp>` snapshot of a dataset |
| `dataset_lock` | Dataset sensor | Lock an encrypted dataset |
| `dataset_unlock` | Dataset sensor | Unlock an encrypted dataset (uses stored passphrase if available, otherwise requires a `passphrase` field) |
| `passphrase_remove` | *(domain-level)* | Remove a stored passphrase by its dataset path (no target entity needed) |
| `system_reboot` · `system_shutdown` | Uptime sensor | Reboot / shut down the TrueNAS system |
| `system_refresh` | Uptime sensor | Force an immediate re-poll so automations can act on current data without waiting for the next poll |

Example:
```yaml
action: truenas.dataset_snapshot
target:
  entity_id: sensor.truenas_<host>_<dataset>
```

> **Run buttons:** snapshot, rsync, replication and cloudsync tasks also expose a one-tap
> **Run** button on their device page, so you can trigger them without calling an action.
>
> **Refresh data button:** the TrueNAS device also has a diagnostic **Refresh data** button
> that triggers `system_refresh` (an immediate re-poll) with one tap, no action call needed.

# Install using HACS (recommended)

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kayl-codes&repository=homeassistant-truenas&category=integration)

1. Click the **My Home Assistant** button above — it opens HACS on your own instance directly on this
   repository. (Manual alternative: open **HACS → 3-dot menu → Custom repositories**, add
   `https://github.com/kayl-codes/homeassistant-truenas` with **Category: Integration**.)
2. Click **Download** to install the integration.
3. Restart Home Assistant (full restart, not quick reload).
4. Navigate to **Settings → Devices & services → Add Integration** and search for **TrueNAS CE**.

> Once the integration is accepted into the HACS default store you can also just search for
> **TrueNAS CE** directly in HACS — until then, use the button or the custom-repository steps above.


Minimum requirements:
* TrueNAS 25.04 or later (tested with 25.10.4)
* Home Assistant 2025.8.0

## Using TrueNAS development branch
If you are using development branch for TrueNAS, some features may stop working.

## Setup integration
1. Create an API key for Home Assistant on your TrueNAS system.

![Setup step 1](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/setup_1.png)
![Setup step 2](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/setup_2.png)
![Setup step 3](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/setup_3.png)

> **⚠️ Required permissions.** On TrueNAS 25.04+ an API key is [tied to a user account](https://www.truenas.com/docs/scale/toptoolbar/managingapikeys/) and inherits that user's privileges, so the integration can only do what the user's role allows. This integration needs **administrative** access: besides reading system/pool/dataset/app data it also performs control actions (reboot/shutdown, start/stop VMs, apps and services, run tasks), which require write privileges across the API. The key's user therefore needs **TrueNAS Access** enabled with the **Full Admin** role (`FULL_ADMIN` grants unrestricted access to every API method). A key whose user has TrueNAS Access disabled, or only a restricted role, will fail to log in **even though the key itself is valid** (this is the usual cause of a *"Login failed, invalid API key"* error with a brand-new key). If you want to scope the key down instead, the [Role-Based Access Control reference](https://api.truenas.com/v25.10/rbac.html) — in particular its *Predefined Group Roles* table — documents exactly what each role can do. The screenshot below shows a dedicated `HomeAssistant` user with Full Admin access.

![Setup step 4 – required user access](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/setup_4.png)

2. Setup this integration for your TrueNAS device in Home Assistant via `Configuration -> Integrations -> Add -> TrueNAS`.
You can add this integration several times for different devices.

NOTES:
- If you dont see "TrueNAS" integration, clear your browser cache.

![Add Integration](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/setup_integration.png)
* "Name of the integration" - Friendly name for this router
* "Host" - The TrueNAS hostname or IP address. Best is a bare host such as `192.168.100.100` (a non-standard port may be appended with a colon, e.g. `192.168.100.100:8888`). If you paste a full URL, any scheme (`https://`) and path are stripped automatically.
* "API key" - TrueNAS API key for Home Assistant (see the required permissions note above)
* "Data size unit" - Choose how storage sizes are displayed. You can select between **GB** (Gigabytes, base 1000) and **GiB** (Gibibytes, base 1024). This will automatically adjust all dataset, pool, and memory sensors.

### Remote access, reverse proxies & Cloudflare

The integration talks to TrueNAS over its modern JSON-RPC 2.0 **WebSocket** API (`wss://<host>/api/current`), which has a few consequences for how you can reach TrueNAS:

* **Use the local IP (or local DNS name) — this works best and is recommended.** Home Assistant and TrueNAS usually sit on the same network, so a local address keeps the traffic entirely local: it does *not* leave to the internet and come back in through a proxy/CDN, which means lower latency, no external dependency and nothing for an auth gateway to intercept. A **VPN** (e.g. WireGuard/Tailscale) achieves the same when HA runs off-site. Of the two, a **plain IP address is the safest choice**, because it removes name resolution from the equation — intermittent DNS/hostname-lookup failures do happen and have been observed in the HA log, and an IP simply cannot hit them.
* **A plain reverse proxy works** (TLS termination only, no authentication) as long as it forwards the WebSocket upgrade and the `/api/current` path untouched. Use a certificate valid for the hostname and keep **Verify SSL certificate** enabled.
* **An authentication gateway in front of TrueNAS does _not_ work** — for example **Cloudflare Access / Zero Trust**, Authelia, or HTTP basic-auth. These intercept the WebSocket handshake and redirect it to a login page (HTTP 302) or reject it (401/403) *before it ever reaches TrueNAS*, so the API key never gets a chance to authenticate. A headless integration cannot complete an interactive SSO login, so this is a hard limitation, not a bug. The integration detects this and reports it clearly instead of a generic error.
  * If you must reach TrueNAS through such a gateway, add a **bypass / service-token policy for the `/api/current` endpoint** so that path skips the interactive login — or simply use the LAN/VPN address instead.

## Options

After setup you can fine-tune the integration via **Settings → Devices & Services → TrueNAS → Configure**. Saving the options reloads the integration so changes take effect immediately.

* **Poll interval** - How often TrueNAS is queried: `5`, `10`, `30`, `60` (default), `120` or `300` seconds. Lower values give near-live network throughput; higher values reduce load on TrueNAS. Interface RX/TX is averaged over the selected interval.
* **Data size unit** - `GB` (base 1000) or `GiB` (base 1024); applied to all dataset, pool and memory sensors.
* **Behaviour**
  * *Skip disabled cronjobs* - hide cronjobs that are disabled in TrueNAS (on by default).
  * *Hide RX/TX sensors for disconnected NICs* - when enabled, traffic sensors are only created for connected interfaces; when disabled (default), every interface gets RX/TX sensors.
* **Monitored groups** - Enable or disable whole sensor groups: **UPS**, **Virtual Machines**, **Containers**, **Cloudsync**, **Replication**, **Rsync Tasks**, **Snapshot Tasks**, **Datasets** and **Directory Services**. Disabling a group skips its API query entirely (saving resources) and removes its entities and device from Home Assistant on the next reload. Core groups (system, network, pools, disks, apps, services, alerts) are always monitored.

## Removing the integration

To completely remove the integration from Home Assistant:

1. Go to **Settings → Devices & Services → TrueNAS**, open the entry menu (⋮) and select **Delete**. This removes the config entry together with all its devices and entities (including any stored dataset passphrases).
2. To also remove the code, open **HACS**, search for **TrueNAS Community Edition**, open its menu (⋮) and select **Remove**, then restart Home Assistant.
3. Optionally revoke the API key that was created for the integration in the TrueNAS UI (**Credentials → Users → API Keys**), since it is no longer needed.

Long-term statistics recorded for the sensors are kept by Home Assistant's recorder and expire with your regular recorder purge settings; no manual cleanup is required.

# Development

## Translation
Translations live directly in this repository under [`custom_components/truenas/translations/`](custom_components/truenas/translations/), with `en.json` (mirrored from `strings.json`) as the source language. Currently shipped: English, German, Spanish, Russian, Slovak and Brazilian Portuguese.

> **Note:** The Lokalise project referenced by the upstream integration is **not wired up for this fork**, so translations are currently maintained by hand in this repository rather than synced through Lokalise.

To fix or improve a translation, edit the matching `<lang>.json` next to `en.json` (keep it in key-parity with `en.json`) and open a pull request. To request a language that is not listed yet, please [open a feature request](https://github.com/kayl-codes/homeassistant-truenas/issues/new?labels=enhancement&title=%5BTranslation%5D%20Add%20new%20language).

## Enabling debug
To enable debug for TrueNAS integration, add following to your configuration.yaml:
```
logger:
  default: info
  logs:
    custom_components.truenas: debug
```


## 🤝 Contributing
Pull Requests are highly welcome! If you find bugs or have feature requests, please create an issue in the GitHub repository.


## ❤️ Support
This integration is actively maintained and updated in my spare time.

If it has helped you, consider supporting ongoing development, bug fixes, compatibility updates, and future enhancements:

- ❤️ GitHub Sponsors: https://github.com/sponsors/kayl-codes
- ☕ Buy Me a Coffee: https://buymeacoffee.com/kayl74

Every contribution is greatly appreciated. Thank you for your support!
