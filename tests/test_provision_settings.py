import unittest

from gentoo_agent.core import provision
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError

GIB = 1024 * 1024


class ValidatedProvisionTests(unittest.TestCase):
    def test_a_valid_setting_is_returned_without_extra_keys(self):
        settings = {"profile": "nvidia-driver", "accept_licenses": ["NVIDIA-r2", "Apache-2.0"], "extra": 1}
        self.assertEqual(provision.validated_provision(settings),
                         {"profile": "nvidia-driver", "accept_licenses": ["NVIDIA-r2", "Apache-2.0"]})

    def test_invalid_settings_are_refused(self):
        cases = {
            "not a dict": None,
            "unknown profile": {"profile": "cuda", "accept_licenses": ["X"]},
            "no licenses": {"profile": "nvidia-driver", "accept_licenses": []},
            "licenses not a list": {"profile": "nvidia-driver", "accept_licenses": "NVIDIA-r2"},
            "a group instead of a name": {"profile": "nvidia-driver", "accept_licenses": ["@EULA"]},
            "shell text": {"profile": "nvidia-driver", "accept_licenses": ["X; reboot"]},
            "duplicate": {"profile": "nvidia-driver", "accept_licenses": ["X", "X"]},
            "too many": {"profile": "nvidia-driver", "accept_licenses": [f"L{i}" for i in range(9)]},
            "non-text item": {"profile": "nvidia-driver", "accept_licenses": [5]},
        }
        for name, settings in cases.items():
            with self.subTest(name), self.assertRaises(AgentError) as caught:
                provision.validated_provision(settings)
            self.assertEqual(caught.exception.code, CONFIG_INVALID)


class TuningTests(unittest.TestCase):
    def test_the_smaller_of_threads_and_half_the_memory_in_gib(self):
        self.assertEqual(provision.derive_tuning(24, 32768000),
                         {"jobs": 15, "emerge_jobs": 3, "load_average": 24})
        self.assertEqual(provision.derive_tuning(8, 64 * GIB), {"jobs": 8, "emerge_jobs": 1, "load_average": 8})
        self.assertEqual(provision.derive_tuning(32, 128 * GIB), {"jobs": 32, "emerge_jobs": 4, "load_average": 32})

    def test_a_small_machine_still_gets_one_job(self):
        self.assertEqual(provision.derive_tuning(4, 1 * GIB), {"jobs": 1, "emerge_jobs": 1, "load_average": 4})

    def test_validated_tuning(self):
        good = {"jobs": 15, "emerge_jobs": 3, "load_average": 24, "extra": 1}
        self.assertEqual(provision.validated_tuning(good), {"jobs": 15, "emerge_jobs": 3, "load_average": 24})
        for bad in (None, {}, {"jobs": 0, "emerge_jobs": 1, "load_average": 1},
                    {"jobs": True, "emerge_jobs": 1, "load_average": 1},
                    {"jobs": "4", "emerge_jobs": 1, "load_average": 1},
                    {"jobs": 5000, "emerge_jobs": 1, "load_average": 1}):
            with self.subTest(bad=bad), self.assertRaises(AgentError) as caught:
                provision.validated_tuning(bad)
            self.assertEqual(caught.exception.code, CONFIG_INVALID)
