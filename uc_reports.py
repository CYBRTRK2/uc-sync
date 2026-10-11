"""Human-readable per-sync documents with bounded, useful history."""
from pathlib import Path
from uc_sync_common import BASE, SOURCE, atomic_json, atomic_text, read_json

MAX_RUNS = 30
MAX_LOG_BYTES = 384 * 1024
MAX_DETAIL_ROWS = 300
TRIGGERS = {'network-restored':'Internet/network connection restored', 'wake':'Mac woke from sleep',
            'login-or-service-start':'Login / background service started', 'extreme-mounted':'Extreme SSD connected',
            'three-hour-check':'Scheduled three-hour check', 'retry-after-failure':'Retry after an incomplete sync',
            'drive-started':'Google Drive started', 'manual':'Manual sync'}

def clean(value):
    return str(value).replace('\r',' ').replace('\n',' ').replace('`','\\`').replace('|','\\|')

def stage_summary(result):
    status = result.get('status', 'unknown')
    if status == 'disconnected':
        return 'Not connected — will catch up when plugged in'
    if status == 'offline':
        return 'Waiting for network — will retry'
    if status == 'ok':
        if 'subjects' in result:
            return '%d repository files checked' % sum(x.get('files',0) for x in result['subjects'])
        generated = len(result.get('generated_changes',[]))
        return '%d added, %d updated, %d unchanged%s' % (sum(c['action']=='new' for c in result.get('changes',[])), sum(c['action']=='updated' for c in result.get('changes',[])), result.get('unchanged',0), '; %d index/report files refreshed' % generated if generated else '')
    return status.upper() + ': ' + clean(result.get('error') or '; '.join(result.get('failed',[])) or 'See errors below')

def change_lines(title, changes):
    if not changes:
        return []
    lines = ['### '+title, '']
    for entry in changes[:MAX_DETAIL_ROWS]:
        action = 'Added' if entry.get('action')=='new' else 'Updated'
        suffix = ' — repository upload: '+clean(entry['uploaded_at']) if entry.get('uploaded_at') else ''
        lines.append('- '+action+': `'+clean(entry['path'])+'`'+suffix)
    if len(changes)>MAX_DETAIL_ROWS:
        lines.append('- %d additional changes omitted from this small document; full current details remain in the local run reports.' % (len(changes)-MAX_DETAIL_ROWS))
    return lines + ['']

def render_run(run, heading='#'):
    status = 'Complete' if run['ok'] else 'Incomplete — retry queued'
    trigger = ', '.join(TRIGGERS.get(x,x) for x in run['trigger'].split(', '))
    lines = [heading+' UC sync — '+run['finished_at'], '', '**'+status+'**', '',
             '- Started: '+run['started_at'], '- Trigger: '+trigger,
             '- Duration: %.1f seconds' % run.get('seconds',0), '',
             '| Stage | Result |', '| --- | --- |']
    for key,label in [('gather','Student Hub → Mac'),('ssd','Mac → Extreme/UC'),('drive','Mac → Drive/Second Year')]:
        lines.append('| '+label+' | '+stage_summary(run['stages'][key])+' |')
    lines += ['']
    changes = run['stages']['gather'].get('changes',[])
    if changes:
        lines += change_lines('Files downloaded or repaired on the Mac',changes)
    elif run['stages']['gather'].get('status')=='ok':
        lines += ['No new or changed repository files were downloaded in this run.', '']
    else:
        lines += ['The latest repository state could not be fully checked. Available local files are retained.', '']
    for key,label in [('ssd','Files caught up on the Extreme SSD'),('drive','Files caught up in the Drive folder')]:
        lines += change_lines(label,run['stages'][key].get('changes',[]))
    errors = []
    for key,stage in run['stages'].items():
        if stage.get('error'):
            errors.append(key+': '+stage['error'])
        for item in stage.get('errors',[]):
            errors.append(key+': '+item['path']+': '+item['error'])
        for subject,error in stage.get('error_details',{}).items():
            errors.append(subject+': '+error)
    if errors:
        lines += ['### Errors / items to retry','']+['- '+clean(e) for e in errors[:30]]+['']
    lines += ['Drive results describe verified copies in the local Google Drive folder. Drive for desktop performs the cloud upload; this report does not claim independent cloud verification.',
              'Repository upload times are preserved as supplied by UC; the API does not specify their timezone.', '']
    return '\n'.join(lines)

def save_reports(run):
    history = read_json(BASE/'sync-history.json', [])
    history = [entry for entry in history if entry.get('run_id') != run['run_id']]
    history.append(run)
    history = history[-MAX_RUNS:]
    # Retain concise change records in history, and full current per-stage reports separately.
    for entry in history:
        for stage in entry.get('stages',{}).values():
            for field in ('changes','generated_changes','errors'):
                if len(stage.get(field,[])) > MAX_DETAIL_ROWS:
                    stage[field+'_total'] = len(stage[field])
                    stage[field] = stage[field][:MAX_DETAIL_ROWS]
    def history_text():
        return '# UC sync log\n\nLatest %d runs. This rolling log is bounded to 30 runs and 384 KiB; older entries roll off.\n\n' % len(history) + '\n---\n\n'.join(render_run(r,'##') for r in reversed(history))
    text = history_text()
    while len(text.encode('utf-8')) > MAX_LOG_BYTES and len(history)>1:
        history.pop(0)
        text = history_text()
    def bounded(value):
        encoded = value.encode('utf-8')
        if len(encoded) <= MAX_LOG_BYTES:
            return value
        return encoded[:MAX_LOG_BYTES-200].decode('utf-8', errors='ignore') + '\n\nFurther details omitted to keep this document small; the local current-stage JSON reports retain the full details.\n'
    atomic_json(BASE/'sync-history.json',history)
    atomic_text(SOURCE/'Latest sync.md',bounded(render_run(run)))
    atomic_text(SOURCE/'Sync log.md',bounded(text))
