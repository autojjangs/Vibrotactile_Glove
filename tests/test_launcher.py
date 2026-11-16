import contextlib
import io
import unittest
from unittest.mock import patch

from vibro import launcher
from vibro.__main__ import parser as experiment_parser


class LauncherTest(unittest.TestCase):
    def resolve(self, argv, answers=()):
        with patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
            values = launcher.resolve_arguments(launcher.parser().parse_args(argv))
        return experiment_parser().parse_args(values), values

    def test_explicit_actual_trials_preserve_route_condition_and_ids(self):
        for route in ("1", "2"):
            for short, full in (("b", "baseline"), ("p", "proposed")):
                with self.subTest(route=route, condition=short):
                    args, values = self.resolve([route, short, "P019", "02"])
                    self.assertEqual(args.route, launcher.ROOT / "routes" / f"route_{route}.json")
                    self.assertEqual((args.condition, args.participant, args.trial), (full, "P019", "02"))
                    self.assertNotIn("--allow-demo", values)
                    self.assertFalse(args.autostart)

    def test_menu_retries_invalid_choices_and_identifiers(self):
        args, _ = self.resolve([], ["x", "2b", "이름", "P02", ""])
        self.assertEqual(args.route.name, "route_2.json")
        self.assertEqual((args.condition, args.participant, args.trial), ("baseline", "P02", "01"))

    def test_all_four_assignments_match_existing_route_condition_commands(self):
        cases = [("1b", "1", "b"), ("1p", "1", "p"), ("2b", "2", "b"), ("2p", "2", "p")]
        for code, route, short in cases:
            with self.subTest(code=code):
                expected, _ = self.resolve([route, short, "P03", "02"])
                for argv, answers in (([code], ["P03", "02"]), ([], [code, "P03", "02"])):
                    selected, _ = self.resolve(argv, answers)
                    self.assertEqual(vars(selected), vars(expected))
                validated, _ = self.resolve([code, "--validate"])
                self.assertTrue(validated.validate)
                self.assertEqual(validated.route, expected.route)

    def test_assignment_cannot_be_overridden_by_another_condition(self):
        for argv in (["1b", "p"], ["2p", "b"]):
            with self.subTest(argv=argv), self.assertRaisesRegex(ValueError, "방식이 포함"):
                self.resolve(argv)

    def test_legacy_route_only_command_still_asks_for_condition(self):
        args, _ = self.resolve(["2"], ["wrong", "p", "P02", "03"])
        self.assertEqual(args.route.name, "route_2.json")
        self.assertEqual((args.condition, args.participant, args.trial), ("proposed", "P02", "03"))

    def test_practice_needs_no_prompts_and_can_choose_manual(self):
        for argv, condition in ((["test"], "proposed"), (["test", "b"], "baseline")):
            args, _ = self.resolve(argv)
            self.assertEqual(args.route.name, "haptic_practice_40s.json")
            self.assertEqual((args.condition, args.participant, args.trial), (condition, "TEST", "PRACTICE"))
            self.assertTrue(args.allow_demo)
            self.assertFalse(args.autostart)

    def test_explicit_practice_ids_and_flags_are_preserved(self):
        args, _ = self.resolve(["test", "baseline", "TEST02", "03", "--simulate", "--verbose"])
        self.assertEqual((args.participant, args.trial), ("TEST02", "03"))
        self.assertTrue(args.simulate)
        self.assertTrue(args.verbose)

    def test_scan_and_validation_do_not_prompt_for_trial(self):
        args, _ = self.resolve(["--scan"])
        self.assertTrue(args.scan)
        args, _ = self.resolve(["2", "--validate"])
        self.assertTrue(args.validate)
        self.assertEqual(args.route.name, "route_2.json")
        args, _ = self.resolve(["--validate"], ["1"])
        self.assertEqual(args.route.name, "route_1.json")

    def test_only_non_root_linux_hardware_uses_sudo_and_venv_python(self):
        with patch.object(launcher.sys, "platform", "linux"), \
             patch.object(launcher.sys, "executable", "/path with spaces/.venv/bin/python"), \
             patch.object(launcher.os, "geteuid", return_value=1000, create=True):
            args, values = self.resolve(["1", "p", "P01", "01"])
            self.assertEqual(launcher.execution_command(values)[:5],
                             ["sudo", "--preserve-env=" + ",".join(launcher.DESKTOP_ENV),
                              "/path with spaces/.venv/bin/python", "-m", "vibro"])
            for flag in ("--scan", "--validate", "--simulate"):
                self.assertEqual(launcher.execution_command([flag])[0],
                                 "/path with spaces/.venv/bin/python")
            with patch.object(launcher.os, "geteuid", return_value=0):
                self.assertNotIn("sudo", launcher.execution_command(values))
        with patch.object(launcher.sys, "platform", "win32"):
            self.assertNotIn("sudo", launcher.execution_command(values))

    def test_cancel_before_launch_does_not_start_hardware(self):
        for error in (EOFError, KeyboardInterrupt):
            with self.subTest(error=error), patch("builtins.input", side_effect=error), \
                 patch.object(launcher.os, "execvp") as execute, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launcher.main([]), 130)
                execute.assert_not_called()

    def test_invalid_explicit_input_and_conflicting_modes_never_launch(self):
        for argv in (["3"], ["1", "wrong"], ["1", "b", "../P01"],
                     ["1", "b", "P01", "bad trial"], ["--scan", "--simulate"], ["1", "--scan"]):
            with self.subTest(argv=argv), patch.object(launcher.os, "execvp") as execute, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                launcher.main(argv)
            self.assertEqual(raised.exception.code, 2)
            execute.assert_not_called()

    def test_exec_uses_project_directory_and_original_runtime(self):
        with patch.object(launcher.os, "chdir") as change_dir, \
             patch.object(launcher.os, "execvp") as execute, \
             patch.object(launcher.sys, "platform", "win32"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(["1", "b", "P01", "01", "--simulate"]), 0)
        change_dir.assert_called_once_with(launcher.ROOT)
        executable, command = execute.call_args.args
        self.assertEqual(executable, launcher.sys.executable)
        self.assertEqual(command[:3], [launcher.sys.executable, "-m", "vibro"])
        parsed = experiment_parser().parse_args(command[3:])
        self.assertTrue(parsed.simulate)
        self.assertFalse(parsed.autostart)
        self.assertEqual((parsed.participant, parsed.trial), ("P01", "01"))

    def test_wrapper_keeps_linux_line_endings(self):
        wrapper = (launcher.ROOT / "run").read_bytes()
        self.assertNotIn(b"\r", wrapper)
        self.assertIn(b'/run text eol=lf', (launcher.ROOT / ".gitattributes").read_bytes())


if __name__ == "__main__":
    unittest.main()
