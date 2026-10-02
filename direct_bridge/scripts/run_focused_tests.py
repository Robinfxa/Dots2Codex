#!/usr/bin/env python3
"""Run every bundled test_*.py module once, using synthetic fixtures only."""
from pathlib import Path
import importlib.util
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
sys.path.insert(0, str(ROOT))


def main():
    paths = sorted(p for p in ROOT.rglob('test_*.py')
                   if '__pycache__' not in p.parts)
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for index, path in enumerate(paths):
        name = 'bridge_bundle_test_' + str(index)
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        tests = loader.loadTestsFromModule(module)
        print(f"{path.relative_to(ROOT).as_posix()}: {tests.countTestCases()} tests", flush=True)
        suite.addTests(tests)
    print(f"Inventory: {len(paths)} modules, {suite.countTestCases()} tests", flush=True)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
