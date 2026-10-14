"""The live-state path guard (betangel_research.betfair.backtest.path_guard) in temporary directories.

These show that a path naming either denied tree is refused whatever its case, whether named directly, reached through
a symlink (a root's or a child's) or as a prospective output under a symlinked parent; that ordinary paths, and paths
that hold the names but not as the denied consecutive sequences, pass; that an unresolvable path is refused; and that
the guard opens, lists and creates nothing. They do not show anything about a custom certificate location, which the
guard cannot know (module docstring).
"""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from betangel_research.betfair.backtest import path_guard
from betangel_research.betfair.backtest.path_guard import (DENIED_COMPONENT_SEQUENCES, PathDenied, denied_sequence,
                                                         guard_path)

STATE = ("Library", "Application Support", "betangel-research", "betfair")
CERTIFICATE = (".config", "betangel-research", "betfair")


class GuardCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

    def tree(self, parts):
        place = self.tmp.joinpath(*parts)
        place.mkdir(parents=True)
        return place

    def assertDenied(self, path, reason="denied_live_state_tree", **kwargs):
        with self.assertRaises(PathDenied) as caught:
            guard_path(path, "input", **kwargs)
        self.assertEqual(str(caught.exception), f"input:{reason}")


class SequenceTests(GuardCase):
    def test_the_deny_list_is_the_two_client_trees(self):
        self.assertEqual(DENIED_COMPONENT_SEQUENCES, (STATE, CERTIFICATE))

    def test_named_trees_are_denied_in_any_case_and_below(self):
        for parts in (STATE, CERTIFICATE, STATE + ("journal",), CERTIFICATE + ("client.crt",),
                      ("library", "application support", "BETANGEL-RESEARCH", "BetFair"),
                      (".CONFIG", "Betangel-Research", "BETFAIR", "x")):
            with self.subTest(parts=parts):
                self.assertTrue(denied_sequence(self.tmp.joinpath(*parts)))
                self.assertDenied(self.tmp.joinpath(*parts))

    def test_the_names_without_the_consecutive_sequence_pass(self):
        for parts in (("Library", "Application Support", "betangel-research"), ("Library", "betangel-research",
                                                                                 "betfair"),
                      (".config", "betangel-research"), (".config", "x", "betangel-research", "betfair"),
                      ("betangel-research", "betfair"), ("Application Support", "betangel-research", "betfair")):
            with self.subTest(parts=parts):
                self.assertFalse(denied_sequence(self.tmp.joinpath(*parts)))
                place = self.tree(parts)
                self.assertEqual(guard_path(place, "input"), place)

    def test_a_relative_path_is_judged_absolute(self):
        self.assertTrue(denied_sequence(Path(*CERTIFICATE)))


class ResolutionTests(GuardCase):
    def test_a_symlinked_root_or_child_into_a_denied_tree_is_denied(self):
        target = self.tree(STATE + ("journal",))
        (target / "live.jsonl").write_bytes(b"")
        root = self.tree(("data",))
        (self.tmp / "link").symlink_to(target)
        (root / "child").symlink_to(target / "live.jsonl")
        self.assertDenied(self.tmp / "link")
        self.assertDenied(root / "child")
        self.assertEqual(guard_path(root, "input"), root)

    def test_a_prospective_output_under_a_symlinked_parent_or_as_a_symlink_is_denied(self):
        target = self.tree(CERTIFICATE)
        (self.tmp / "out").symlink_to(target)
        self.assertDenied(self.tmp / "out" / "report.json", prospective=True)
        runs = self.tree(("runs",))
        self.assertEqual(guard_path(runs / "new.json", "input", prospective=True), runs / "new.json")

    def test_a_prospective_output_that_already_exists_is_refused(self):
        runs = self.tree(("runs",))
        data = self.tree(("data",))
        (runs / "redirect.json").symlink_to(data / "planted.json")      # dangling: a write would land in data
        (runs / "denied.json").symlink_to(self.tree(CERTIFICATE) / "x")
        (runs / "plain.json").write_bytes(b"")
        for name in ("redirect.json", "denied.json", "plain.json"):
            with self.subTest(name=name):
                self.assertDenied(runs / name, "already_exists", prospective=True)
        self.assertFalse((data / "planted.json").exists())

    def test_unresolvable_or_malformed_paths_are_refused(self):
        self.assertDenied(self.tmp / "nothing", "missing_path")
        self.assertDenied(self.tmp / "nothing" / "out.json", "unresolvable", prospective=True)
        (self.tmp / "file").write_bytes(b"")
        self.assertDenied(self.tmp / "file" / "out.json", "parent_not_a_directory", prospective=True)
        self.assertDenied(self.tmp / "..", "not_one_name", prospective=True)
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertDenied(value, "missing_path")
        (self.tmp / "loop").symlink_to(self.tmp / "loop")
        self.assertDenied(self.tmp / "loop", "unresolvable")
        self.assertDenied(str(self.tmp / "nul\0name"), "unresolvable")
        self.assertDenied(str(self.tmp / "nul\0name"), "unresolvable", prospective=True)

    def test_an_inspection_error_is_refused(self):
        place = self.tree(("data",))
        with mock.patch.object(type(place), "resolve", side_effect=PermissionError(13, "synthetic")):
            self.assertDenied(place, "unresolvable")
            self.assertDenied(place / "out.json", "unresolvable", prospective=True)

    def test_the_guard_opens_lists_and_creates_nothing(self):
        place = self.tree(("data",))
        kind = type(place)
        forbidden = {name: mock.patch.object(kind, name, side_effect=AssertionError(name))
                     for name in ("open", "iterdir", "mkdir", "touch", "write_bytes", "read_bytes", "unlink")}
        for patch in forbidden.values():
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(guard_path(place, "input"), place)
        self.assertEqual(guard_path(place / "new.json", "output", prospective=True), place / "new.json")
        self.assertDenied(self.tmp.joinpath(*STATE))

    def test_the_module_names_the_trees_only_in_its_deny_literal(self):
        source = Path(path_guard.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count('"Application Support"'), 1)
        self.assertEqual(source.count('".config"'), 1)


if __name__ == "__main__":
    unittest.main()
