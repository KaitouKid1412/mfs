#!/usr/bin/env bash
# mfs backup + restore-test (F-1). Runbook: docs/ops/backups.md
#
#   scripts/backup.sh                 # = backup
#   scripts/backup.sh backup          # pg_dump -Fc + restic-or-tar of data/raw, with rotation
#   scripts/backup.sh restore-check   # restore latest dump into a scratch DB, compare row
#                                     # counts vs live (±1%), verify one raw-file checksum
#
# Config (env, or .env in the repo root — gitignored):
#   MFS_BACKUP_DIR      where dumps/tarballs land   (default: ~/mfs-backups)
#   MFS_DB              database name               (default: mfs)
#   MFS_RAW_DIR         raw-data tree to back up    (default: <repo>/data/raw)
#   RESTIC_REPOSITORY + RESTIC_PASSWORD             enable restic for data/raw
#                                                   (recommended: external drive
#                                                   repo; tar fallback otherwise)
#
# NOT scheduled by design — scheduling is an operator decision; see the
# runbook for the cron/launchd recipes. The one-shot pre-stage archives under
# data/_archive/ are unrelated to this rotation.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Load .env (RESTIC_*, MFS_*) if present; export everything it sets.
if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/.env"
  set +a
fi

MFS_BACKUP_DIR="${MFS_BACKUP_DIR:-${HOME}/mfs-backups}"
MFS_DB="${MFS_DB:-mfs}"
MFS_RAW_DIR="${MFS_RAW_DIR:-${REPO_ROOT}/data/raw}"

PG_DIR="${MFS_BACKUP_DIR}/pg"
RAW_TAR_DIR="${MFS_BACKUP_DIR}/raw"
SCRATCH_DB="${MFS_DB}_restore_check"

#: Rotation policy (dumps and tar fallback alike).
KEEP_DAILY_DAYS=7        # keep every dump younger than this
KEEP_WEEKLY_DAYS=28      # additionally keep Sunday dumps younger than this
TAR_KEEP=2               # tar fallback is huge (~9.5GB) — keep only this many

#: Tables whose restored row counts must be within 1% of live.
CHECK_TABLES=(nav_daily benchmark_daily holdings_monthly)

log() { printf '[backup] %s\n' "$*" >&2; }
die() { printf '[backup] ERROR: %s\n' "$*" >&2; exit 1; }

restic_enabled() {
  command -v restic >/dev/null 2>&1 && [[ -n "${RESTIC_REPOSITORY:-}" ]]
}

# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------

dump_postgres() {
  mkdir -p "${PG_DIR}"
  local out="${PG_DIR}/${MFS_DB}_$(date +%F).dump"
  log "pg_dump -Fc ${MFS_DB} -> ${out}"
  # Write to a temp sibling, then move: a killed dump never leaves a
  # plausible-looking truncated file at the final name.
  pg_dump -Fc -d "${MFS_DB}" -f "${out}.part"
  mv "${out}.part" "${out}"
  log "dump complete: $(du -h "${out}" | cut -f1)"
}

prune_dumps() {
  # Keep: everything < KEEP_DAILY_DAYS old; Sunday dumps < KEEP_WEEKLY_DAYS.
  local f base d age dow now
  now=$(date +%s)
  shopt -s nullglob
  for f in "${PG_DIR}/${MFS_DB}_"*.dump; do
    base="$(basename "${f}")"
    d="${base#"${MFS_DB}"_}"; d="${d%.dump}"
    # GNU date and BSD date disagree; try both.
    local epoch
    epoch=$(date -j -f %Y-%m-%d "${d}" +%s 2>/dev/null \
            || date -d "${d}" +%s 2>/dev/null) || continue
    age=$(( (now - epoch) / 86400 ))
    dow=$(date -j -f %Y-%m-%d "${d}" +%u 2>/dev/null \
          || date -d "${d}" +%u 2>/dev/null) || continue
    if (( age <= KEEP_DAILY_DAYS )) \
       || { [[ "${dow}" == 7 ]] && (( age <= KEEP_WEEKLY_DAYS )); }; then
      continue
    fi
    log "prune ${base} (age ${age}d)"
    rm -f -- "${f}"
  done
  shopt -u nullglob
}

backup_raw() {
  [[ -d "${MFS_RAW_DIR}" ]] || die "raw dir not found: ${MFS_RAW_DIR}"
  if restic_enabled; then
    log "restic backup ${MFS_RAW_DIR} -> ${RESTIC_REPOSITORY}"
    restic backup "${MFS_RAW_DIR}" --tag mfs-raw
    restic forget --tag mfs-raw --keep-daily "${KEEP_DAILY_DAYS}" \
                  --keep-weekly 4 --prune
  else
    log "restic not configured (set RESTIC_REPOSITORY/RESTIC_PASSWORD);"
    log "falling back to a full tar.gz — set up restic, this is ~9.5GB/run"
    mkdir -p "${RAW_TAR_DIR}"
    local out="${RAW_TAR_DIR}/raw_$(date +%F).tar.gz"
    tar -czf "${out}.part" -C "$(dirname "${MFS_RAW_DIR}")" \
        "$(basename "${MFS_RAW_DIR}")"
    mv "${out}.part" "${out}"
    log "tarball complete: $(du -h "${out}" | cut -f1)"
    # Keep only the newest TAR_KEEP tarballs.
    ls -1t "${RAW_TAR_DIR}"/raw_*.tar.gz 2>/dev/null \
      | tail -n "+$((TAR_KEEP + 1))" \
      | while IFS= read -r old; do log "prune $(basename "${old}")"; rm -f -- "${old}"; done
  fi
}

cmd_backup() {
  dump_postgres
  prune_dumps
  backup_raw
  log "backup OK"
}

# --------------------------------------------------------------------------
# restore-check (the test — run monthly, see runbook)
# --------------------------------------------------------------------------

latest_dump() {
  ls -1t "${PG_DIR}/${MFS_DB}_"*.dump 2>/dev/null | head -n1
}

check_counts() {
  local table="$1" live restored diff tol
  live=$(psql -d "${MFS_DB}" -tAc "SELECT COUNT(*) FROM ${table}")
  restored=$(psql -d "${SCRATCH_DB}" -tAc "SELECT COUNT(*) FROM ${table}")
  diff=$(( live > restored ? live - restored : restored - live ))
  tol=$(( live / 100 ))   # 1% of live (integer floor; live>=100 in practice)
  printf '[backup] %-28s live=%-12s restored=%-12s diff=%s\n' \
         "${table}" "${live}" "${restored}" "${diff}" >&2
  (( diff <= tol )) || die "${table}: restored count deviates > 1% from live"
}

check_raw_sample() {
  # Verify one raw artifact round-trips byte-identically from the backup.
  local sample rel tmp
  sample=$(find "${MFS_RAW_DIR}" -type f \( -name '*.pdf' -o -name '*.xlsx' \) \
           2>/dev/null | head -n1)
  [[ -n "${sample}" ]] || { log "no sample file under ${MFS_RAW_DIR}; skipping raw check"; return 0; }
  tmp="$(mktemp -d /tmp/mfs-restore-check.XXXXXX)"
  if restic_enabled; then
    log "restic check + sample restore: ${sample}"
    restic check
    restic restore latest --target "${tmp}" --include "${sample}"
    # restic recreates the absolute path under the target dir.
    rel="${tmp}/${sample#/}"
  else
    local tarball
    tarball=$(ls -1t "${RAW_TAR_DIR}"/raw_*.tar.gz 2>/dev/null | head -n1)
    [[ -n "${tarball}" ]] || { log "no raw tarball yet; skipping raw check"; rm -rf "${tmp}"; return 0; }
    rel="$(basename "${MFS_RAW_DIR}")/${sample#"${MFS_RAW_DIR}"/}"
    log "extracting sample from $(basename "${tarball}"): ${rel}"
    tar -xzf "${tarball}" -C "${tmp}" "${rel}"
    rel="${tmp}/${rel}"
  fi
  local h1 h2
  h1=$(shasum -a 256 "${sample}" | cut -d' ' -f1)
  h2=$(shasum -a 256 "${rel}" | cut -d' ' -f1)
  rm -rf "${tmp}"
  [[ "${h1}" == "${h2}" ]] || die "raw sample sha256 mismatch: ${sample}"
  log "raw sample sha256 OK"
}

cmd_restore_check() {
  local dump
  dump=$(latest_dump)
  [[ -n "${dump}" ]] || die "no dump found under ${PG_DIR}; run backup first"
  log "restoring $(basename "${dump}") into scratch DB ${SCRATCH_DB}"
  dropdb --if-exists "${SCRATCH_DB}"
  createdb "${SCRATCH_DB}"
  # pg_restore exits non-zero on harmless ownership warnings; require the
  # tables we check to exist + match instead.
  pg_restore -d "${SCRATCH_DB}" --no-owner --no-privileges -j 4 "${dump}" \
    || log "pg_restore reported warnings (continuing to count checks)"
  for t in "${CHECK_TABLES[@]}"; do
    check_counts "${t}"
  done
  dropdb "${SCRATCH_DB}"
  check_raw_sample
  log "restore-check OK"
}

# --------------------------------------------------------------------------

case "${1:-backup}" in
  backup)        cmd_backup ;;
  restore-check) cmd_restore_check ;;
  *) die "unknown subcommand: ${1} (use: backup | restore-check)" ;;
esac
