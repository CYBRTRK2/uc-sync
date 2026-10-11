import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uc_sync as gather
import uc_mirror as mirror
import uc_reports as reports
import uc_sync_and_mirror as pipeline
from uc_sync_common import sha

class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.base=self.root/'config';self.base.mkdir();(self.base/'manifests').mkdir()
        self.source=self.root/'UC';self.source.mkdir()
        self.dest=self.root/'destination';self.dest.mkdir()
        self.patches=[]
        for mod,key,value in [(gather,'BASE',self.base),(gather,'DEST_ROOT',self.source),(mirror,'BASE',self.base),(mirror,'SOURCE',self.source),(reports,'BASE',self.base),(reports,'SOURCE',self.source),(pipeline,'BASE',self.base)]:
            p=patch.object(mod,key,value);p.start();self.patches.append(p)
        gather.RUN_ID='test-run';gather.CHANGES.clear()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def test_same_size_and_timestamp_repair_no_deletion(self):
        a=self.source/'a';b=self.dest/'a';a.write_bytes(b'good');b.write_bytes(b'evil')
        os.utime(a,(1000,1000));os.utime(b,(1000,1000))
        (self.dest/'personal').write_bytes(b'keep')
        r=mirror.mirror(self.source,self.dest)
        self.assertEqual(r['updated'],1);self.assertEqual(b.read_bytes(),b'good')
        self.assertEqual((self.dest/'personal').read_bytes(),b'keep')
        self.assertEqual(mirror.mirror(self.source,self.dest)['unchanged'],1)
    def test_interrupted_copy_keeps_original(self):
        a=self.source/'a';b=self.dest/'a';a.write_bytes(b'good');b.write_bytes(b'old')
        original=mirror.sha
        def fail_partial(path):
            return 'incorrect' if str(path).endswith('.uc-sync-part') else original(path)
        with patch.object(mirror,'sha',side_effect=fail_partial):
            r=mirror.mirror(self.source,self.dest)
        self.assertEqual(len(r['errors']),1);self.assertEqual(b.read_bytes(),b'old')
        self.assertFalse(list(self.dest.glob('.*.uc-sync-part')))
    def test_unplug_preserves_completed_progress_and_stops(self):
        for name in ['a','b','c']:(self.source/name).write_text(name)
        real=mirror.copy_one
        def copy_file(src,dst,guard):
            if src.name=='b':raise mirror.MountUnavailable('unplugged')
            return real(src,dst,guard)
        checkpoints=[]
        with patch.object(mirror,'copy_one',side_effect=copy_file):
            r=mirror.mirror(self.source,self.dest,checkpoint=lambda v:checkpoints.append(copy.deepcopy(v)))
        self.assertEqual(r['new'],1);self.assertFalse((self.dest/'c').exists())
        self.assertEqual(checkpoints[-1]['changes'][0]['path'],'a')
        self.assertEqual(mirror.mirror(self.source,self.dest)['new'],2)
    def test_reports_not_counted_as_material_changes(self):
        (self.source/'a').write_text('a');(self.source/'Latest sync.md').write_text('report')
        (self.source/'Sync log.md').write_text('history')
        (self.source/'__pycache__').mkdir();(self.source/'__pycache__'/'a.pyc').write_text('cache')
        r=mirror.mirror(self.source,self.dest)
        self.assertEqual(r['new'],1);self.assertFalse((self.dest/'Latest sync.md').exists())
        r=mirror.mirror(self.source,self.dest,reports_only=True)
        self.assertEqual(r['new'],2);self.assertEqual(r['changes'],[])
    def test_partial_gather_checkpoint_and_source_dates(self):
        subject=gather.SUBJECTS[0]
        def item(key):return {'key':key,'type':'file','file':{'key':'file-'+key,'filename':key+'.pdf'},'metadata':{'size':4},'created_date':'2026-09-27T13:33:57.407747'}
        class API:
            def request(self,*a):return {'items':[item('a'),item('b')]}
            def download(self,file,target,size):
                if file['key']=='file-b':raise ConnectionError('offline')
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'data');return sha(target),False
        buckets=[{'key':'8zsyam8n','type':'unit','stats':{'files':2},'context':{'unit_title':subject,'academic_year':gather.YEAR}}]
        with self.assertRaises(ConnectionError):gather.sync_subject(API(),subject,buckets)
        manifest=json.loads(next((self.base/'manifests').glob('*.json')).read_text())
        self.assertIn('a.pdf',manifest['files'])
        self.assertEqual(gather.CHANGES[0]['uploaded_at'],'2026-09-27T13:33:57.407747')
        self.assertEqual(json.loads((self.base/'gather-progress.json').read_text())['run_id'],'test-run')
    def test_oversized_download_never_replaces_original(self):
        api=gather.API();target=self.source/'a';target.write_bytes(b'old')
        with patch.object(api.opener,'open',return_value=io.BytesIO(b'oversized')):
            with self.assertRaises(RuntimeError):api.download({'url':'https://storage.fw.uc.pt/test'},target,3)
        self.assertEqual(target.read_bytes(),b'old');self.assertFalse(list(self.source.glob('.*.uc-sync-part')))
    def fake_stage(self,name,args,timeout,result_path,run_id):
        if name=='gather':return {'ok':False,'status':'failed','error':'one repository unavailable','changes':[{'path':'subject/a.pdf','action':'new'}]}
        return {'ok':True,'status':'ok','new':0,'updated':0,'unchanged':1,'changes':[]}
    def test_failed_gather_does_not_block_mirrors_and_reports(self):
        calls=[]
        def stage(*args):calls.append(args[0]);return self.fake_stage(*args)
        with patch.object(pipeline,'network_probe',return_value={'online':True,'hub_reachable':True}),patch.object(pipeline,'run_stage',side_effect=stage),patch.object(pipeline.subprocess,'run'):
            code=pipeline.pipeline('run-one','manual')
        self.assertEqual(code,1);self.assertEqual(calls,['gather','ssd','drive','ssd-reports','drive-reports'])
        doc=(self.source/'Latest sync.md').read_text();self.assertIn('Incomplete',doc);self.assertIn('subject/a.pdf',doc)
    def test_offline_writes_history_and_still_syncs_ssd(self):
        calls=[]
        def stage(*args):calls.append(args[0]);return {'ok':True,'status':'ok','changes':[],'new':0,'updated':0,'unchanged':1}
        with patch.object(pipeline,'network_probe',return_value={'online':False,'hub_reachable':False}),patch.object(pipeline,'run_stage',side_effect=stage):
            code=pipeline.pipeline('offline','wake')
        self.assertEqual(code,1);self.assertEqual(calls,['ssd','ssd-reports'])
        self.assertIn('Waiting for network',(self.source/'Latest sync.md').read_text())
        with patch.object(pipeline,'network_probe',return_value={'online':True,'hub_reachable':True}),patch.object(pipeline,'run_stage',side_effect=stage),patch.object(pipeline.subprocess,'run'):
            self.assertEqual(pipeline.pipeline('online','network-restored'),0)
        text=(self.source/'Sync log.md').read_text();self.assertIn('Waiting for network',text);self.assertIn('connection restored',text)
    def test_stale_stage_report_is_not_success(self):
        (self.base/'last-run.json').write_text(json.dumps({'run_id':'old','ok':True}))
        with patch.object(pipeline.subprocess,'Popen') as process:
            process.return_value.wait.return_value=0
            result=pipeline.run_stage('gather',['uc_sync.py'],1,'last-run.json','new')
        self.assertFalse(result['ok']);self.assertEqual(result['status'],'failed')
    def test_unfinished_previous_run_is_reported_on_recovery(self):
        (self.base/'current-pipeline.json').write_text(json.dumps({'run_id':'crashed','started_at':'2026-09-27T00:00:00+01:00','trigger':'wake','stages':{}}))
        (self.base/'gather-progress.json').write_text(json.dumps({'run_id':'crashed','changes':[{'path':'subject/saved.pdf','action':'new'}]}))
        pipeline.recover_interrupted_run()
        text=(self.source/'Sync log.md').read_text()
        self.assertIn('Incomplete',text);self.assertIn('subject/saved.pdf',text);self.assertIn('Previous process stopped',text)

    def test_bounded_history(self):
        base={'started_at':'2026-09-27T00:00:00+01:00','finished_at':'2026-09-27T00:00:01+01:00','trigger':'manual','seconds':1,'ok':True,
              'stages':{k:{'ok':True,'status':'ok','changes':[]} for k in ['gather','ssd','drive']}}
        for i in range(40):reports.save_reports(dict(base,run_id=str(i)))
        history=json.loads((self.base/'sync-history.json').read_text())
        self.assertEqual(len(history),30);self.assertEqual(history[0]['run_id'],'10')
        self.assertLessEqual((self.source/'Sync log.md').stat().st_size,reports.MAX_LOG_BYTES)

if __name__=='__main__':unittest.main(verbosity=2)
