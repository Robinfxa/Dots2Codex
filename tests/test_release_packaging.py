"""Offline release portability and contract regressions; no model or network use."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from portable import Deployment, init, protocol
import handoff
import probe


class ReleasePackaging(unittest.TestCase):
    def copy_checkout(self, parent, name='checkout'):
        target = Path(parent) / name
        shutil.copytree(ROOT, target, ignore=shutil.ignore_patterns(
            'runtime', 'runtimes', 'router-sessions', 'router-join-state', 'router-joins', 'runs', 'evidence', 'audit', '__pycache__', '*.pyc',
            '.git', '.venv', '.venv-router', 'venv', '.codex', 'release', 'dist', 'outbox', 'acks',
            'observations', 'control', '.env', '.env.*', '*.log', '*.jsonl',
            '*.sqlite*', '*.db', 'auth.json'))
        return target

    def run_python(self, checkout, *arguments):
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        env.pop('ROUTING_PACKAGE', None)
        return subprocess.run([sys.executable, '-B', *arguments], cwd=checkout,
                              env=env, text=True, capture_output=True, check=True)

    def test_clean_checkout_freeze_and_verify(self):
        with tempfile.TemporaryDirectory() as temp:
            checkout = self.copy_checkout(temp)
            self.assertFalse((checkout / 'evidence').exists())
            self.run_python(checkout, 'freeze_routing.py')
            self.assertTrue((checkout / 'evidence/routing-source-freeze.json').is_file())
            for excluded in ('.venv', '.venv-router', 'venv', 'runtime', 'runtimes', 'router-sessions', 'router-join-state', 'router-joins', 'runs', 'evidence',
                             '.git', '.codex', 'release', 'dist'):
                (checkout / excluded).mkdir(exist_ok=True)
                (checkout / excluded / 'excluded.py').write_text('# private fixture\n')
            result = self.run_python(checkout, 'freeze_routing.py')
            manifest = json.loads((checkout / 'evidence/routing-source-freeze.json').read_text())
            self.assertFalse(any(name.endswith('excluded.py') for name in manifest['files']))
            self.assertEqual(json.loads(result.stdout)['aggregate_sha256'], manifest['aggregate_sha256'])
            for name, digest in manifest['files'].items():
                self.assertEqual(hashlib.sha256((checkout / name).read_bytes()).hexdigest(), digest)
            self.run_python(checkout, '-c', 'from routing_desktop import verify_freeze; verify_freeze()')

    def test_checkout_path_is_shell_quoted(self):
        with tempfile.TemporaryDirectory() as temp:
            checkout = self.copy_checkout(temp, 'checkout with spaces;literal')
            result = self.run_python(checkout, '-c',
                                     'import repo_review; print(repo_review.command("tree"))')
            self.assertEqual(shlex.split(result.stdout.strip()),
                             ['python3', '-I', '-B', str(checkout / 'repo_fetch.py'), 'tree'])
            # Invalid helper arguments terminate locally without issuing any fetch.
            self.run_python(checkout, '-c',
                            'import repo_review; assert repo_review.parse_command(dict(cmd=repo_review.command("tree"), **repo_review.FIXED_ARGS)) == {"action":"tree"}')

    def test_generated_schemas_are_reproducible(self):
        with tempfile.TemporaryDirectory() as temp:
            checkout = self.copy_checkout(temp)
            self.run_python(checkout, 'generate_schemas.py')
            for path in sorted((ROOT / 'schemas').glob('*.json')):
                self.assertEqual(path.read_bytes(), (checkout / 'schemas' / path.name).read_bytes(), path.name)
            self.assertEqual((ROOT / 'ROLE_CONTRACT.json').read_bytes(),
                             (checkout / 'ROLE_CONTRACT.json').read_bytes())

    def test_scope_specific_schema_request_budgets(self):
        from jsonschema import Draft202012Validator
        schema = json.loads((ROOT / 'schemas/deployment.schema.json').read_text())
        validator = Draft202012Validator(schema)
        with tempfile.TemporaryDirectory() as temp:
            for scope, limit in [('text_only', 3), ('tool_probe', 3), ('repo_review', 16)]:
                manifest = init(Path(temp) / scope, 'owner', scope=scope, max_requests=limit)
                self.assertTrue(validator.is_valid(manifest), scope)
                invalid = copy.deepcopy(manifest)
                invalid['policy']['max_requests'] = limit + 1
                self.assertFalse(validator.is_valid(invalid), scope)

    def test_repo_review_handoff_matches_scope_and_schema(self):
        from jsonschema import Draft202012Validator
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'deployment'
            init(path, 'owner', scope='repo_review', max_requests=16)
            deployment = Deployment(path)
            probe.observe(deployment, 'desktop'); probe.offer(deployment)
            probe.observe(deployment, 'broker'); probe.answer(deployment); probe.verify(deployment)
            assignment = deployment.assign('owner', 'broker', 'worker', native_attested=True)
            assignment_path = path / 'evidence/broker.json'
            protocol.atomic_json(assignment_path, assignment)
            packet = handoff.packet(deployment, 'owner', assignment_path, ROOT)
            self.assertEqual(packet['instructions'], 'REPO_REVIEW_RUNBOOK.md')
            schema = json.loads((ROOT / 'schemas/handoff.schema.json').read_text())
            self.assertTrue(Draft202012Validator(schema).is_valid(packet))


if __name__ == '__main__':
    unittest.main()
