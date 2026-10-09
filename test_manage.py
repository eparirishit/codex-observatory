import os
import subprocess
import unittest
from unittest.mock import patch

import manage


class ManagementTests(unittest.TestCase):
    @patch('manage.shutil.which', return_value='/usr/local/bin/docker')
    @patch('manage.subprocess.run')
    def test_start_detached_and_retrieve_link(self, run, which):
        with patch('sys.argv', ['manage.py', 'up']):
            self.assertEqual(manage.main(), 0)
        self.assertEqual(run.call_args_list[0].args[0], ['docker', 'compose', 'up', '-d', '--build', '--wait'])
        self.assertEqual(run.call_args_list[1].args[0], ['docker', 'compose', 'exec', '-T', 'observatory', 'cat', '/tmp/observatory-link'])
        self.assertEqual(run.call_args_list[0].kwargs['env']['LOCAL_UID'], str(os.getuid()))
        self.assertTrue(run.call_args_list[0].kwargs['check'])

    @patch('manage.shutil.which', return_value='/usr/local/bin/docker')
    @patch('manage.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'docker'))
    def test_failure_does_not_report_success(self, run, which):
        with patch('sys.argv', ['manage.py', 'up']):
            self.assertEqual(manage.main(), 1)
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
