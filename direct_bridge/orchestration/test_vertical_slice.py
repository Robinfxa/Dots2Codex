import unittest
from orchestration.vertical_slice import run_vertical_slice

class VerticalSliceTests(unittest.TestCase):
    def test_three_callbacks_over_actual_mcp_stdio_and_http(self):
        result = run_vertical_slice()
        self.assertEqual(result['successive_callbacks'], 3)
        self.assertEqual(result['context_kinds'], ['full', 'delta', 'delta', 'delta'])
        self.assertFalse(result['native_platform_verified'])
        self.assertFalse(result['actual_mac_tools_executed'])

if __name__ == '__main__':
    unittest.main()
