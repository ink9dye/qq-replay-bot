import shutil
import subprocess
import unittest
from pathlib import Path

class InputCandidatesTests(unittest.TestCase):
    def test_candidate_interactions(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node.js is required for UI interaction unit tests')
        result = subprocess.run([node, '--test', str(Path(__file__).with_suffix('.js'))], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
