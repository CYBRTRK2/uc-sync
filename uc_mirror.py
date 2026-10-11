#!/usr/bin/python3
"""Verified, recoverable, add/update-only UC destination copies."""
import argparse
import datetime as dt
import errno
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from uc_sync_common import BASE, SOURCE, REPORT_NAMES, INDEX_NAMES, atomic_json, read_json, sha

_drive_path = read_json(BASE / 'destinations.json', {}).get('drive_path')
DRIVE = Path(_drive_path).expanduser() if _drive_path else None
LOG = logging.getLogger('uc-mirror')

class MountUnavailable(RuntimeError):
    pass

def now():
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')

def volume_info(path):
    result = subprocess.run(['/usr/sbin/diskutil', 'info', '-plist', str(path)], capture_output=True, timeout=15)
    return plistlib.loads(result.stdout) if result.returncode == 0 else {}

def find_ssd(uuid):
    # Query only the configured UUID: unrelated sleeping/problematic volumes must not block UC.
    info = volume_info(uuid)
    mount = info.get('MountPoint')
    if info.get('VolumeUUID', '').upper() == uuid.upper() and mount and info.get('Mounted', True):
        path = Path(mount)
        if not path.is_symlink() and os.path.ismount(path):
            return path
    return None


def target_location(target):
    if target == 'ssd':
        config = read_json(BASE / 'destinations.json', {})
        if not config.get('ssd_volume_uuid'):
            raise RuntimeError('SSD volume ID is not configured')
        mount = find_ssd(config['ssd_volume_uuid'])
        if mount is None:
            return None, None
        device = mount.stat().st_dev
        def guard():
            if not os.path.ismount(mount) or mount.stat().st_dev != device:
                raise MountUnavailable('SSD disconnected during copy')
        return mount / 'UC', guard
    def guard():
        if DRIVE is None or not DRIVE.is_dir():
            raise MountUnavailable('Google Drive Second Year folder unavailable')
    guard()
    return DRIVE, guard

def copy_one(source, destination, guard):
    guard()
    # Never follow a leftover partial symlink or replace through a symlink.
    if destination.is_symlink():
        raise RuntimeError('Destination is a symlink; left unchanged')
    expected_size = source.stat().st_size
    checksum = sha(source)
    if destination.is_file() and destination.stat().st_size == expected_size and sha(destination) == checksum:
        return 'unchanged'
    guard()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < expected_size + 64*1024*1024:
        raise OSError(errno.ENOSPC, 'Not enough free space for verified copy')
    partial = destination.with_name('.' + destination.name + '.uc-sync-part')
    if partial.is_symlink():
        raise RuntimeError('Unsafe partial-file symlink')
    existed = destination.exists()
    try:
        with source.open('rb') as inp, partial.open('wb') as out:
            size = 0
            for chunk in iter(lambda: inp.read(1024*1024), b''):
                size += len(chunk)
                if size > expected_size:
                    raise RuntimeError('Source changed while copying; retry required')
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if size != expected_size or sha(partial) != checksum:
            raise RuntimeError('Copy integrity check failed; original destination retained')
        stamp = source.stat().st_mtime
        os.utime(partial, (stamp, stamp))
        guard()
        os.replace(partial, destination)
        if sha(destination) != checksum:
            raise RuntimeError('Destination verification failed; retry required')
        return 'updated' if existed else 'new'
    finally:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            # If physically unplugged, the same partial is overwritten on reconnect.
            pass

def material_paths(source):
    def walk_error(e):
        raise e
    for root, dirs, files in os.walk(source, onerror=walk_error):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in ('__MACOSX', '__pycache__') and not (Path(root)/d).is_symlink())
        for name in sorted(files):
            if name.startswith('.') or name == 'Thumbs.db':
                continue
            path = Path(root) / name
            if path.is_symlink() or (path.parent == source and name in REPORT_NAMES):
                continue
            yield path

def mirror(source, destination, guard=lambda: None, report=None, checkpoint=lambda r: None, reports_only=False):
    report = report if report is not None else {}
    report.update(new=0, updated=0, unchanged=0, changes=[], generated_changes=[], errors=[])
    paths = [source/name for name in sorted(REPORT_NAMES)] if reports_only else material_paths(source)
    for src in paths:
        rel = src.relative_to(source)
        dst = destination / rel
        try:
            guard()
            if not dst.resolve().is_relative_to(destination.resolve()):
                raise RuntimeError('Destination path leaves the selected folder')
            # Retries are only for transient filesystem errors, not permissions or full disks.
            for attempt in range(3):
                try:
                    action = copy_one(src, dst, guard)
                    break
                except OSError as e:
                    if e.errno not in (errno.EAGAIN, errno.EBUSY, errno.ETIMEDOUT, errno.EINTR) or attempt == 2:
                        raise
                    time.sleep(attempt + 1)
            report[action] += 1
            if action != 'unchanged':
                entry = {'path': str(rel), 'action': action}
                key = 'generated_changes' if src.parent == source and src.name in INDEX_NAMES | REPORT_NAMES else 'changes'
                report[key].append(entry)
                LOG.info('%s: %s', action, rel)
                checkpoint(report)
        except Exception as e:
            report['errors'].append({'path': str(rel), 'error': str(e)})
            LOG.error('%s: %s', rel, e)
            checkpoint(report)
            if isinstance(e, MountUnavailable) or isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.ENODEV, errno.ENXIO):
                break
    return report

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=['drive', 'ssd'])
    parser.add_argument('--reports-only', action='store_true')
    args = parser.parse_args()
    handler = RotatingFileHandler(BASE/(args.target+'_mirror.log'), maxBytes=1024*1024, backupCount=2)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    LOG.addHandler(logging.StreamHandler())
    LOG.setLevel(logging.INFO)
    report = {'target': args.target, 'started_at': now(), 'finished_at': None, 'ok': False, 'status': 'running',
              'run_id': os.getenv('UC_SYNC_RUN_ID'), 'reports_only': args.reports_only}
    report_path = BASE / ('last-'+args.target+('-reports' if args.reports_only else '-mirror')+'.json')
    def checkpoint(value):
        atomic_json(report_path, value)
    with (BASE/(args.target+'_mirror.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.warning('Target mirror is busy; retry scheduled')
            return 75
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        checkpoint(report)
        try:
            if not SOURCE.is_dir():
                raise RuntimeError('Local UC source missing')
            destination, guard = target_location(args.target)
            if destination is None:
                report.update(ok=True, status='disconnected', skipped='SSD disconnected; catch-up on reconnect')
                return 0
            guard()
            report['destination'] = str(destination)
            mirror(SOURCE, destination, guard, report, checkpoint, args.reports_only)
            report['ok'] = not report['errors']
            report['status'] = 'ok' if report['ok'] else 'failed'
            LOG.info('%s: %d new, %d updated, %d unchanged, %d errors', args.target, report['new'], report['updated'], report['unchanged'], len(report['errors']))
            return int(not report['ok'])
        except Exception as e:
            report.update(status='failed', error=str(e))
            LOG.error('%s failed: %s', args.target, e)
            return 1
        finally:
            if report['status'] == 'running':
                report.update(status='interrupted', error='Sync interrupted; completed copies retained; next run will resume')
            report['finished_at'] = now()
            checkpoint(report)

if __name__ == '__main__':
    sys.exit(main())
