"""Resource accounting must separate browser children and swap storage."""
import unittest
from unittest.mock import patch
from pathlib import Path

from measure_resources import firefox_count, measure, system_memory


class ResourceTests(unittest.TestCase):
    def test_firefox_descendants_exclude_controller(self):
        processes = {
            1: (0, 0, 0, 1, 'python'),
            2: (1, 0, 0, 1, 'geckodriver'),
            3: (2, 0, 0, 1, 'firefox'),
            4: (3, 0, 0, 1, 'forkserver'),
            5: (4, 0, 0, 1, 'Web Content'),
            6: (1, 0, 0, 1, 'pw-record'),
        }
        self.assertEqual(firefox_count(processes), 3)

    def test_zram_logical_and_physical_memory_are_distinct(self):
        def read(path):
            if str(path) == '/proc/swaps':
                return ('Filename Type Size Used Priority\n'
                        '/swapfile file 524284 1024 -1\n'
                        '/dev/zram0 partition 1922816 2048 100\n')
            return '2097152 262144 524288 0 0 0 0 0 0'
        with patch.object(Path, 'read_text', read), patch.object(
                Path, 'glob', return_value=[Path('/sys/block/zram0/mm_stat')]):
            self.assertEqual(system_memory(), {
                'system_swap_used_mib': 1,
                'system_zram_used_mib': 2,
                'system_zram_ram_mib': .5})

    def test_exit_invalidates_sample(self):
        with patch('measure_resources.snapshot', side_effect=[
                {7: (0, 0, 100, 1, 'python')}, {}]), patch(
                'measure_resources.system_memory', return_value={}), patch(
                'measure_resources.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                measure(7, 1)


if __name__ == '__main__':
    unittest.main()
