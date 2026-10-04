# Changelog

## Unreleased

### Fixed

- **SD-1 (P0)**: the compression/encryption pipeline now writes the final
  artifact back to the backup result. Upload, `vault_backups` record
  (`size_bytes` / `compressed_size` / `encryption` / `checksum_sha256`),
  listing and restore now all reference the same on-disk file. Each stage
  removes its input after producing output, so an encrypted backup no longer
  leaves a plaintext original on disk (previously the unencrypted/uncompressed
  raw archive was uploaded and recorded, and the `.gz` / `.enc` artifacts
  were orphaned outside the list/restore chain).
- **SD-1 follow-up (P0)**: list/detail/download/delete/sign/verify/cleanup/
  rotation and restore/drill now resolve the real final artifact
  (`.tar.gz`, `.tar.gz.gz/.zst/.lz4`, and their `.enc` forms) instead of
  globbing only bare `.tar.gz`. Restore transparently decrypts and
  decompresses in a temporary directory; a missing key, wrong key, or
  unavailable `cryptography` library now fails with an explicit error
  instead of a 500 or silent fallback.
- **ENV-1 (P0)**: `pg_dump` (backup) and `psql` (restore) major versions are
  now checked against the target server's `SHOW server_version` and must
  match exactly; the operation is refused with an actionable message
  otherwise, preventing silently unrestorable dumps (e.g. a v17/v18 dump
  fed to a v14 `psql` with `ON_ERROR_STOP=1`). `psql` / `createdb` /
  `dropdb` are also resolved through `PG_BIN` / `<TOOL>_PATH` / `PATH`,
  matching `pg_dump`, so portable deployments no longer fail with
  "executable not found".
- **ENV-1b (P0)**: the version gate no longer trusts a bare
  `<tool> --version` probe, which is fooled by the Debian/Ubuntu
  `pg_wrapper` multiplexer (`/usr/bin/pg_dump` / `psql` are symlinks that
  report the default cluster version for a bare probe but execute the newest
  installed client for real `-h` connections — observed producing a
  "pg_dump 18.6" dump of a v14 server that v14 `psql` could not restore).
  A new `resolve_pg_tool_for_server()` locates the actual on-disk binary
  (`/usr/lib/postgresql/<major>/bin`, `/usr/pgsql-<major>/bin`,
  `/usr/local/pgsql/bin`), verifies its real major version against
  `SHOW server_version`, rejects `pg_wrapper`, and applies to backup,
  restore, sandbox create/drop and drill verify. Explicit `<TOOL>_PATH` /
  `PG_BIN` still wins; a mismatched explicit binary now fails closed with
  an actionable message instead of silently producing an unrestorable dump.
  Verified end-to-end on a real PostgreSQL 14 host: with no env overrides
  the v14 client is auto-selected and backup → gzip → AES-GCM → decrypt →
  restore is byte-identical (1000 rows).
- **N-1 (P3)**: `.env` is now read as UTF-8 explicitly in both `get_pg_env()`
  and `BackupEngine._redact_env()`. On Windows/GBK standalone deployments a
  UTF-8 `.env` containing non-ASCII comments or values previously raised
  `UnicodeDecodeError` before backup/config export; the migration reader
  already used UTF-8. No permissive decode fallback is added so an incorrectly
  encoded file fails loudly instead of silently corrupting non-ASCII secrets.
- **SD-6 / F-C (P1)**: scheduled backups no longer retry three times inside
  one cron tick with a blocking 60-second `sleep`. Failures now reschedule
  across subsequent minute-ticks (at most 3 cross-tick retries, then the
  normal cron cadence resumes) without any new database column, and the
  runner acquires a non-blocking cross-process singleton lock
  (`data/vault/.scheduler.lock`) so overlapping cron runs cannot process
  the same schedule twice.
- **SD-7 / RS-05b (P1)**: tar extraction now uses `filter='data'` on Python
  3.12+ (with the existing manual member validation as fallback on older
  versions). Symlink targets are resolved relative to the link's own
  directory and hardlink targets relative to the archive root, both
  required to stay inside the extraction directory; absolute/UNC links,
  path traversal and device/FIFO members are rejected. In-archive relative
  symlinks no longer fail validation.
- **SD-5 (P1)**: point-in-time recovery now fails closed. `restore_pitr()`
  no longer creates a sandbox, writes parameters into the primary data
  directory's `recovery.signal`, or reports success without replaying WAL;
  it returns `supported: false` with an explanation (HTTP 501), since true
  PITR requires physical `pg_basebackup` backups plus continuous WAL
  archiving and a dedicated recovery instance.
- **F-A (P1)**: uninstalling the plugin (which drops the `vault` schema)
  no longer leaves the process-level `_SCHEMA_ENSURED` flag stuck on.
  `ensure_schema()` now verifies against `information_schema` that the
  schema still exists before trusting the flag — so a reinstall recreates
  the tables on the next request even when the DROP ran in another gunicorn
  worker — and `on_uninstall` explicitly resets the flag after a successful
  `DROP SCHEMA`.
- **SD-3 (P1)**: the backup list and health score are now driven by the
  `vault_backups` table merged with on-disk artifacts. Real
  `status` / `backup_type` (including failed backups with no file) are
  shown and counted; if the table is unavailable the listing degrades to
  the previous disk-only behaviour. Deleting a backup and the retention
  cleanup now also remove the corresponding table rows, so deleted and
  cleaned backups no longer reappear or keep penalising the health score.
- **SD-10 (P2)**: audit `operator` and backup `created_by` now resolve the
  real acting identity in the same order — JWT identity injected as
  `request.vault_user` (`username`, then `display_name`, `phone`,
  `user_id`, matching the platform's existing resolution), then the Flask
  session user, then `system` — instead of only reading the session, which
  recorded Bearer/JWT callers and the scheduled job uniformly as `system`.
- **ENV-3 (P2)**: schedule creation and the health check's next-schedule
  query no longer rely on PostgreSQL `NOW()` for the timezone-less
  `TIMESTAMP` columns. Both now bind a Python naive-UTC
  `datetime.utcnow()`, matching the comparison used everywhere else, so a
  server whose PostgreSQL session `TimeZone` is not UTC can no longer skew
  schedule times or the next-schedule result.
- **SD-2 / F-B (P2)**: removed the dead `set_bandwidth_limit()` /
  `_apply_bandwidth()` methods and `bandwidth_limit_bytes` attribute from
  `BackupEngine` (never called anywhere; `_apply_bandwidth` also referenced
  an unimported `time` and would have raised if invoked). Fixed the stale
  `plugins/vault/dumper.py` entry in `veroguard/tools/build_manifest.py`
  (that path never existed) to point at the real
  `plugins/vault/services/backup_engine.py`.
- **Track A — scheduled backup pipeline (SD-1 follow-up)**:
  `VaultScheduler.execute_schedule` now runs the same post-backup pipeline
  as manual backups — `finalize_artifact` (compression/encryption with the
  final artifact and real size/checksum written back), best-effort upload,
  notification, a `vault_backups` row (success **and** failure, created by
  `system`), and an audit entry. A finalize failure marks the run failed so
  it is retried/backed off instead of advancing the cron with a
  half-finished artifact. Schedule retention cleanup now enumerates all
  artifact forms via `list_backup_files()` (no longer a bare `.tar.gz`
  glob) and deletes the matching `vault_backups` rows. The multi-target
  StorageRouter switch remains deferred to the SD-8 batch; scheduled and
  manual uploads still share the existing single-target `upload_backup`.

## v2.6.4 — 2026-09-13

### Notes

- Version bump for store release; no independent code change was recorded for this version.
- Fixes delivered after this bump (2026-10-02 onward) remain listed under **Unreleased**.

## v2.6.3 — 2026-08-30

### Changes

- Version bump from v2.5.3

## v2.5.2 — 2026-08-22

### Changes

- Version bump from v2.5.1

## v2.5.1 — 2026-08-20

### Changes

- Version bump from v2.4.1

## v2.4.0 — 2026-08-19

### Changes

- Version bump from v2.3.0

