"""Whisper uses every core by default; threads and the core list are settings."""
import unittest
from unittest import mock

from verifyarr import procprio, settings


class CpuList(unittest.TestCase):
    def test_parse(self):
        p = procprio.parse_cpu_list
        self.assertEqual(p(""), [])
        self.assertEqual(p(None), [])
        self.assertEqual(p("0-3,6"), [0, 1, 2, 3, 6])
        self.assertEqual(p(" 2 , 4-5 "), [2, 4, 5])
        for bad in ("a", "3-1", "1-", "-2", "1,,x"):
            self.assertIsNone(p(bad), bad)

    def test_threads_default_is_every_usable_core(self):
        with mock.patch("os.sched_getaffinity", return_value=set(range(12))):
            self.assertEqual(procprio.whisper_threads(0, ""), 12)
            self.assertEqual(procprio.whisper_threads(0, "0-3,6"), 5)
            self.assertEqual(procprio.whisper_threads(3, "0-3,6"), 3)

    def test_pin(self):
        with mock.patch.object(procprio, "_TASKSET_BIN", "/usr/bin/taskset"):
            self.assertEqual(procprio.pin_to_cpus(["w"], "1-2"), ["/usr/bin/taskset", "-c", "1,2", "w"])
            self.assertEqual(procprio.pin_to_cpus(["w"], ""), ["w"])
        with mock.patch.object(procprio, "_TASKSET_BIN", None):
            self.assertEqual(procprio.pin_to_cpus(["w"], "1-2"), ["w"])

    def test_pin_ignores_cores_the_process_may_not_use(self):
        with mock.patch.object(procprio, "_TASKSET_BIN", "/usr/bin/taskset"), \
                mock.patch("os.sched_getaffinity", return_value={1, 2, 3}):
            self.assertEqual(procprio.pin_to_cpus(["w"], "0-2"), ["/usr/bin/taskset", "-c", "1,2", "w"])
            self.assertEqual(procprio.pin_to_cpus(["w"], "0,9"), ["w"])
            self.assertEqual(procprio.whisper_threads(0, "0,9"), 3)

    def test_ionice_left_out_when_it_cannot_run(self):
        procprio._ionice_works.cache_clear()
        with mock.patch.object(procprio, "_IONICE_BIN", "/usr/bin/ionice"), \
                mock.patch.object(procprio, "_NICE_BIN", "/usr/bin/nice"), \
                mock.patch("subprocess.run", return_value=mock.Mock(returncode=1)):
            self.assertEqual(procprio.wrap_low_priority(["w"]), ["/usr/bin/nice", "-n", "19", "w"])
        procprio._ionice_works.cache_clear()

    def test_defaults_and_validation(self):
        self.assertEqual(settings.SETTING_DEFS["correctness.local_whisper_threads"][2], 0)
        self.assertEqual(settings.SETTING_DEFS["correctness.local_whisper_cpus"][2], "")


if __name__ == "__main__":
    unittest.main()
