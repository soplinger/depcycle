# Depcycle

**Audit package dependency manifests offline: cycles, build order, blast radius and layering rules from one plain-text file.**

Depcycle reads a simple `name: dependency ...` manifest and answers the questions a monorepo keeps asking: *is there a loop, what order do I build in, what breaks if I touch this package, and does anything reach across a boundary it shouldn't?* It needs only Python 3's standard library, makes no network calls, and keeps no state.

## Features

- **Line-numbered lint**: syntax errors, invalid names, duplicate packages and dependencies, self-dependencies, unknown directives and unknown package references, each reported as `file:line`.
- **Isolated-package warnings**: packages with no dependencies that nothing uses.
- **Cycle detection** with Tarjan's strongly-connected-components algorithm, printing one example loop per tangled group (`api -> db -> api`).
- **Build order** with dependencies first and alphabetical tie-breaks, so output is stable run to run.
- **Layers**: group packages by depth, where level 0 has no dependencies.
- **Needs**: everything a package depends on, directly or indirectly, with hop distance.
- **Impact**: everything a change to a package affects (reverse closure), with hop distance.
- **Roots**: top-level packages that nothing else depends on.
- **Policy rules**: `@forbid SRC -> DST` blocks transitive dependencies, with the offending path shown; `@max-depth N` caps the longest chain.
- **Stats**: package and edge counts, undefined references, fan-in and fan-out leaders, cycle groups and longest chain.
- **Diff**: added and removed packages and edges between two manifests.
- **Graphviz export**: `dot` writes a DOT graph with cycle edges in red and undefined packages dashed.
- **CI gate**: `ci` runs lint, cycle and policy checks together and exits 1 on any problem.

## Manifest format

```text
# comments and blank lines are ignored
@forbid ui -> db        # ui must not depend on db, directly or indirectly
@max-depth 4            # longest dependency chain allowed

app: ui api             # arrows point from a package to what it needs
ui: core
api: core db
db: core logger
core: logger
logger:
```

## Quick start

Requires Python 3.8 or newer. There is nothing to install.

```sh
python3 depcycle.py ci examples/monorepo.dep        # PASS/FAIL gate, exit 0 or 1
python3 depcycle.py layers examples/monorepo.dep    # group packages by depth
python3 depcycle.py impact examples/monorepo.dep logger   # what a change to logger affects
```

To see the error paths, run the same commands against the deliberately broken example:

```sh
python3 depcycle.py lint examples/broken.dep
python3 depcycle.py cycles examples/broken.dep
```

## Commands

| Command | What it does |
| --- | --- |
| `lint FILE` | Line-numbered errors and warnings; exit 1 if any error |
| `cycles FILE` | Dependency loops, one example path per group |
| `order FILE` | Build order, dependencies first (refuses cyclic graphs) |
| `layers FILE` | Packages grouped by depth |
| `needs FILE NODE` | Everything NODE depends on, with hop counts |
| `impact FILE NODE` | Everything a change to NODE affects, with hop counts |
| `roots FILE` | Packages nothing else depends on |
| `check FILE` | Evaluate `@forbid` and `@max-depth` rules |
| `stats FILE` | Size, fan-in/out, cycle groups and longest chain |
| `diff OLD NEW` | Added and removed packages and edges |
| `dot FILE` | Graphviz DOT export |
| `ci FILE` | Lint, cycles and policy in one gate |

Exit codes: `0` success, `1` the manifest has problems (lint errors, cycles, violations), `2` usage error such as a missing file or unknown package.

## How to run

Run `python3 depcycle.py --help` or `python3 depcycle.py COMMAND --help`. Each command reads one manifest; `diff` reads two. Error lines go to standard output as `file:line: error: message`, so they can be read by editors and CI log viewers.

## How to test

```sh
python3 -m pytest
```

Or without pytest:

```sh
python3 -m unittest discover -s tests -v
```

The tests cover parsing and its error lines, lint findings, cycle detection, build order, layers, reach and impact, policy checks, diff and DOT output, and the CLI exit codes.

## Notes

- Depcycle treats every `name: deps` line as one package. Package names may use letters, digits, `_`, `.`, `-` and `/`.
- Cycles are reported once per tangled group, not once per possible loop, so a group may show only one of several valid paths.
- A package that is referenced but never defined is an error in `lint`. Other commands still include it in the graph so that impact and needs results stay complete.
