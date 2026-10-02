"""Run the synthetic production-pipe fixture and print measured local evidence."""
import json
from pathlib import Path
import statistics
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from test_global_mcp import GlobalMCPTests


def main():
    test = GlobalMCPTests('test_production_multi_route_tool_callback_restart_final_turn')
    result = unittest.TestResult()
    test.run(result)
    if not result.wasSuccessful():
        print(json.dumps({'status':'failed','errors':len(result.errors),'failures':len(result.failures)}))
        return 1
    evidence = test.evidence
    times = [x['seconds'] for x in evidence.pop('mcp_calls')]
    evidence.update(status='passed', mcp_tool_calls=len(times),
                    measured_mcp_median_ms=statistics.median(times)*1000,
                    measured_mcp_max_ms=max(times)*1000,
                    timing_scope='Local synthetic fixture overhead, not Mac, tunnel, native inference or production latency')
    print(json.dumps(evidence, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
