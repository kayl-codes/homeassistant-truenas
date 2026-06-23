![TrueNAS Community Edition](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/header-ce.png)

**TrueNAS CE** — monitor and control your TrueNAS device (25.04+) from Home Assistant.

> **⚠️ Version 2.0.0 — breaking change:** the Home Assistant domain changed from `truenas` to
> `truenas_ce` as this project became independent. After updating, add **“TrueNAS CE”** once — host,
> API key, options, **entities, history and long-term statistics are migrated automatically** (with a
> Repairs-based rollback while the old integration is still installed). Full guide:
> [docs/migration.md](https://github.com/kayl-codes/homeassistant-truenas/blob/master/docs/migration.md).

 * Monitor System (CPU, Load, Memory, Temperature, ARC/L2ARC, Uptime)
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
 * Control and Monitor Cloudsync
 * Monitor Directory Services (Active Directory / LDAP / IPA status)
 * Monitor Active Alerts and Diagnostics
 * Create a Dataset Snapshot
 * Lock / unlock encrypted Datasets
 * Reboot and Shutdown the TrueNAS system

## Links
- [Documentation](https://github.com/kayl-codes/homeassistant-truenas/tree/master)
- [Setup guide](https://github.com/kayl-codes/homeassistant-truenas/tree/master#setup-integration)
- [Migration guide](https://github.com/kayl-codes/homeassistant-truenas/blob/master/docs/migration.md)
- [Report a Bug](https://github.com/kayl-codes/homeassistant-truenas/issues/new?labels=bug&template=bug_report.md&title=%5BBug%5D)
- [Suggest an idea](https://github.com/kayl-codes/homeassistant-truenas/issues/new?labels=enhancement&template=feature_request.md&title=%5BFeature%5D)

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-support-FFDD00?style=plastic&logo=buymeacoffee&logoColor=black)](https://www.buymeacoffee.com/kayl74)
