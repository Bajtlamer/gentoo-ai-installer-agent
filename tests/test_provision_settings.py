import unittest

from gentoo_agent.core import provision
from gentoo_agent.core.errors import CONFIG_INVALID, AgentError

GIB = 1024 * 1024


class ValidatedProvisionTests(unittest.TestCase):
    def test_a_valid_setting_is_returned_without_extra_keys(self):
        settings = {"profile": "nvidia-driver", "accept_licenses": ["NVIDIA-r2", "Apache-2.0"], "extra": 1}
        self.assertEqual(provision.validated_provision(settings),
                         {"profile": "nvidia-driver", "accept_licenses": ["NVIDIA-r2", "Apache-2.0"], "package_use": []})

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
        for name, licenses in {"glob": "*/*", "negation of all": "-*", "newline at the end": "X\n",
                               "leading dot": ".x", "leading dash": "-x", "empty": "", "65 characters": "A" * 65}.items():
            cases[name] = {"profile": "nvidia-driver", "accept_licenses": [licenses]}
        cases["tuple instead of list"] = {"profile": "nvidia-driver", "accept_licenses": ("X",)}
        for name, settings in cases.items():
            with self.subTest(name), self.assertRaises(AgentError) as caught:
                provision.validated_provision(settings)
            self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_all_is_accepted_as_an_ordinary_name(self):
        settings = {"profile": "nvidia-driver", "accept_licenses": ["ALL"]}
        self.assertEqual(provision.validated_provision(settings)["accept_licenses"], ["ALL"])

    def test_a_64_character_name_is_accepted(self):
        settings = {"profile": "nvidia-driver", "accept_licenses": ["A" * 64]}
        self.assertEqual(provision.validated_provision(settings)["accept_licenses"], ["A" * 64])


LIBGLVND = ">=media-libs/libglvnd-1.7.0 X"


def with_use(lines):
    return {"profile": "nvidia-driver", "accept_licenses": ["X"], "package_use": lines}


class PackageUseTests(unittest.TestCase):
    def test_a_real_line_is_accepted(self):
        self.assertEqual(provision.validated_provision(with_use([LIBGLVND]))["package_use"], [LIBGLVND])

    def test_the_default_is_empty(self):
        settings = {"profile": "nvidia-driver", "accept_licenses": ["X"]}
        self.assertEqual(provision.validated_provision(settings)["package_use"], [])

    def test_more_forms_are_accepted(self):
        lines = ["media-libs/freetype harfbuzz", ">=x11-libs/cairo-1.18.4-r1 X -gtk_doc", "dev-libs/c_d.e+ X"]
        self.assertEqual(provision.validated_provision(with_use(lines))["package_use"], lines)

    def test_refused(self):
        cases = {
            "not a list": "media-libs/a X",
            "tuple": ("media-libs/a X",),
            "non-text item": [5],
            "duplicates": [LIBGLVND, LIBGLVND],
            "more than 8": [f"media-libs/a{i} X" for i in range(9)],
            "wildcard package": ["media-libs/* X"],
            "wildcard flag": ["media-libs/a *"],
            "newline": ["media-libs/a X\nmedia-libs/b Y"],
            "trailing newline": ["media-libs/a X\n"],
            "shell text": ["media-libs/a X; reboot"],
            "command substitution": ["media-libs/a $(reboot)"],
            "backticks": ["media-libs/a `reboot`"],
            "no flag": ["media-libs/a"],
            "no category": ["a X"],
            "doubled space": ["media-libs/a  X"],
            "leading space": [" media-libs/a X"],
            "trailing space": ["media-libs/a X "],
            "9 flags": ["media-libs/a " + " ".join("f%d" % i for i in range(9))],
            "over 200 characters": ["media-libs/a " + "X" * 200],
            "empty": [""],
            "comment": ["# media-libs/a X"],
            "nvidia plain": ["x11-drivers/nvidia-drivers kernel"],
            "nvidia with operator and version": [">=x11-drivers/nvidia-drivers-595 tools"],
            "nvidia exact version": ["=x11-drivers/nvidia-drivers-595.104.02 -tools"],
            "nvidia tilde": ["~x11-drivers/nvidia-drivers-595 X"],
        }
        for name, lines in cases.items():
            with self.subTest(name), self.assertRaises(AgentError) as caught:
                provision.validated_provision(with_use(lines))
            self.assertEqual(caught.exception.code, CONFIG_INVALID)
            self.assertEqual(caught.exception.details["field"], "package_use")

    def test_eight_lines_are_accepted(self):
        lines = [f"media-libs/a{i} X" for i in range(8)]
        self.assertEqual(provision.validated_provision(with_use(lines))["package_use"], lines)

    def test_a_similarly_named_package_is_not_the_driver(self):
        lines = ["x11-drivers/nvidia-drivers-extra X", "x11-drivers/nvidia-settings X"]
        self.assertEqual(provision.validated_provision(with_use(lines))["package_use"], lines)


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

    def test_validated_tuning_edge_cases(self):
        def tuning(**changes):
            return {"jobs": 1, "emerge_jobs": 1, "load_average": 1, **changes}
        self.assertEqual(provision.validated_tuning(tuning(jobs=4096))["jobs"], 4096)
        bad_values = {"missing key": {"jobs": 1, "emerge_jobs": 1}, "float": tuning(jobs=15.0),
                      "negative": tuning(jobs=-1), "4097": tuning(load_average=4097)}
        for name, bad in bad_values.items():
            with self.subTest(name), self.assertRaises(AgentError) as caught:
                provision.validated_tuning(bad)
            self.assertEqual(caught.exception.code, CONFIG_INVALID)

    def test_the_error_names_the_failing_key(self):
        with self.assertRaises(AgentError) as caught:
            provision.validated_tuning({"jobs": 1, "emerge_jobs": 0, "load_average": 1})
        self.assertIn("'emerge_jobs'", caught.exception.message)
        self.assertEqual(caught.exception.details["field"], "tuning")

    def test_a_huge_thread_count_is_refused_after_derivation(self):
        with self.assertRaises(AgentError) as caught:
            provision.validated_tuning(provision.derive_tuning(5000, 64 * GIB))
        self.assertEqual(caught.exception.code, CONFIG_INVALID)
