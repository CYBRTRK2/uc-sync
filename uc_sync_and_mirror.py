#!/usr/bin/python3
"""Recoverable UC pipeline: independent targets, truthful reports, bounded execution."""
import datetime as dt
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from uc_sync_common import BASE, atomic_json, read_json
from uc_reports import save_reports

LOG = logging.getLogger('uc-pipeline')
CHILD = None

def now():
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')

def check_url(url):
    try:
        request = urllib.request.Request(url, headers={'User-Agent':'UC-Sync/2'})
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read(1)
            return True
    except urllib.error.HTTPError:
        # Server responses prove connectivity, including 404/503; the gatherer records API errors.
        return True
    except (OSError, ValueError):
        return False

def network_probe():
    if os.getenv('UC_NETWORK_AVAILABLE') == '0':
        return {'online':False, 'hub_reachable':False, 'detail':'macOS reports no usable network path'}
    hub = check_url('https://ucstudent.uc.pt/login')
    online = hub or check_url('https://www.google.com/generate_204')
    return {'online':online, 'hub_reachable':hub,
            'detail':'Connection checked' if online else 'Internet endpoints unavailable; local copies will still be processed'}

def terminate_child():
    global CHILD
    if CHILD is not None and CHILD.poll() is None:
        CHILD.terminate()
        try:
            CHILD.wait(timeout=8)
        except subprocess.TimeoutExpired:
            CHILD.kill()
            CHILD.wait(timeout=5)

def interrupted(signum, frame):
    terminate_child()
    raise KeyboardInterrupt('Pipeline interrupted')

def run_stage(name, args, timeout, result_path, run_id):
    global CHILD
    env = os.environ.copy()
    env['UC_SYNC_RUN_ID'] = run_id
    started = now()
    try:
        CHILD = subprocess.Popen(['/usr/bin/python3','-B',str(BASE/args[0])]+args[1:],
                                 env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        code = CHILD.wait(timeout=timeout)
        result = read_json(BASE/result_path,{})
        if result.get('run_id') != run_id:
            result = {'status':'failed','error':'Stage did not write a fresh result (possibly already running)'}
        result['exit_code'] = code
        if code and result.get('status') not in ('failed','interrupted','timeout'):
            result['status'] = 'failed'
        result['ok'] = code == 0 and result.get('ok',False)
    except subprocess.TimeoutExpired:
        terminate_child()
        result = read_json(BASE/result_path,{})
        if result.get('run_id') != run_id:
            result = {}
        result.update(ok=False,status='timeout',error='%s timed out after %s seconds; next run will resume' % (name,timeout))
    except OSError as e:
        result = {'ok':False,'status':'failed','error':str(e)}
    finally:
        CHILD = None
    if name=='gather' and not result.get('ok'):
        progress = read_json(BASE/'gather-progress.json',{})
        if progress.get('run_id') == run_id:
            result['changes'] = progress.get('changes',[])
    result.setdefault('started_at',started)
    result.setdefault('finished_at',now())
    result.setdefault('status','ok' if result.get('ok') else 'failed')
    result.setdefault('changes',[])
    LOG.info('%s stage: %s',name,result['status'])
    return result

def offline_stage(detail):
    return {'ok':False,'status':'offline','error':detail,'changes':[]}

def publish_reports(run_id, network):
    results = {}
    for target in ('ssd','drive'):
        if target=='drive' and not network['online']:
            results[target] = offline_stage('Report saved locally; Drive publication waits for network')
            continue
        results[target] = run_stage(target+'-reports',['uc_mirror.py',target,'--reports-only'],90,
                                    'last-'+target+'-reports.json',run_id)
    return results

def recover_interrupted_run():
    previous = read_json(BASE/'current-pipeline.json',{})
    if not previous.get('run_id') or previous.get('finished_at'):
        return
    previous.setdefault('stages',{})
    for stage,filename in [('gather','last-run.json'),('ssd','last-ssd-mirror.json'),('drive','last-drive-mirror.json')]:
        fresh = read_json(BASE/filename,{})
        if fresh.get('run_id') == previous['run_id'] and fresh.get('finished_at'):
            previous['stages'][stage] = fresh
        else:
            previous['stages'][stage] = {'ok':False,'status':'interrupted','error':'Previous process stopped before completion; resumed by this run','changes':[]}
    progress = read_json(BASE/'gather-progress.json',{})
    if progress.get('run_id') == previous['run_id']:
        previous['stages']['gather']['changes'] = progress.get('changes',[])
    previous.update(ok=False,finished_at=now(),seconds=0,recovered_after_interruption=True)
    save_reports(previous)
    LOG.warning('Recovered report for interrupted run %s',previous['run_id'])


def pipeline(run_id, trigger):
    recover_interrupted_run()
    start = time.monotonic()
    run = {'run_id':run_id,'started_at':now(),'trigger':trigger,'stages':{},'ok':False}
    # Save a small current state before potentially slow network or filesystem calls.
    atomic_json(BASE/'current-pipeline.json',run)
    network = network_probe()
    run['network'] = network
    try:
        if network['hub_reachable']:
            run['stages']['gather'] = run_stage('gather',['uc_sync.py'],1800,'last-run.json',run_id)
        else:
            run['stages']['gather'] = offline_stage('Student Hub not reachable; latest local material retained')
            # Prevent last-known source data from being labelled a fresh successful scan.
            import uc_sync
            uc_sync.export_upload_dates(['API'])
        atomic_json(BASE/'current-pipeline.json',run)
        run['stages']['ssd'] = run_stage('ssd',['uc_mirror.py','ssd'],600,'last-ssd-mirror.json',run_id)
        if network['online']:
            # Drive manages its own background process and login startup. Sending
            # it an open event on each sync can reopen its window even with -gj.
            # Leave unavailable destinations queued for the existing retry path.
            run['stages']['drive'] = run_stage('drive',['uc_mirror.py','drive'],600,'last-drive-mirror.json',run_id)
        else:
            run['stages']['drive'] = offline_stage('No Internet connection; pending files remain on the Mac')
        run['ok'] = all(r.get('ok',False) for r in run['stages'].values())
    except (Exception,KeyboardInterrupt) as e:
        LOG.error('Pipeline interrupted/failed: %s',e)
        for stage in ('gather','ssd','drive'):
            run['stages'].setdefault(stage,{'ok':False,'status':'interrupted','error':'Pipeline interrupted before this stage finished','changes':[]})
        run['ok'] = False
    run.update(finished_at=now(),seconds=round(time.monotonic()-start,1))
    atomic_json(BASE/'last-pipeline.json',run)
    try:
        save_reports(run)
        run['report_publication'] = publish_reports(run_id,network)
        run['reports_ok'] = all(r.get('ok',False) for r in run['report_publication'].values())
    except Exception as e:
        run['reports_ok'] = False
        run['report_error'] = str(e)
        LOG.error('Report generation/publication failed: %s',e)
    run['ok'] = run['ok'] and run['reports_ok']
    atomic_json(BASE/'last-pipeline.json',run)
    atomic_json(BASE/'current-pipeline.json',run)
    LOG.info('Pipeline finished: %s; reports %s',run['ok'],run['reports_ok'])
    return int(not run['ok'])

def run():
    BASE.mkdir(parents=True,exist_ok=True)
    LOG.setLevel(logging.INFO)
    handler = RotatingFileHandler(BASE/'pipeline.log',maxBytes=1024*1024,backupCount=2)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    with (BASE/'pipeline.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info('Pipeline busy; event queued for retry')
            return 75
        signal.signal(signal.SIGTERM,interrupted)
        trigger = os.getenv('UC_SYNC_TRIGGER','manual')
        LOG.info('Pipeline triggered: %s',trigger)
        return pipeline(str(uuid.uuid4()),trigger)

if __name__=='__main__':
    sys.exit(run())
