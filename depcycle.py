#!/usr/bin/env python3
"""Depcycle: an offline dependency-graph auditor for plain-text manifests.

A manifest lists what each package depends on, one package per line::

    # comments and blank lines are ignored
    app: ui api
    ui: core
    core:

Directives add policy: ``@forbid SRC -> DST`` says SRC must not depend on DST,
directly or through other packages, and ``@max-depth N`` caps the longest
dependency chain. Arrows always point from a package to what it needs.

Standard library only. Run ``python3 depcycle.py --help`` for the commands.
"""
from __future__ import annotations

import argparse
import heapq
import re
import sys
from collections import deque
from dataclasses import dataclass, field

NAME_RE = re.compile(r"[A-Za-z0-9_.\-/]+")
FORBID_RE = re.compile(r"@forbid\s+(\S+)\s*->\s*(\S+)")
DEPTH_RE = re.compile(r"@max-depth\s+(\d+)")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


class UsageError(Exception):
    """Bad arguments or unreadable input; the CLI exits with status 2."""


class CycleError(Exception):
    """The graph has a dependency cycle, so no build order exists."""


@dataclass
class Manifest:
    deps: dict = field(default_factory=dict)     # defined name -> dependency names, in file order
    lines: dict = field(default_factory=dict)    # defined name -> line of its definition
    refs: list = field(default_factory=list)     # (dependency, owner, line) for each accepted edge
    forbids: list = field(default_factory=list)  # (source, target, line)
    max_depth: int | None = None
    errors: list = field(default_factory=list)   # (line, message) found while parsing


def parse_manifest(text):
    """Parse manifest text. Bad lines are recorded in ``errors`` and skipped."""
    m = Manifest()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("@"):
            _parse_directive(m, line, lineno)
            continue
        if ":" not in line:
            m.errors.append((lineno, "missing ':' after package name"))
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        if not NAME_RE.fullmatch(name):
            m.errors.append((lineno, f"invalid package name {name!r}"))
            continue
        if name in m.deps:
            m.errors.append((lineno, f"duplicate package {name!r} (first defined on line {m.lines[name]})"))
        else:
            m.deps[name] = []
            m.lines[name] = lineno
        for dep in rest.split():
            if not NAME_RE.fullmatch(dep):
                m.errors.append((lineno, f"invalid dependency name {dep!r}"))
            elif dep in m.deps[name]:
                m.errors.append((lineno, f"duplicate dependency {dep!r} of {name!r}"))
            else:
                if dep == name:
                    m.errors.append((lineno, f"package {name!r} depends on itself"))
                m.deps[name].append(dep)
                m.refs.append((dep, name, lineno))
    return m


def _parse_directive(m, line, lineno):
    if match := FORBID_RE.fullmatch(line):
        m.forbids.append((match[1], match[2], lineno))
    elif match := DEPTH_RE.fullmatch(line):
        if m.max_depth is not None:
            m.errors.append((lineno, "duplicate @max-depth directive"))
        m.max_depth = int(match[1])
    else:
        m.errors.append((lineno, f"unknown directive {line.split()[0]!r}"))


def lint(m):
    """Return sorted (line, severity, message) findings. Severity is 'error' or 'warning'."""
    found = [(line, "error", msg) for line, msg in m.errors]
    referenced = set()
    for dep, owner, line in m.refs:
        referenced.add(dep)
        if dep not in m.deps:
            found.append((line, "error", f"unknown package {dep!r} (needed by {owner!r})"))
    for name, line in m.lines.items():
        if not m.deps[name] and name not in referenced:
            found.append((line, "warning", f"package {name!r} has no dependencies and nothing depends on it"))
    for src, dst, line in m.forbids:
        for name in (src, dst):
            if name not in m.deps:
                found.append((line, "error", f"@forbid names unknown package {name!r}"))
        if src == dst:
            found.append((line, "error", f"@forbid {src} -> {dst} compares a package with itself"))
    return sorted(found, key=lambda f: (f[0], f[1] != "error", f[2]))


def full_graph(deps):
    """Adjacency map that also includes referenced-but-undefined packages."""
    g = {name: list(targets) for name, targets in deps.items()}
    for targets in list(g.values()):
        for target in targets:
            g.setdefault(target, [])
    return g


def reverse(g):
    """Map each package to the packages that depend on it."""
    r = {name: [] for name in g}
    for name, targets in g.items():
        for target in targets:
            r[target].append(name)
    return r


def strongly_connected(g):
    """Tarjan's algorithm, iterative. Returns sorted groups of mutually dependent packages."""
    index, low, on_stack = {}, {}, set()
    stack, groups = [], []
    counter = 0
    for root in sorted(g):
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(sorted(g[root])))]
        while work:
            node, children = work[-1]
            descended = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(g[child]))))
                    descended = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                group = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    group.append(member)
                    if member == node:
                        break
                groups.append(sorted(group))
    return groups


def find_cycles(g):
    """One example cycle path per tangled group, e.g. ['a', 'b', 'a']."""
    cycles = []
    for group in strongly_connected(g):
        if len(group) > 1 or group[0] in g[group[0]]:
            cycles.append(_example_cycle(g, group))
    return cycles


def _example_cycle(g, group):
    start = group[0]
    if start in g[start]:
        return [start, start]
    members = set(group)
    parent = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in sorted(g[node]):
            if nxt not in members:
                continue
            if nxt == start:
                path = [node]
                while parent[path[-1]] is not None:
                    path.append(parent[path[-1]])
                return path[::-1] + [start]
            if nxt not in parent:
                parent[nxt] = node
                queue.append(nxt)
    return [start, start]  # unreachable for a real strongly connected group


def build_order(g):
    """Kahn's algorithm with alphabetical tie-breaks: dependencies come first."""
    pending = {name: len(targets) for name, targets in g.items()}
    dependents = reverse(g)
    ready = [name for name, count in pending.items() if count == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        name = heapq.heappop(ready)
        order.append(name)
        for parent in dependents[name]:
            pending[parent] -= 1
            if pending[parent] == 0:
                heapq.heappush(ready, parent)
    if len(order) < len(g):
        stuck = sorted(name for name, count in pending.items() if count > 0)
        raise CycleError(f"dependency cycle blocks {len(stuck)} package(s), e.g. {stuck[0]!r}")
    return order


def layers(g):
    """Level per package: 0 for no dependencies, otherwise 1 + the deepest dependency."""
    level = {}
    for name in build_order(g):
        level[name] = 1 + max((level[dep] for dep in g[name]), default=-1)
    return level


def closure(g, start):
    """Packages reachable from start (excluding start) mapped to their hop distance."""
    dist = {start: 0}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in sorted(g[node]):
            if nxt not in dist:
                dist[nxt] = dist[node] + 1
                queue.append(nxt)
    del dist[start]
    return dist


def shortest_path(g, src, dst):
    """Shortest dependency path from src to dst as a list of names, or None."""
    parent = {src: None}
    queue = deque([src])
    while queue:
        node = queue.popleft()
        if node == dst:
            path = []
            while node is not None:
                path.append(node)
                node = parent[node]
            return path[::-1]
        for nxt in sorted(g[node]):
            if nxt not in parent:
                parent[nxt] = node
                queue.append(nxt)
    return None


def policy_violations(m):
    """Return (line, message) pairs for broken @forbid rules and an exceeded @max-depth."""
    g = full_graph(m.deps)
    found = []
    for src, dst, line in m.forbids:
        if src != dst and src in g and dst in g:
            path = shortest_path(g, src, dst)
            if path:
                found.append((line, f"{src!r} must not depend on {dst!r}: {' -> '.join(path)}"))
    if m.max_depth is not None and g:
        if find_cycles(g):
            found.append((0, "@max-depth not checked because the graph has cycles"))
        else:
            level = layers(g)
            deepest = max(level.values())
            if deepest > m.max_depth:
                node = min(name for name, lvl in level.items() if lvl == deepest)
                found.append((0, f"longest chain is {deepest} hop(s), limit is {m.max_depth} (deepest: {node!r})"))
    return found


def stats_lines(m):
    g = full_graph(m.deps)
    rev = reverse(g)
    cycles = find_cycles(g)
    undefined = sorted(name for name in g if name not in m.deps)
    edges = sum(len(targets) for targets in g.values())
    lines = [
        f"packages: {len(g)} ({len(m.deps)} defined, {len(undefined)} undefined)",
        f"edges: {edges}",
        f"roots (nothing depends on them): {sum(1 for n in g if not rev[n])}",
        f"leaves (no dependencies): {sum(1 for n in g if not g[n])}",
        f"cycle groups: {len(cycles)}",
    ]
    if g:
        most_needs = sorted(g, key=lambda n: (-len(g[n]), n))[0]
        most_needed = sorted(g, key=lambda n: (-len(rev[n]), n))[0]
        lines.append(f"most dependencies: {most_needs} ({len(g[most_needs])})")
        lines.append(f"most depended on: {most_needed} ({len(rev[most_needed])})")
    if cycles:
        lines.append("longest chain: n/a (graph has cycles)")
    else:
        lines.append(f"longest chain: {max(layers(g).values(), default=0)} hop(s)")
    return lines


def diff_lines(old, new):
    ga, gb = full_graph(old.deps), full_graph(new.deps)
    edges_a = {(n, t) for n, ts in ga.items() for t in ts}
    edges_b = {(n, t) for n, ts in gb.items() for t in ts}
    lines = [f"+ package {n}" for n in sorted(set(gb) - set(ga))]
    lines += [f"- package {n}" for n in sorted(set(ga) - set(gb))]
    lines += [f"+ edge {s} -> {t}" for s, t in sorted(edges_b - edges_a)]
    lines += [f"- edge {s} -> {t}" for s, t in sorted(edges_a - edges_b)]
    return lines


def to_dot(m):
    g = full_graph(m.deps)
    in_cycle = set()
    for group in strongly_connected(g):
        if len(group) > 1 or group[0] in g[group[0]]:
            members = set(group)
            in_cycle.update((n, t) for n in group for t in g[n] if t in members)
    lines = ["digraph depcycle {", "  rankdir=LR;"]
    for name in sorted(g):
        style = "" if name in m.deps else " [style=dashed]"
        lines.append(f'  "{name}"{style};')
    for name in sorted(g):
        for target in sorted(g[name]):
            colour = " [color=red]" if (name, target) in in_cycle else ""
            lines.append(f'  "{name}" -> "{target}"{colour};')
    lines.append("}")
    return "\n".join(lines) + "\n"


def read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise UsageError(f"{path} is not UTF-8 text") from exc


def load_manifest(path, err=None):
    m = parse_manifest(read_text(path))
    if m.errors and err is not None:
        err.write(f"depcycle: warning: {path} has {len(m.errors)} parse error(s); run 'depcycle lint {path}'\n")
    return m


def _where(path, line):
    return f"{path}:{line}" if line else path


def cmd_lint(args, out, err):
    findings = lint(load_manifest(args.file))
    for line, severity, msg in findings:
        out.write(f"{_where(args.file, line)}: {severity}: {msg}\n")
    errors = sum(1 for f in findings if f[1] == "error")
    out.write(f"{errors} error(s), {len(findings) - errors} warning(s)\n")
    return EXIT_FAIL if errors else EXIT_OK


def cmd_cycles(args, out, err):
    cycles = find_cycles(full_graph(load_manifest(args.file, err).deps))
    if not cycles:
        out.write("no cycles\n")
        return EXIT_OK
    for cycle in cycles:
        out.write("cycle: " + " -> ".join(cycle) + "\n")
    out.write(f"{len(cycles)} cycle group(s)\n")
    return EXIT_FAIL


def cmd_order(args, out, err):
    order = build_order(full_graph(load_manifest(args.file, err).deps))
    for name in order:
        out.write(name + "\n")
    return EXIT_OK


def cmd_layers(args, out, err):
    level = layers(full_graph(load_manifest(args.file, err).deps))
    for lvl in sorted(set(level.values())):
        names = sorted(n for n, v in level.items() if v == lvl)
        out.write(f"level {lvl}: {' '.join(names)}\n")
    return EXIT_OK


def _reach_command(direction):
    def handler(args, out, err):
        g = full_graph(load_manifest(args.file, err).deps)
        if args.node not in g:
            raise UsageError(f"unknown package {args.node!r}")
        reached = closure(reverse(g) if direction == "impact" else g, args.node)
        if direction == "impact":
            out.write(f"Changing {args.node} affects {len(reached)} package(s):\n")
        else:
            out.write(f"{args.node} needs {len(reached)} package(s):\n")
        if not reached:
            out.write("  (none)\n")
        for name, depth in sorted(reached.items(), key=lambda kv: (kv[1], kv[0])):
            out.write(f"  {depth:>2}  {name}\n")
        return EXIT_OK
    return handler


def cmd_roots(args, out, err):
    g = full_graph(load_manifest(args.file, err).deps)
    rev = reverse(g)
    roots = sorted(n for n in g if not rev[n])
    out.write("roots (nothing depends on them): " + (" ".join(roots) or "(none)") + "\n")
    return EXIT_OK


def cmd_check(args, out, err):
    m = load_manifest(args.file, err)
    if not m.forbids and m.max_depth is None:
        out.write("no @forbid or @max-depth rules declared\n")
        return EXIT_OK
    violations = policy_violations(m)
    for line, msg in violations:
        out.write(f"{_where(args.file, line)}: violation: {msg}\n")
    if violations:
        out.write(f"FAIL: {len(violations)} violation(s)\n")
        return EXIT_FAIL
    out.write("PASS: all policy rules hold\n")
    return EXIT_OK


def cmd_stats(args, out, err):
    for line in stats_lines(load_manifest(args.file, err)):
        out.write(line + "\n")
    return EXIT_OK


def cmd_diff(args, out, err):
    lines = diff_lines(load_manifest(args.old, err), load_manifest(args.new, err))
    for line in lines:
        out.write(line + "\n")
    if not lines:
        out.write("no differences\n")
    return EXIT_OK


def cmd_dot(args, out, err):
    out.write(to_dot(load_manifest(args.file, err)))
    return EXIT_OK


def cmd_ci(args, out, err):
    m = load_manifest(args.file)
    problems = 0
    for line, severity, msg in lint(m):
        if severity == "error":
            problems += 1
            out.write(f"{_where(args.file, line)}: error: {msg}\n")
    for cycle in find_cycles(full_graph(m.deps)):
        problems += 1
        out.write(f"{args.file}: cycle: {' -> '.join(cycle)}\n")
    for line, msg in policy_violations(m):
        problems += 1
        out.write(f"{_where(args.file, line)}: violation: {msg}\n")
    if problems:
        out.write(f"FAIL: {problems} problem(s)\n")
        return EXIT_FAIL
    out.write("PASS: manifest is clean\n")
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="depcycle",
        description="Audit package dependency manifests offline. Arrows point from a package to what it needs.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    single = {
        "lint": ("check a manifest for syntax and reference errors, line by line", cmd_lint),
        "cycles": ("list dependency cycles, one example path per tangled group", cmd_cycles),
        "order": ("print a build order with dependencies first", cmd_order),
        "layers": ("group packages by depth; level 0 has no dependencies", cmd_layers),
        "roots": ("list packages that nothing else depends on", cmd_roots),
        "check": ("check @forbid rules and the @max-depth limit", cmd_check),
        "stats": ("summarise size, fan-in and fan-out, depth and cycles", cmd_stats),
        "dot": ("export a Graphviz DOT graph, with cycle edges in red", cmd_dot),
        "ci": ("run lint, cycle and policy checks; exit 1 on any problem", cmd_ci),
    }
    for name, (text, handler) in single.items():
        p = sub.add_parser(name, help=text, description=text)
        p.add_argument("file", help="manifest file")
        p.set_defaults(handler=handler)

    for name, text in (
        ("needs", "list everything a package needs, directly or indirectly"),
        ("impact", "list everything a change to a package affects"),
    ):
        p = sub.add_parser(name, help=text, description=text)
        p.add_argument("file", help="manifest file")
        p.add_argument("node", help="package name")
        p.set_defaults(handler=_reach_command(name))

    p = sub.add_parser("diff", help="compare two manifests by packages and edges",
                       description="compare two manifests by packages and edges")
    p.add_argument("old", help="earlier manifest")
    p.add_argument("new", help="later manifest")
    p.set_defaults(handler=cmd_diff)
    return parser


def main(argv=None, out=None, err=None):
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args, out, err)
    except UsageError as exc:
        err.write(f"depcycle: {exc}\n")
        return EXIT_USAGE
    except CycleError as exc:
        err.write(f"depcycle: {exc}; run 'depcycle cycles FILE' for the loop\n")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
