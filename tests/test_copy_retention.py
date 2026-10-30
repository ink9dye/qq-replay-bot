import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from bot.copy_retention import purge,next_delay


class CopyRetentionTests(unittest.TestCase):
    def test_workspace_scope_is_limited_to_owned_preview_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);data=root/'production';data.mkdir()
            workspace=root/'workspace';workspace.mkdir()
            preview=workspace/'phase1/preview-data';preview.mkdir(parents=True)
            owned=preview/'preview.sqlite3';owned.write_bytes(b'copied fact')
            other=workspace/'other-chat.json';other.write_bytes(b'preserve')
            rows=[dict(scope='workspace_validation',path=str(p.relative_to(workspace)),expires_at=10,
                sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in (owned,other)]
            (data/'fact-copy-retention.json').write_text(json.dumps(dict(entries=rows)))
            result=purge(data,10,workspace)
            self.assertEqual(result['removed'],1)
            self.assertTrue(other.exists())

    def test_original_expiry_deletes_only_registered_unchanged_data_and_keeps_source(self):
        with tempfile.TemporaryDirectory() as directory:
            data=Path(directory);root=data/'code-backups';root.mkdir()
            old=root/'facts.sqlite3';old.write_bytes(b'old facts')
            source=root/'source.py';source.write_text('source')
            changed=root/'changed.json';changed.write_bytes(b'old')
            rows=[dict(path=p.name,expires_at=100,sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in (old,changed)]
            (data/'fact-copy-retention.json').write_text(json.dumps(dict(entries=rows)))
            changed.write_bytes(b'other chat updated it')
            self.assertEqual(purge(data,99)['removed'],0)
            self.assertEqual(next_delay(data,99),1)
            result=purge(data,100)
            self.assertEqual(result['removed'],1)
            self.assertEqual(result['blocked'],['changed.json'])
            self.assertTrue(source.exists());self.assertTrue(changed.exists())

    def test_invalid_or_outside_paths_never_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            data=Path(directory);(data/'code-backups').mkdir()
            other=data/'other';other.write_bytes(b'fact')
            row=dict(path='../other',expires_at=1,sha256=hashlib.sha256(other.read_bytes()).hexdigest())
            (data/'fact-copy-retention.json').write_text(json.dumps(dict(entries=[row])))
            self.assertEqual(purge(data,2)['removed'],0)
            self.assertTrue(other.exists())
