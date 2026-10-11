#!/usr/bin/python3
"""UC materials sync: standard-library HTTPS only; no browser, cache or ZIP extraction."""
import argparse
import csv
import io
import datetime as dt
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import signal
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

BASE = Path.home() / '.config/uc-sync'
DEST_ROOT = Path.home() / 'UC'
YEAR = '2026/2027'
SUBJECTS = ['Artificial Intelligence and Problem Solving', 'Data Structures and Algorithms',
            'Knowledge, Reasoning and Planning', 'Linear Algebra and Analytical Geometry',
            'Sensing and Actuation Networks and Systems', 'Statistics for Data Science']
LOG = logging.getLogger('uc-sync')
CHANGES = []
RUN_ID = None


def safe_name(value):
    value = unicodedata.normalize('NFC', value).replace('/', '-').replace('\\', '-').strip()
    if value in ('', '.', '..') or any(ord(c) < 32 for c in value):
        raise ValueError('Unsafe remote filename')
    return value


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Unexpected API redirect')


class API:
    def __init__(self):
        self.headers = {'Accept': 'application/json', 'Accept-Language': 'en',
                        'FwApp': 'ucstudent|3.4.1|uc-sync', 'Content-Type': 'application/json'}
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, host, path, data=None, method=None):
        for attempt in range(3):
            request = urllib.request.Request('https://' + host + '.fw.uc.pt' + path,
                data=json.dumps(data).encode() if data is not None else None,
                headers=self.headers, method=method)
            try:
                with self.opener.open(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise RuntimeError('API %s %s returned HTTP %s' % (host, path, e.code)) from None
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise ConnectionError('API connection failed: ' + host) from None
            time.sleep(2 ** attempt)

    def login(self):
        pw = BASE / 'password'
        if pw.stat().st_mode & 0o077:
            raise RuntimeError('Password file must have mode 600')
        data = self.request('id', '/v1/login', {
            'email': (BASE / 'email').read_text().strip(), 'password': pw.read_text().strip(),
            'longLivedToken': False, 'callbackApp': None})
        self.headers['Authorization'] = data['token']

    def logout(self):
        if 'Authorization' in self.headers:
            try:
                self.request('id', '/v1/logout', method='DELETE')
            except Exception:
                LOG.warning('Session logout failed; token was not saved to disk')
            self.headers.pop('Authorization', None)

    def download(self, file, target, size):
        url = file.get('url')
        if file.get('url_format'):
            url = file['url_format'].replace('{TOKEN}', file['token']).replace('{KEY}', file['key']).replace('{FILENAME}', urllib.parse.quote(file['filename']))
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'storage.fw.uc.pt':
            raise RuntimeError('Unexpected download host')
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, urllib.parse.quote(parsed.path, safe='/%'), parsed.query, ''))
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name('.' + target.name + '.uc-sync-part')
        try:
            if shutil.disk_usage(target.parent).free < size + 128 * 1024 * 1024:
                raise RuntimeError('Insufficient free space for download')
            # Storage receives its scoped URL token, never the account Authorization header.
            with self.opener.open(url, timeout=120) as response, partial.open('wb') as out:
                received = 0
                for chunk in iter(lambda: response.read(1024 * 1024), b''):
                    received += len(chunk)
                    if received > size:
                        raise RuntimeError('Download exceeds advertised size; original file retained')
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if partial.stat().st_size != size:
                raise RuntimeError('Incomplete download: size does not match listing')
            checksum = digest(partial)
            same = target.is_file() and digest(target) == checksum
            if not same:
                os.replace(partial, target)
            return checksum, same
        except urllib.error.HTTPError as e:
            raise RuntimeError('File download failed: HTTP %s (token omitted)' % e.code) from None
        except (urllib.error.URLError, TimeoutError):
            raise ConnectionError('File download connection failed (token omitted)') from None
        finally:
            partial.unlink(missing_ok=True)


def listing(api, bucket, prefix):
    stack, visited, files = [(None, prefix)], set(), []
    while stack:
        folder, path = stack.pop()
        if folder in visited:
            raise RuntimeError('Repeated folder ID in repository')
        visited.add(folder)
        query = '?' + urllib.parse.urlencode({'folder_key': folder}) if folder else ''
        data = api.request('buckets', '/v1/bucket/' + bucket['key'] + query)
        if not isinstance(data.get('items'), list):
            raise RuntimeError('Unexpected folder listing schema')
        for item in data['items']:
            if item['type'] == 'folder':
                stack.append((item['key'], path / safe_name(item['title'])))
            elif item.get('file'):
                files.append((path / safe_name(item['file']['filename']), item, bucket['key']))
            else:
                raise RuntimeError('Unsupported repository item type: ' + item['type'])
    expected = bucket.get('stats', {}).get('files')
    if expected is not None and len(files) != expected:
        raise RuntimeError('Repository file count mismatch: expected %s, listed %s' % (expected, len(files)))
    return files


def fingerprint(item):
    file = item['file']
    return {'item_key': item['key'], 'file_key': file['key'],
            'size': int(item['metadata']['size']), 'created_date': item.get('created_date'),
            'updated_date': item.get('updated_date'), 'file_updated_date': file.get('updated_date')}


def write_json(path, data):
    tmp = path.with_suffix('.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def unique_paths(records, old):
    """Keep previously assigned names; suffix genuine collisions by stable item ID."""
    prior = {(v.get('bucket_key'), v.get('remote', {}).get('item_key')): Path(p) for p,v in old.items()}
    used, result = set(), []
    normalize = lambda p: unicodedata.normalize('NFC', str(p)).casefold()
    records = sorted(records, key=lambda r: ((r[2],r[1]['key']) not in prior, r[2],r[1]['key']))
    for rel,item,bucket in records:
        rel = prior.get((bucket,item['key']), rel)
        if normalize(rel) in used:
            rel = rel.with_name(rel.stem+' ['+safe_name(item['key'])+']'+rel.suffix)
        if normalize(rel) in used:
            raise RuntimeError('Duplicate item ID or destination collision')
        used.add(normalize(rel));result.append((rel,item,bucket))
    return result


def sync_subject(api, subject, buckets, dry_run=False):
    general_keys = dict(zip(SUBJECTS, ['8zsyam8n','1jewtxno','1zb4a31c','m93e9k6v','ir1j6igd','vt99buyy']))
    aliases = {subject}
    aliases.update(b['context']['unit_title'] for b in buckets if b['key'] == general_keys[subject])
    roots = [b for b in buckets if b.get('context', {}).get('unit_title') in aliases
             and b['context'].get('academic_year') == YEAR]
    if not roots or not any(b['type'] == 'unit' for b in roots):
        raise RuntimeError('Subject repository missing for ' + YEAR)
    manifest_path = BASE / 'manifests' / (subject.replace(' ', '_') + '.api.json')
    old = json.loads(manifest_path.read_text()).get('files', {}) if manifest_path.exists() else {}
    records = []
    for bucket in sorted(roots, key=lambda b: b['key']):
        prefix = Path('.') if bucket['type'] == 'unit' else Path(safe_name(bucket['context'].get('profile') or bucket['title']))
        records.extend(listing(api, bucket, prefix))
    records = unique_paths(records, old)
    new, downloads, unchanged, updated, state = 0, 0, 0, 0, {}
    root = DEST_ROOT / subject
    for rel, item, bucket_key in records:
        target = root / rel
        if not target.resolve().is_relative_to(DEST_ROOT.resolve()):
            raise RuntimeError('Destination escapes UC folder')
        metadata = fingerprint(item)
        previous = old.get(str(rel), {})
        valid = target.is_file() and target.stat().st_size == metadata['size']
        if valid and previous.get('remote') == metadata and previous.get('sha256') == digest(target):
            state[str(rel)] = previous
            unchanged += 1
            continue
        if dry_run:
            LOG.info('Would verify/download: %s/%s', subject, rel)
            downloads += 1
            continue
        existed = target.exists()
        checksum, same = api.download(item['file'], target, metadata['size'])
        downloads += 1
        new += int(not existed)
        date = metadata['updated_date'] or metadata['file_updated_date'] or metadata['created_date']
        if date:
            # API timestamps have no timezone: retain exact source text in manifest.
            stamp = dt.datetime.fromisoformat(date.replace('Z', '+00:00')).timestamp()
            os.utime(target, (stamp, stamp))
        state[str(rel)] = {'remote': metadata, 'sha256': checksum, 'bucket_key': bucket_key}
        if not same:
            updated += int(existed)
            CHANGES.append({'path': str(Path(subject)/rel), 'action': 'updated' if existed else 'new',
                            'uploaded_at': metadata['created_date'], 'repository': 'https://ucstudent.uc.pt/bucket/'+bucket_key})
            write_json(BASE/'gather-progress.json', {'run_id': RUN_ID, 'changes': CHANGES})
        checkpoint = dict(old)
        checkpoint.update(state)
        write_json(manifest_path, {'schema': 1, 'academic_year': YEAR, 'files': checkpoint})
        LOG.info('%s: %s/%s', 'Verified' if same else 'Downloaded', subject, rel)
    if not dry_run:
        write_json(manifest_path, {'schema': 1, 'academic_year': YEAR, 'files': state})
    LOG.info('%s: %d repositories, %d files; %d downloads (%d new), %d unchanged',
             subject, len(roots), len(records), downloads, new, unchanged)
    return {'subject': subject, 'repositories': len(roots), 'files': len(records), 'downloads': downloads, 'new': new, 'updated': updated, 'unchanged': unchanged}


def export_upload_dates(failed=()):
    rows = []
    for subject in SUBJECTS:
        manifest = BASE / 'manifests' / (subject.replace(' ', '_') + '.api.json')
        if not manifest.exists():
            continue
        data = json.loads(manifest.read_text())
        for name, record in sorted(data.get('files', {}).items()):
            remote = record['remote']
            rows.append([subject, name, remote.get('created_date') or '',
                         remote.get('updated_date') or remote.get('file_updated_date') or '',
                         'https://ucstudent.uc.pt/bucket/' + record['bucket_key'],
                         'Last known listing; latest check failed' if subject in failed or 'API' in failed else 'Checked successfully'])
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(['Subject', 'File', 'Uploaded to repository (source timestamp)',
                     'Modified in repository (when supplied)', 'Repository URL', 'Listing status'])
    writer.writerows(rows)
    text = ['# Repository upload dates', '',
            'These are UC Student Hub repository entry dates (`created_date`), not the date downloaded to this Mac.',
            'Timestamps are preserved exactly as supplied. The API does not specify a timezone for these values; no timezone conversion is inferred.',
            'A blank modified date means the API did not supply one. Extra local files and extracted ZIP contents have no separate repository upload date and are not listed.', '',
            '[Open the CSV index](Upload%20dates.csv)', '']
    escape = lambda value: str(value).replace('|', r'\|').replace('\n', ' ')
    for subject in SUBJECTS:
        text.extend(['## ' + subject, '', '| File | Uploaded (source timestamp) | Modified | Repository | Status |',
                     '| --- | --- | --- | --- | --- |'])
        for sub, name, uploaded, modified, url, status in rows:
            if sub == subject:
                text.append('| ' + ' | '.join([escape(name), uploaded or 'Not supplied', modified or 'Not supplied', '[Open]('+url+')', status]) + ' |')
        text.append('')
    DEST_ROOT.mkdir(parents=True, exist_ok=True)
    for name, content in [('Upload dates.csv', output.getvalue()), ('Upload dates.md', '\n'.join(text))]:
        path = DEST_ROOT / name
        if path.exists() and path.read_text() == content:
            continue
        tmp = path.with_name('.' + path.name + '.uc-sync-part')
        try:
            tmp.write_text(content)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    LOG.info('Upload-date index: %d repository files', len(rows))


def main():
    global RUN_ID
    RUN_ID = os.getenv("UC_SYNC_RUN_ID")
    CHANGES.clear()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--subject', choices=SUBJECTS)
    args = parser.parse_args()
    BASE.mkdir(parents=True, exist_ok=True)
    (BASE / 'manifests').mkdir(exist_ok=True)
    LOG.setLevel(logging.INFO)
    handler = RotatingFileHandler(BASE / 'sync.log', maxBytes=1024*1024, backupCount=2)
    for h in (handler, logging.StreamHandler()):
        h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        LOG.addHandler(h)
    with (BASE / 'sync.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error('Another gatherer is running')
            return 1
        lock.seek(0); lock.truncate(); lock.write(str(os.getpid())); lock.flush()
        # Only remove our own interrupted download files, under the exclusive lock.
        for subject in SUBJECTS:
            for partial in (DEST_ROOT / subject).rglob('.*.uc-sync-part'):
                partial.unlink()
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        start, api, results, errors = time.monotonic(), API(), [], []
        started_at = dt.datetime.now().astimezone().isoformat()
        error_details = {}
        if not args.dry_run:
            write_json(BASE/'gather-progress.json', {'run_id': RUN_ID, 'changes': []})
        try:
            LOG.info('API sync started%s', ' (dry run)' if args.dry_run else '')
            api.login()
            buckets = api.request('buckets', '/v1/buckets')
            if not isinstance(buckets, list):
                raise RuntimeError('Unexpected repository listing schema')
            selected = [args.subject or os.getenv('TEST_ONE')] if args.subject or os.getenv('TEST_ONE') else SUBJECTS
            for subject in selected:
                try:
                    results.append(sync_subject(api, subject, buckets, args.dry_run))
                except Exception as e:
                    errors.append(subject)
                    error_details[subject] = str(e)
                    LOG.error('%s: %s', subject, e)
                    if isinstance(e, ConnectionError):
                        errors.append('API')
                        break
        except Exception as e:
            errors.append('API')
            error_details['API'] = str(e)
            LOG.error('%s', e)
        finally:
            api.logout()
        if not args.dry_run:
            try:
                export_upload_dates(errors)
            except Exception as e:
                errors.append('upload-date-index')
                LOG.error('Upload-date export failed: %s', e)
        report = {'run_id': RUN_ID, 'started_at': started_at, 'changes': CHANGES, 'error_details': error_details, 'status': 'failed' if errors else 'ok', 'ok': not errors, 'finished_at': dt.datetime.now().astimezone().isoformat(),
                  'seconds': round(time.monotonic()-start, 2), 'subjects': results, 'failed': errors, 'dry_run': args.dry_run}
        write_json(BASE / 'last-run.json', report)
        LOG.info('API sync %s in %.2fs', 'FAILED' if errors else 'completed', report['seconds'])
        return int(bool(errors))


if __name__ == '__main__':
    sys.exit(main())
