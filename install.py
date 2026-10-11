#!/usr/bin/python3
"""Install source and build the native watcher; never start syncing implicitly."""
from pathlib import Path
import datetime,plistlib,shutil,subprocess
root=Path(__file__).resolve().parent
base=Path.home()/'.config/uc-sync';base.mkdir(parents=True,exist_ok=True)
names=['uc_sync.py','uc_sync_common.py','uc_mirror.py','uc_reports.py','uc_sync_and_mirror.py','uc_sync_and_mirror.sh','uc_drive_mirror.py','uc_sync_events.m']
# Compile before changing an existing installation.
build=root/'build';build.mkdir(exist_ok=True)
subprocess.run(['xcrun','clang','-fobjc-arc','-framework','Cocoa','-framework','Network',str(root/'uc_sync_events.m'),'-o',str(build/'uc_sync_events')],check=True)
backup=base/'backups'/datetime.datetime.now().strftime('install-%Y%m%d-%H%M%S')
for name in names+['uc_sync_events']:
 if (base/name).exists():
  backup.mkdir(parents=True,exist_ok=True);shutil.copy2(base/name,backup/name)
for name in names:shutil.copy2(root/name,base/name)
shutil.copy2(build/'uc_sync_events',base/'uc_sync_events')
agent=Path.home()/'Library/LaunchAgents/com.manuel.ucbucketsync.plist';agent.parent.mkdir(parents=True,exist_ok=True)
agent.write_bytes(plistlib.dumps({'Label':'com.manuel.ucbucketsync','ProgramArguments':[str(base/'uc_sync_events')],'KeepAlive':True,'RunAtLoad':True,'ProcessType':'Background','ThrottleInterval':30,'StandardOutPath':'/dev/null','StandardErrorPath':'/dev/null'}))
print('Installed. Configure email, password, and destinations.json, then follow README to enable the service.')
