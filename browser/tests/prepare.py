"""Prepare a disposable, account-free browser fixture. Never inspect a profile."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import wave

root, test = map(Path, sys.argv[1:])
test.chmod(0o755)
for name in ['install', 'state', 'policy', 'etc-upper', 'etc-work', 'home', 'runtime']:
    (test/name).mkdir()
shutil.copytree(root/'stopscroll', test/'install/stopscroll')
shutil.copytree(root/'browser', test/'install/browser')
subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(test/'key.pem'),'-out',str(test/'cert.pem'),'-days','1','-subj','/CN=www.youtube.com'],check=True,capture_output=True)
with wave.open(str(test/'media.wav'),'wb') as media:
    media.setnchannels(1); media.setsampwidth(2); media.setframerate(8000)
    media.writeframes(b'\x00\x00'*8000*30)
legacy=test/'legacy';legacy.mkdir()
(legacy/'manifest.json').write_text(json.dumps({'manifest_version':3,'name':'Existing mandatory fixture','version':'1.0'}))
subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(test/'legacy.pem')],check=True,capture_output=True)
public=subprocess.check_output(['openssl','pkey','-in',str(test/'legacy.pem'),'-pubout','-outform','DER'])
id=''.join(chr(97+int(n,16)) for n in hashlib.sha256(public).hexdigest()[:32])
subprocess.run(['/opt/google/chrome/chrome','--headless','--no-sandbox','--no-message-box',f'--user-data-dir={test}/pack-profile',f'--pack-extension={legacy}',f'--pack-extension-key={test}/legacy.pem'],check=True,capture_output=True,timeout=30)
(test/'legacy.xml').write_text(f'<gupdate xmlns="http://www.google.com/update2/response" protocol="2.0"><app appid="{id}"><updatecheck codebase="file://{test}/legacy.crx" version="1.0"/></app></gupdate>')
(test/'legacy-policy.json').write_text(json.dumps({'ExtensionInstallForcelist':[f'{id};file://{test}/legacy.xml']}))
(test/'legacy-id').write_text(id)
