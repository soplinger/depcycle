"""Tests for depcycle. Run with: python3 -m pytest  or  python3 -m unittest discover -s tests"""
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import depcycle as dc  # noqa: E402

CLEAN = """\
@forbid ui -> db
@max-depth 4
app: ui api
ui: core
api: core db
db: core logger
core: logger
logger:
"""

BROKEN = """\
app: ui api
ui: core ui
api: core db
db: api
core: cache
cache core
"""


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    code = dc.main(argv, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


class ParseTests(unittest.TestCase):
    def test_parses_dependencies_and_definition_lines(self):
        m = dc.parse_manifest(CLEAN)
        self.assertEqual(m.deps["app"], ["ui", "api"])
        self.assertEqual(m.deps["logger"], [])
        self.assertEqual(m.lines["api"], 5)
        self.assertEqual(m.errors, [])

    def test_directives_are_read(self):
        m = dc.parse_manifest(CLEAN)
        self.assertEqual(m.forbids, [("ui", "db", 1)])
        self.assertEqual(m.max_depth, 4)

    def test_syntax_errors_are_line_numbered(self):
        m = dc.parse_manifest("good: a\nno colon here\nbad name!: x\ngood: b\nlone: a a\n@nope 1\n")
        found = dict(m.errors)
        self.assertIn(2, found)
        self.assertIn("missing ':'", found[2])
        self.assertIn("invalid package name", found[3])
        self.assertIn("duplicate package 'good'", found[4])
        self.assertIn("duplicate dependency 'a'", found[5])
        self.assertIn("unknown directive", found[6])

    def test_inline_comments_are_ignored(self):
        m = dc.parse_manifest("a: b  # note\nb:\n@forbid a -> b  # keep apart\n")
        self.assertEqual(m.deps["a"], ["b"])
        self.assertEqual(m.forbids, [("a", "b", 3)])
        self.assertEqual(m.errors, [])

    def test_self_dependency_is_an_error_but_kept_for_cycle_checks(self):
        m = dc.parse_manifest("ui: core ui\ncore:\n")
        self.assertIn((1, "package 'ui' depends on itself"), m.errors)
        self.assertIn("ui", m.deps["ui"])


class LintTests(unittest.TestCase):
    def test_clean_manifest_has_no_findings(self):
        self.assertEqual(dc.lint(dc.parse_manifest(CLEAN)), [])

    def test_unknown_package_and_bad_forbid_are_errors(self):
        m = dc.parse_manifest("app: ghost\n@forbid app -> nope\n")
        messages = [(line, sev, msg) for line, sev, msg in dc.lint(m)]
        self.assertIn((1, "error", "unknown package 'ghost' (needed by 'app')"), messages)
        self.assertIn((2, "error", "@forbid names unknown package 'nope'"), messages)

    def test_isolated_package_is_only_a_warning(self):
        findings = dc.lint(dc.parse_manifest("lonely:\n"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][1], "warning")


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.g = dc.full_graph(dc.parse_manifest(CLEAN).deps)

    def test_cycle_is_found_with_a_closed_path(self):
        g = dc.full_graph({"a": ["b"], "b": ["c"], "c": ["a"], "d": ["a"]})
        self.assertEqual(dc.find_cycles(g), [["a", "b", "c", "a"]])

    def test_self_loop_is_a_cycle(self):
        g = dc.full_graph({"x": ["x"]})
        self.assertEqual(dc.find_cycles(g), [["x", "x"]])

    def test_acyclic_graph_has_no_cycles(self):
        self.assertEqual(dc.find_cycles(self.g), [])

    def test_build_order_puts_dependencies_first_with_alphabetical_ties(self):
        order = dc.build_order(self.g)
        self.assertEqual(order, ["logger", "core", "db", "api", "ui", "app"])
        for name, targets in self.g.items():
            for target in targets:
                self.assertLess(order.index(target), order.index(name))

    def test_build_order_refuses_cycles(self):
        with self.assertRaises(dc.CycleError):
            dc.build_order(dc.full_graph({"a": ["b"], "b": ["a"]}))

    def test_layers_give_longest_chain_depth(self):
        level = dc.layers(self.g)
        self.assertEqual(level["logger"], 0)
        self.assertEqual(level["core"], 1)
        self.assertEqual(level["db"], 2)
        self.assertEqual(level["api"], 3)
        self.assertEqual(level["app"], 4)

    def test_needs_and_impact_use_hop_distance(self):
        needs = dc.closure(self.g, "app")
        self.assertEqual(needs, {"ui": 1, "api": 1, "core": 2, "db": 2, "logger": 3})
        impact = dc.closure(dc.reverse(self.g), "logger")
        self.assertEqual(impact, {"core": 1, "db": 1, "ui": 2, "api": 2, "app": 3})

    def test_shortest_path_and_unreachable(self):
        self.assertEqual(dc.shortest_path(self.g, "app", "logger"), ["app", "api", "core", "logger"])
        self.assertIsNone(dc.shortest_path(self.g, "logger", "app"))


class PolicyTests(unittest.TestCase):
    def test_clean_manifest_passes_policy(self):
        self.assertEqual(dc.policy_violations(dc.parse_manifest(CLEAN)), [])

    def test_transitive_forbid_is_reported_with_path(self):
        m = dc.parse_manifest("@forbid app -> logger\napp: ui\nui: logger\nlogger:\n")
        violations = dc.policy_violations(m)
        self.assertEqual(len(violations), 1)
        line, message = violations[0]
        self.assertEqual(line, 1)
        self.assertIn("app -> ui -> logger", message)

    def test_forbid_naming_an_unknown_package_is_skipped_not_crashed(self):
        self.assertEqual(dc.policy_violations(dc.parse_manifest("@forbid ghost -> core\ncore:\n")), [])

    def test_max_depth_violation_names_deepest_package(self):
        m = dc.parse_manifest("@max-depth 1\na: b\nb: c\nc:\n")
        violations = dc.policy_violations(m)
        self.assertEqual(violations, [(0, "longest chain is 2 hop(s), limit is 1 (deepest: 'a')")])

    def test_max_depth_is_skipped_with_a_note_when_cyclic(self):
        m = dc.parse_manifest("@max-depth 1\na: b\nb: a\n")
        self.assertEqual(dc.policy_violations(m), [(0, "@max-depth not checked because the graph has cycles")])


class OutputTests(unittest.TestCase):
    def test_diff_reports_added_and_removed_packages_and_edges(self):
        old = dc.parse_manifest("a: b\nb:\n")
        new = dc.parse_manifest("a: c\nb:\nc:\n")
        self.assertEqual(
            dc.diff_lines(old, new),
            ["+ package c", "+ edge a -> c", "- edge a -> b"],
        )

    def test_dot_marks_cycle_edges_red(self):
        dot = dc.to_dot(dc.parse_manifest("a: b\nb: a\nc: a\n"))
        self.assertIn('"a" -> "b" [color=red];', dot)
        self.assertIn('"c" -> "a";', dot)

    def test_stats_handles_empty_manifest(self):
        lines = dc.stats_lines(dc.parse_manifest("# nothing here\n"))
        self.assertIn("packages: 0 (0 defined, 0 undefined)", lines)
        self.assertIn("longest chain: 0 hop(s)", lines)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clean = self._write("clean.dep", CLEAN)
        self.broken = self._write("broken.dep", BROKEN)

    def _write(self, name, text):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_ci_passes_clean_manifest(self):
        code, out, _ = run_cli(["ci", self.clean])
        self.assertEqual(code, dc.EXIT_OK)
        self.assertIn("PASS", out)

    def test_ci_fails_broken_manifest_with_cycles_and_line_numbers(self):
        code, out, _ = run_cli(["ci", self.broken])
        self.assertEqual(code, dc.EXIT_FAIL)
        self.assertIn(f"{self.broken}:2: error: package 'ui' depends on itself", out)
        self.assertIn("cycle: api -> db -> api", out)
        self.assertIn("FAIL:", out)

    def test_ci_fails_cleanly_on_forbid_with_unknown_package(self):
        path = self._write("ghost.dep", "@forbid ghost -> core\ncore:\n")
        code, out, _ = run_cli(["ci", path])
        self.assertEqual(code, dc.EXIT_FAIL)
        self.assertIn("@forbid names unknown package 'ghost'", out)

    def test_lint_reports_missing_colon_on_its_line(self):
        code, out, _ = run_cli(["lint", self.broken])
        self.assertEqual(code, dc.EXIT_FAIL)
        self.assertIn(f"{self.broken}:6: error: missing ':' after package name", out)

    def test_order_on_cyclic_graph_exits_with_failure(self):
        code, out, err = run_cli(["order", self.broken])
        self.assertEqual(code, dc.EXIT_FAIL)
        self.assertEqual(out, "")
        self.assertIn("dependency cycle", err)

    def test_impact_of_unknown_package_is_a_usage_error(self):
        code, _, err = run_cli(["impact", self.clean, "nosuch"])
        self.assertEqual(code, dc.EXIT_USAGE)
        self.assertIn("unknown package 'nosuch'", err)

    def test_missing_file_is_a_usage_error(self):
        code, _, err = run_cli(["stats", os.path.join(self.tmp.name, "absent.dep")])
        self.assertEqual(code, dc.EXIT_USAGE)
        self.assertIn("cannot read", err)

    def test_impact_prints_affected_packages(self):
        code, out, _ = run_cli(["impact", self.clean, "logger"])
        self.assertEqual(code, dc.EXIT_OK)
        self.assertIn("Changing logger affects 5 package(s):", out)

    def test_diff_identical_manifests(self):
        code, out, _ = run_cli(["diff", self.clean, self.clean])
        self.assertEqual(code, dc.EXIT_OK)
        self.assertEqual(out, "no differences\n")


if __name__ == "__main__":
    unittest.main()
