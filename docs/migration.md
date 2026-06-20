# Migrating to TrueNAS Community Edition (v2.0.0)

Starting with **v2.0.0**, this integration uses the domain **`truenas_ce`** instead of
`truenas`. The visible brand ("TrueNAS") is unchanged — only the internal domain is
renamed so the project can be listed independently on HACS.

This is a **breaking change**, but the migration is designed to be **transparent**:
your entities keep their `entity_id`s and **their full history (states + long-term
statistics)**, and your connection settings are carried over so you don't re-type
anything. The old integration is **disabled, not deleted**, so you can roll back.

> **TL;DR** — Update via HACS, restart, add **"TrueNAS CE"**, choose *Take over existing
> configuration*. Done. Your history is preserved and the old integration stays disabled
> as a safety net you can roll back to.

---

## Why the rename?

This project is a community-maintained fork. To be distributed independently through
HACS (and the Home Assistant brands repository), the integration needs a **unique
domain** — the original `truenas` domain is already taken. Renaming to `truenas_ce`
("Community Edition") resolves that without you losing any data.

## What is preserved

| Preserved | Notes |
| --- | --- |
| **Entity IDs** (`sensor.truenas_*`, …) | Re-attached to the new integration. |
| **History & long-term statistics** | Reconnected to the same entity IDs. |
| **Your customisations** | Custom name, icon, area and "disabled" state are restored. |
| **Connection settings** | Host, API key, SSL verification and all options are taken over. |

What changes cosmetically: the **devices** are recreated under the new integration (device
grouping is not history-bearing). The old, now-disabled integration remains visible until
you remove it.

---

## Step-by-step

### 1. Update the integration

Update **TrueNAS** to **v2.0.0** in HACS and **restart Home Assistant** when prompted.

HACS installs the new code into `custom_components/truenas_ce/` and **leaves the old
`custom_components/truenas/` folder untouched**. Both versions are therefore present after
the update — there is no "unavailable" gap, and the rollback path stays safe.

### 2. Add "TrueNAS CE"

HACS only downloads files; it does not start a config flow. After the restart, go to
**Settings → Devices & Services → Add Integration** and add **TrueNAS CE**.

The integration detects your previous TrueNAS configuration and offers to take it over:

![Take over existing configuration](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/migration_1.png)

Choose **Take over existing configuration**.

### 3. Confirm the prefilled settings

The setup form is prefilled with your existing host, API key, SSL option and the same
integration **name** (the name must match — entity IDs are derived from it, which is what
lets the migration re-attach your history). Just confirm.

![Prefilled setup form](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/migration_2.png)

Your options (poll interval, data unit, behaviours, monitored groups) are carried over as
well, so the *Configure* dialog looks exactly as before:

![Options carried over](https://raw.githubusercontent.com/kayl-codes/homeassistant-truenas/master/docs/assets/images/ui/migration_3.png)

### 4. Migration runs automatically

On first setup, TrueNAS CE:

1. **disables** the old `truenas` integration (so it stops polling),
2. **adopts** its entity IDs (freeing them, history stays in the recorder DB),
3. recreates the entities under `truenas_ce` and **re-attaches the original entity IDs**,
   reconnecting states and statistics,
4. writes a **safety backup snapshot** to `.storage`, and
5. persists a reverse map so the whole step can be rolled back.

You'll then get a **success notification** summarising the result, e.g.:

> **TrueNAS Community Edition migration complete.**
> N entities adopted with full history; the previous TrueNAS integration was disabled
> (not deleted).
> **Validation**
> - ✅ Entities adopted: N
> - ✅ History reconnected: N/N
> - ✅ Previous TrueNAS integration disabled (not deleted)
> - ✅ Safety backup written
> - ✅ Rollback available

A ⚠️ next to any line means that check did not fully pass — see
[Troubleshooting](#troubleshooting).

---

## Rolling back

The old integration is kept (disabled) as a safety net. Nothing is shown automatically —
to roll back, press the **Roll back migration** button on the TrueNAS device
(Settings → Devices & Services → the TrueNAS CE device → *Diagnostic*). The button is
**safe**: it does nothing destructive itself, it only opens a confirmation under
**Repairs** ("Roll back the TrueNAS CE migration?").

In that dialog you can **deliberately** choose to roll back, or dismiss it. Dismissing just
closes the dialog — press the button again to reopen it. The button only appears while a
rollback is still possible (the old integration still exists).

A confirmed rollback:

1. removes the TrueNAS CE integration (freeing the adopted entity IDs),
2. re-enables the old `truenas` integration, which reclaims the original entity IDs, and
3. restores your customisations.

History is preserved in both directions.

> ⏳ **Time-boxed:** the rollback is only possible **while you keep the old TrueNAS
> integration**. Once you delete it, the bridge is permanently burned and the Repairs
> entry disappears. Verify everything looks right *before* removing the old integration.

After you're satisfied, you can **delete the old (disabled) TrueNAS integration** to
complete the move. There's no rush — leaving it disabled does no harm.

---

## Troubleshooting

- **History didn't reconnect for some entities** — make sure the **integration name**
  matches the old one. Entity IDs (and therefore statistics) are derived from the name; a
  different name regenerates different IDs.
- **Old `sensor.truenas_* _2` duplicates appear** — this happens if the old integration
  was still enabled when the new entities were created. Roll back, ensure the old
  integration is present (it will be auto-disabled), and let the migration run again.
- **Safety backup line shows ⚠️** — the standalone `.storage` snapshot couldn't be
  written; the reverse map on the config entry is still the primary undo, so rollback
  remains available. Check the Home Assistant log for the warning.
- **I removed the old integration too early** — the automatic rollback is no longer
  available. Your TrueNAS CE entities and history are intact; only the one-click revert is
  gone.

---

## FAQ

**Do I need to recreate my dashboards/automations?**
No. They reference `entity_id`s, which are preserved.

**Will my long-term statistics/energy history survive?**
Yes — that's the whole point of the adoption step.

**Can I run both integrations at once?**
No. During migration the old one is disabled on purpose; running both against the same
host with the same name would collide on entity IDs.

**The brand still says "TrueNAS" — is that right?**
Yes. Only the internal domain changed (`truenas` → `truenas_ce`); the display name stays
**TrueNAS**.
