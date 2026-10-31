# Basement Cloud

Your own private Nextcloud file server beside GFPicturebook, with iPhone photo uploads and encrypted, versioned backups on a different physical drive. This is an independent Docker Compose project and repository. It does not change the album's code, database, accounts, containers, or HTTPS routes.

## Your server layout

| Use | Existing filesystem | New application folder |
| --- | --- | --- |
| Live Nextcloud files | `/mnt/album-drive-1`, UUID `d10eb869-4458-449e-880a-36ad70210d1d` | `basement-cloud/` |
| Encrypted backup history | `/mnt/album-drive-3`, UUID `f0b3d701-2343-46d2-b2b2-09791ba22b2d` | `basement-cloud-backups/restic/` |
| Database, app/config, secrets | OS UUID `62fba36b-4bd5-46be-8e8e-b20149db4e02` | `/var/lib/basement-cloud/` |

**Drive 2 is currently absent.** Your `df` output shows the OS filesystem beneath `/mnt/album-drive-2`; this program never uses that path. Repair/reconnection of that disk is a separate task. Never rerun the old album formatter to reconnect it.

The existing `album-archive` folders remain separate. The initial Nextcloud owner quota is **1 TiB**, with **100 GiB minimum free-space headroom** on shared drives and an additional initial allowance for history. This is a user quota, not a filesystem reservation: album growth and backup history also use these disks. A smaller quota can be set at initialization with `--quota-gib 512`. Increasing capacity later requires measuring both drives and allowing for version history. The two disks' raw capacities are not added together as usable protected storage. Startup reapplies the stored quota; adjust the root-owned `config.json` budget after checking space rather than relying only on a temporary UI change.

No part of this program formats, partitions, repairs, mounts, or erases a disk. Preflight checks compare filesystem UUIDs, actual mountpoints, device numbers, and physical disk parents. Initialization pins mount descriptors while creating new directories, refuses existing/unrecognized application folders, and never changes ownership of a whole disk. Runtime guards detect a missing/wrong live mount or low live free space and stop Nextcloud writers. A missing backup disk leaves the live site running but backup status degraded.

## Install on the Linux server

Requires existing mounted ext4 drives, Python **3.12+**, Docker Engine with Compose v2, systemd, `findmnt`, and your working Tailscale installation. Python has no third-party runtime dependencies, so no pip installation is needed. Development uses uv.

From a checkout of this separate repository:

```bash
# Read-only validation first; it should identify ONLY drives 1 and 3.
python3 cloud.py check

# Creates new application directories and secrets; prompts for the owner password.
# Use 12+ characters; the initial password must not start with a dash.
sudo python3 cloud.py init

# Starts only this new Compose project and waits for installation/health checks.
sudo python3 cloud.py start

# Adds a separate tailnet-only HTTPS listener without resetting existing routes.
sudo python3 cloud.py expose

# Take the first backup and read it back before relying on the service.
sudo python3 cloud.py backup
sudo python3 cloud.py verify

# Install guarded boot startup, a one-minute health check, and backup timers.
sudo python3 cloud.py enable
sudo python3 cloud.py status
```

Open **https://basement.taila4bea8.ts.net:8443** with Tailscale connected. Sign in as **`owner`** with the password you chose. The album stays at its current addresses on port 443. The new HTTP port is bound only to `127.0.0.1:8081`; database/cache ports are not published. If your tailnet uses restrictive grants, permit your authorized devices to this server's TCP port 8443. Tailscale Funnel is not used.

Save the contents of `/var/lib/basement-cloud/secrets/restic_password` in your password manager, separate from this server. You need this recovery key to decrypt backups after loss of the OS disk; a copy inside the encrypted repository cannot unlock itself. You can read the key privately with `sudo cat /var/lib/basement-cloud/secrets/restic_password`; save it in your password manager and keep credentials out of Git and chat. The owner account is private and has Nextcloud administration access; public links/uploads are disabled initially.

After initialization, use either the checkout's `cloud.py` or the root-owned installed copy:

```bash
sudo python3 /var/lib/basement-cloud/runtime/cloud.py status
```

Do not run the album's environment generator or storage formatter for this application. Initialization is deliberately not a reset command: if interrupted or already initialized, it stops for inspection instead of overwriting files or rotating keys. Keep the existing partial directories and report the error.

## Set up your iPhone

1. Keep **Tailscale connected**, install the official **Nextcloud** iPhone app, and enter `https://basement.taila4bea8.ts.net:8443`.
2. Sign in as `owner`, enable the app in iOS **Files → Browse → Edit → Locations**, and use Nextcloud for ordinary file uploads/downloads and folders.
3. Enable photo/video auto-upload in the Nextcloud app, grant access to the photos you want, and select a private folder such as `Photos/iPhone`. Choose original-format uploads rather than JPEG conversion if you want the original HEIC files. Verify Live Photos retain both still and video components before relying on them.
4. For the first large library upload, keep the phone charging, connected, and the app open. iOS controls background execution; auto-upload is best effort, not a promise that every photo uploads immediately while the app is closed. Photos stored only in iCloud may need downloading first.
5. Confirm uploads have finished, inspect sample photos/videos and original downloads, and check that a nightly backup has completed before using any phone storage-cleanup feature. This setup does not automatically delete anything from your phone.

Manage cloud files through Nextcloud, its WebDAV API, or the iOS Files provider so the index and version history stay consistent.

Photos and files stored here are separate from the shared picture book. Adding selected memories to GFPicturebook remains explicit. This phase is **photo/video and file storage**, not a restorable backup of iPhone app data, messages, or settings. Full-device backups via Apple's supported computer software are deferred.

## Backups and recovery

- Nightly backup: **3:00 a.m. America/New_York**, including daylight-saving changes; a missed run is retried when the server returns.
- Weekly full read/decrypt/check: **Sunday at 5:00 a.m. America/New_York**.
- Retention: keep at least the last **2** snapshots plus **7 daily, 4 weekly, and 3 monthly** snapshots. Restic deduplicates unchanged files. A failed backup never prunes the previous good snapshot to make space.
- A backup briefly puts Nextcloud into maintenance and stops its web/PHP/cron writers, then captures PostgreSQL roles and a database dump, application/config, original data, and recovery configuration/secrets together. The app resumes before repository verification/retention. The initial backup or a very large changed batch can take longer; phone transfers can retry/resume.
- Backup jobs have a host lock, bounded resources, free-space/UUID guards, and durable status. A failure resumes the previous maintenance state and keeps the previous completed snapshot. Startup detects interrupted jobs and stops an owned orphan Restic container before recovery. The health timer skips active backups and respects an intentional stop.
- `status` distinguishes the latest completed snapshot from the last full integrity check. Full checks read repository data, rather than merely reporting that a process started. Failures are recorded locally and attempt an owner Nextcloud notification; offline servers cannot deliver in-app notifications.

A newly uploaded file has **one live server copy until the next successful nightly backup**, potentially about 24 hours. This service does not promise immediate two-copy replication or automatic failover from a lost primary drive. Album replication remains the album's separate feature. A second disk in the same server is not an offsite backup; save especially important data separately too.

Useful operator commands:

```bash
sudo python3 cloud.py status
sudo python3 cloud.py snapshots
sudo python3 cloud.py backup
sudo python3 cloud.py verify
journalctl -u basement-cloud-backup.service --since yesterday
systemctl list-timers 'basement-cloud-*'
```

To stop intentionally, use `sudo python3 cloud.py stop`; the health timer respects that choice. `start` re-enables service operation. To disable boot/timers too, use `systemctl disable --now` for `basement-cloud.service` and all three `basement-cloud-*.timer` units. None of these commands affects GFPicturebook.

### Restore into a separate, empty directory

```bash
sudo mkdir -m 700 /srv/basement-cloud-recovery
sudo python3 cloud.py restore --snapshot SNAPSHOT_ID --target /srv/basement-cloud-recovery
```

Use an explicit snapshot ID from `snapshots`. Restore refuses a nonempty target, the live state/data/backup directories, and their parents. It does not automatically replace a running service or database. Restored files are under `source/nextcloud`, `source/data`, `source/database/roles.sql`, `source/database/database.dump`, and `source/secrets`.

For a complete recovery, retain the damaged deployment, recover to fresh empty application and PostgreSQL directories with the **recorded image versions** from `source/database/images.json`, and recover the required encryption key/config. Copy the restored Nextcloud application tree and data to the new locations with the recorded permissions (Alpine www-data UID/GID 82). Start only a fresh database/cache with bootstrap role/database `postgres` (the original dedicated server starts with `cloud`). Restore `roles.sql` first using `psql -U postgres -d postgres --set ON_ERROR_STOP=on`, then restore `database.dump` using `pg_restore --exit-on-error --create -U postgres -d postgres`. This recreates the `cloud` database and Nextcloud’s `oc_admin` role with their recorded ownership, grants, and password hashes. Do not use `--no-owner` or omit the roles file. Restore the normal Compose database environment afterward, then start PHP, run `occ maintenance:data-fingerprint` as UID 82 so clients detect the rollback, clear maintenance mode only when ready, and verify the owner login and original downloads before enabling normal traffic. Do not copy a running PostgreSQL data directory or start an empty Nextcloud install against the recovered data. Test recovery in isolation first; `tests/integration.py` exercises this process with synthetic data.

### If the OS disk and installed helper are lost

The encrypted repository on drive 3 can still be read without the primary disk. Recover a checkout of this repository and the offline Restic key, verify that `/mnt/album-drive-3` has UUID `f0b3d701-2343-46d2-b2b2-09791ba22b2d`, and save the recovered key in a root-only file such as `/root/basement-cloud-recovery.key`. Do not initialize or reset the existing repository.

For bootstrap recovery, use the pinned Restic image in `images.json` with the backup repository mounted **read-only** at `/repository`, the key file mounted read-only at `/run/secrets/password`, and `RESTIC_REPOSITORY=/repository` plus `RESTIC_PASSWORD_FILE=/run/secrets/password`. Run `--no-cache --no-lock snapshots` to find a completed snapshot. Then mount a fresh, empty recovery directory at `/restore` and run `--no-cache --no-lock restore SNAPSHOT_ID --target /restore`. Using `--no-lock` here allows a read-only repository; keep the old backup jobs stopped while recovering. Check the target has room for the snapshot first.

Recover the recorded application versions, config, data, database dump, and secrets from that snapshot, then use the complete recovery procedure above. Validate any replacement disk UUIDs before enabling normal service. The normal helper's snapshot/verify/restore operations also work when the original primary disk is missing; only new backups require both data drives.

### Updates

Images are pinned by digest in `images.json`, including the stable Nextcloud version. There is no Watchtower or unattended major upgrade. Updating the checkout alone does not change the root-owned installed runtime. Before a software update, take and verify a backup; review upstream Nextcloud's supported one-major-at-a-time upgrade path, preserve the installed configuration/secrets, and test the new images in isolation. This first release provides a deliberately explicit deployment, not a one-command destructive reinstaller. Never rerun `init` over an existing installation.

## Resource limits and upload behavior

The deployment uses the official Nextcloud 34.0.5 Alpine FPM image, Nginx, a separate PostgreSQL 17 instance, Redis file locking, and a separate cron service. The image/ownership choices do not affect the album's UID 10001.

PHP is limited to 512 MiB per worker and four workers; its container is capped at 4 GiB. Database, cron, Nginx, and Redis have separate limits (2 GiB, 1 GiB, 256 MiB, and 256 MiB). Restic uses at most 2 GiB and one CPU. Large uploads and Nextcloud/Nginx/PHP temporary files use the verified live storage disk, not unbounded RAM or the OS disk. Nginx accepts requests up to 100 GiB, and Nextcloud supports chunked/resumable transfers within quota and available space. There is no 50 MB limit. The OS can use remaining RAM for filesystem caching alongside the album.

## Verification and development

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v
# Explicit integration test: Docker required, synthetic /tmp storage only.
uv run python -u tests/integration.py
```

Unit tests cover UUID/mount checks, rejecting the OS/missing/same physical drives, symlinks, no-overwrite behavior, free-space/quotas, locks, private restores, guarded shutdown, and preserving both album Tailscale addresses. The integration test creates fresh temporary directories and a unique Compose project, then checks actual uploads, chunk resume across backup, encrypted snapshots, failed-job recovery, old-file recovery, fresh-instance database/account recovery, and corrupted-repository detection. Production has no environment switch to bypass UUID checks. The test uses in-process mocks only for temporary mount identities and reduced cloud-workspace capacity.

The Nginx configuration is adapted from [Nextcloud's official Docker example](https://github.com/nextcloud/docker/blob/master/.examples/docker-compose/with-nginx-proxy/postgres/fpm/web/nginx.conf), under the included AGPL license. Nextcloud and Restic remain upstream applications; this repository supplies deployment and safety tooling.

See [verification results and limits](VERIFICATION.md) for checks actually completed.
