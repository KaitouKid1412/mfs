# Backups runbook (F-1)

Two things hold state worth money here:

| asset | size | recoverability if lost |
|---|---|---|
| Postgres `mfs` DB (nav_daily, benchmark_daily, holdings, metrics, history tables) | ~11 GB | NAV/TRI re-ingestable over days; **rank_history / scheme_master_history snapshots are NOT** — point-in-time state is gone forever |
| `data/raw/` (factsheet PDFs, holdings Excels, bhavcopies) | ~9.5 GB | **mostly unrecoverable** — AMCs delist old factsheets/Excels within months |

`scripts/backup.sh` covers both. It is **deliberately not scheduled** —
installing the cron/launchd job is an operator decision (recipes below). The
one-shot pre-stage archives under `data/_archive/` are separate, manual
safety copies and are not rotated by this script.

## What the script does

```bash
scripts/backup.sh backup          # default
```

1. `pg_dump -Fc -d mfs` → `$MFS_BACKUP_DIR/pg/mfs_<YYYY-MM-DD>.dump`
   (compressed custom format; written to a `.part` temp and renamed, so a
   killed run never leaves a plausible truncated dump).
2. Rotation: keeps every dump younger than 7 days, plus Sunday dumps younger
   than 28 days (≈ 7 daily + 4 weekly).
3. `data/raw`: **restic** (preferred — incremental, deduplicating, fast after
   the first run) if `RESTIC_REPOSITORY`/`RESTIC_PASSWORD` are set, with
   `forget --keep-daily 7 --keep-weekly 4 --prune`; otherwise a full
   `tar.gz` fallback keeping the newest 2 tarballs (a loud nudge to set up
   restic — 9.5 GB per run is not a sustainable fallback).

```bash
scripts/backup.sh restore-check   # THE test — run monthly
```

1. Restores the latest dump into a scratch DB `mfs_restore_check`
   (`createdb` → `pg_restore --no-owner -j 4`).
2. Asserts `nav_daily`, `benchmark_daily`, `holdings_monthly` row counts are
   within **1%** of the live DB, then drops the scratch DB.
3. `restic check` + restores one sample PDF/Excel to `/tmp` and compares its
   sha256 against the live file (tar fallback: extracts the sample from the
   newest tarball instead).

A backup that has never been restored is a hope, not a backup — the monthly
restore-check is part of the policy, not optional.

## Configuration

Environment variables, or the repo-root `.env` (gitignored — never hardcode
secrets):

```bash
MFS_BACKUP_DIR=/Volumes/backup/mfs     # default ~/mfs-backups
MFS_DB=mfs
RESTIC_REPOSITORY=/Volumes/backup/mfs-restic   # external drive repo (minimum)
RESTIC_PASSWORD=...
```

First-time restic setup: `restic init` against the repository path. A second
cloud target (B2/S3 via a second `RESTIC_REPOSITORY`) is recommended for the
irreplaceable `data/raw` but is the operator's cost call.

Disk budget: dumps ≈ 11 dump files ≈ low-100s of GB worst case uncompressed
— `-Fc` compresses well; expect single-digit GB per dump. Restic dedupes, so
the raw repo grows roughly with new months only.

## Scheduling (operator decision — NOT installed by default)

cron (simplest; runs only if the machine is awake at 02:30):

```cron
30 2 * * * /Users/<you>/mfs/scripts/backup.sh backup >> $HOME/mfs-backups/backup.log 2>&1
```

launchd (macOS-native; runs missed jobs after wake) — save as
`~/Library/LaunchAgents/com.mfs.backup.plist`, then
`launchctl load ~/Library/LaunchAgents/com.mfs.backup.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.mfs.backup</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/<you>/mfs/scripts/backup.sh</string>
    <string>backup</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>2</integer>
        <key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>/tmp/com.mfs.backup.log</string>
  <key>StandardErrorPath</key><string>/tmp/com.mfs.backup.log</string>
</dict></plist>
```

Verify: `launchctl list | grep com.mfs.backup`.

## Cadence policy

- **Daily**: `backup.sh backup` (scheduled, once installed).
- **Monthly**: `backup.sh restore-check` (manual; ~minutes). Record the date
  + result in your ops notes. A failed check is a drop-everything event:
  the next data loss is unrecoverable.
- **Before risky operations** (schema migrations, bulk DELETEs, full
  recomputes): run `backup.sh backup` manually first.
