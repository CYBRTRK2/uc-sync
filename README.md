# UC Sync

Lightweight macOS sync for UC Student Hub teaching materials. Python's standard library handles authentication, repository traversal and downloads; a small native Objective-C watcher handles login, wake, network recovery and external-disk attachment. No Selenium, headless browser, ZIP extraction or browser caches.

## What it does

- Downloads new or changed material into `~/UC/<Subject>/`, retaining repository folder structure.
- Mirrors material to Google Drive's local **Second Year** folder and a configured external volume's `UC` folder.
- Uses SHA-256 verification, atomic partial files, stable source IDs, duplicate-name protection and source file-count checks.
- Preserves extra destination files. Source deletions are not propagated.
- Retries failed runs after five minutes and checks every three hours after success. Wake, login, restored connectivity, SSD attachment and Google Drive startup trigger earlier checks.
- Writes `Latest sync.md`, rolling `Sync log.md`, and `Upload dates.csv`/`.md` into the sync destinations. Logs/history are bounded.
- Uses the Student Hub's original upload-date text; it does not invent a timezone where the API supplies none.

The included subject list and repository keys target six courses in academic year **2026/2027**. Edit `YEAR`, `SUBJECTS` and `general_keys` in `uc_sync.py` to adapt the tool. This is an unofficial integration; Student Hub API changes may require maintenance.

## Setup

Requires macOS, Apple Command Line Tools (`xcode-select --install`, including `/usr/bin/python3`), and Google Drive for desktop signed in. No pip packages are needed.

1. Run `python3 -B install.py`. Existing program files are backed up before replacement. This installs but does not start the watcher.
2. Create `~/.config/uc-sync/email` containing your UC student email.
3. Store your password locally, without putting it in shell history:

   ```sh
   python3 -B - <<'PY'
   import getpass, os
   from pathlib import Path
   p = Path.home() / '.config/uc-sync/password'
   fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
   os.fchmod(fd, 0o600)
   with os.fdopen(fd, 'w') as f:
       f.write(getpass.getpass('UC password: ') + '\n')
   PY
   ```

4. Copy `destinations.example.json` to `~/.config/uc-sync/destinations.json`. Set `drive_path` to the real local Google Drive folder and `ssd_volume_uuid` to the external volume UUID reported by `diskutil info /Volumes/<volume>`. The UUID prevents writing to a lookalike folder when the SSD is absent or its mount name changes.
5. Run a manual sync:

   ```sh
   /usr/bin/python3 -B ~/.config/uc-sync/uc_sync_and_mirror.py
   ```

6. Enable the watcher:

   ```sh
   launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.manuel.ucbucketsync.plist"
   ```

For an existing loaded service, use `launchctl bootout` with the same domain/plist before bootstrapping the replacement. macOS may ask the newly built watcher for removable-volume access; allow it to use the configured SSD.

## Operation and troubleshooting

State lives in `~/.config/uc-sync/`. Inspect `last-pipeline.json`, `watcher-status.json` and the rotating `pipeline.log`, `sync.log`, `drive_mirror.log` and `ssd_mirror.log`. Session tokens stay in memory; the password file must remain mode 600. Never commit this runtime directory or coursework.

A successful Drive stage verifies the **local Drive-mounted copy**. Google Drive for desktop performs cloud upload independently; local success alone does not prove the cloud queue is empty. Disconnected destinations are retried. Failed or timed-out stages remain visible in reports and resume on the next run.

Stop the watcher using `launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.manuel.ucbucketsync.plist"`. This leaves downloaded files untouched.

## Verification

```sh
python3 -B -m unittest -v test_reliable_sync
mkdir -p build
xcrun clang -fobjc-arc -framework Cocoa -framework Network uc_sync_events.m -o build/uc_sync_events
build/uc_sync_events --test-events
```

The regression suite covers interrupted writes, checksum repair, duplicate names, manifest dates, stale stage reports, offline behavior and bounded history. Native tests use synthetic network/wake notifications; they do not physically sleep the Mac or unplug a volume.

The repository is a sanitized export of the installed tool. Publishing it does not replace or restart the existing service.
