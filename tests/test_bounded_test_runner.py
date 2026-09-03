import io
import sys
import unittest
from unittest.mock import patch

from scripts.run_bounded_tests import (
    DEFAULT_MEMORY_LIMIT_BYTES,
    MEMORY_LIMIT_ENV,
    apply_memory_limit,
    main,
    memory_limit_bytes,
)


class BoundedTestRunnerTests(unittest.TestCase):
    def test_default_limit_is_four_gib(self):
        self.assertEqual(DEFAULT_MEMORY_LIMIT_BYTES, 4 * 1024**3)
        self.assertEqual(memory_limit_bytes({}), DEFAULT_MEMORY_LIMIT_BYTES)

    def test_environment_overrides_limit_in_bytes(self):
        self.assertEqual(
            memory_limit_bytes({MEMORY_LIMIT_ENV: "2147483648"}), 2 * 1024**3
        )

    def test_limit_must_be_a_positive_byte_count(self):
        for value in ("0", "-1", "4GiB"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "positive byte count"),
            ):
                memory_limit_bytes({MEMORY_LIMIT_ENV: value})

    def test_posix_limit_is_inherited_as_soft_and_hard_cap(self):
        import resource

        with (
            patch("scripts.run_bounded_tests.os.name", "posix"),
            patch(
                "resource.getrlimit",
                return_value=(resource.RLIM_INFINITY, resource.RLIM_INFINITY),
            ),
            patch("resource.setrlimit") as setrlimit,
        ):
            self.assertTrue(apply_memory_limit(2 * 1024**3))

        setrlimit.assert_called_once_with(
            resource.RLIMIT_AS,
            (2 * 1024**3, 2 * 1024**3),
        )

    def test_non_posix_platform_warns_and_runs_unbounded(self):
        stderr = io.StringIO()
        with (
            patch("scripts.run_bounded_tests.os.name", "nt"),
            patch("sys.stderr", stderr),
        ):
            self.assertFalse(apply_memory_limit(DEFAULT_MEMORY_LIMIT_BYTES))
        self.assertIn("tests are unbounded", stderr.getvalue())

    def test_main_execs_unittest_with_verbatim_arguments(self):
        with (
            patch("scripts.run_bounded_tests.memory_limit_bytes", return_value=1024),
            patch("scripts.run_bounded_tests.apply_memory_limit", return_value=True),
            patch("scripts.run_bounded_tests.os.execv") as execv,
        ):
            main(["tests.test_example", "-v"])

        execv.assert_called_once_with(
            sys.executable,
            [sys.executable, "-m", "unittest", "tests.test_example", "-v"],
        )

    def test_main_defaults_to_test_discovery(self):
        with (
            patch("scripts.run_bounded_tests.memory_limit_bytes", return_value=1024),
            patch("scripts.run_bounded_tests.apply_memory_limit", return_value=True),
            patch("scripts.run_bounded_tests.os.execv") as execv,
        ):
            main([])

        execv.assert_called_once_with(
            sys.executable,
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        )


if __name__ == "__main__":
    unittest.main()
