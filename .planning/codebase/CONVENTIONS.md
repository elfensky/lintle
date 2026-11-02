---
last_mapped_commit: bf834f78234eaa5b6f879c0bff96587181b4d723
last_mapped_at: 2026-11-02
---
# Coding Conventions

**Analysis Date:** 2026-11-02

## Naming Patterns

**Files:**
- Modules are lowercase snake_case, one clear responsibility each: `src/lintle/tle.py`, `src/lintle/fsutil.py`, `src/lintle/run_planning.py`.
- Sub-package for the post-run auditor: `src/lintle/verify/` (`checks.py`, `grouping.py`, `orbit.py`, `records.py`, `report.py`, `scan.py`).
- Tests mirror modules: `src/lintle/pipeline.py` → `tests/test_pipeline.py`. Test files use `test_<module>.py`.

**Functions:**
- snake_case. Public helpers have no prefix: `tle.compute_checksum`, `tle.validate_record`, `repair.normalize_edges`.
- Module-private helpers take a leading underscore: `tle._check_columns`, `cli._chunk_records_type`, `cli._norad_id_type`, `cli._add_out_dir_positional`.
- argparse `type=` converters are named `_<thing>_type` (`_chunk_records_type`, `_norad_id_type`) and raise `argparse.ArgumentTypeError`.

**Variables:**
- snake_case. Module-level constants are UPPER_SNAKE: `LINE_LENGTH = 69` (`src/lintle/tle.py`), `CLEANED_SUFFIX = ".cleaned.txt"` and `CHUNK_RECORDS_DEFAULT` (`src/lintle/__init__.py`, `src/lintle/chunking.py`).
- Private module constants use a leading underscore: `_DIGIT`, `_CHECKSUM_CONTRIB`, `_LINE1_CHARS` (`src/lintle/tle.py`).

**Types:**
- Classes are PascalCase: `RecordCandidate`, `FileStats`, `ChunkedWriter`, `RuleID`, `RepairTier`.
- Enums subclass `enum.StrEnum` when the string value is a public contract: `RuleID` in `src/lintle/diagnostics.py` (`"TLE-CHK-001"`), `RepairTier`.
- Exceptions end in `Error` or name the failure: `ChunkSetError`, `DiffError`, `ExtractError`, `LockHeldError`, `UnknownTag`.
- Type aliases use `Literal` for closed vocabularies: `FieldErrorKind = Literal["length", "column", "semantic", "checksum", "catalog"]` in `src/lintle/tle.py`.

**Rule IDs and tags:**
- Diagnostic IDs follow `TLE-<FAMILY>-<NNN>` (`TLE-COL-001`, `TLE-CHK-001`, `TLE-PAIR-003`). IDs are never reused. See `src/lintle/diagnostics.py`.

## Code Style

**Formatting:**
- Tool: `ruff format` (Black-compatible output).
- Line length: 88 (`[tool.ruff] line-length = 88` in `pyproject.toml`).
- Target: Python 3.14 (`target-version = "py314"`).
- `docs/` is excluded from ruff (`extend-exclude`) because those files are point-in-time records.
- Long unavoidable lines (a fixed 69-column TLE literal) carry an inline `# noqa: E501` with a reason. Example in `tests/conftest.py`:
  ```python
  CANONICAL_LINE1 = (
      "1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753"  # noqa: E501 — a TLE line is a fixed 69-column record
  )
  ```

**Linting:**
- Tool: `ruff check` with rule sets `E`, `F`, `I`, `UP`, `B`, `SIM` (`pyproject.toml`).
- Every `# noqa` names the rule and states the reason after an em dash (`—`). Examples:
  - `src/lintle/chunking.py:93`: `# noqa: SIM115 — streaming; closed on roll/commit/discard`
  - `src/lintle/verify/report.py:183`: `# noqa: B006 — read-only, never mutated`
  - `src/lintle/verify/records.py:115`: `# noqa: SIM113 — two lines consumed per record, not one-per-item`
- Run `uv run ruff check .` and `uv run ruff format --check .` before committing. CI (`.github/workflows/ci.yml`) runs both and gates the test job on them.

**Modern idioms (enforced by hand where ruff does not enforce):**
- `match` for 3-or-more-way type or shape dispatch. A single two-way `isinstance` check stays a plain `if`. Five `match` statements exist in `src/lintle/`.
- `@dataclasses.dataclass(slots=True)` on every dataclass. Add `frozen=True` when immutable. Slotted dataclasses pickle across the worker pool, so do not rely on `__dict__`.
  - Example: `src/lintle/pipeline.py` (`RecordCandidate`, `Orphan`, `FileStarted`, `FileEnded`, `FileProgress`).
  - Example: `src/lintle/diagnostics.py` (`RuleSpec`, `Diagnostic` are frozen and slotted).
- `collections.Counter` with `.update()` for tallies. Do not write `d[k] = d.get(k, 0) + 1`. Convert back with `dict(...)` at any byte-deterministic output boundary so first-seen key order survives.
- f-strings, `X | None` unions, builtin generics (`list[bytes]`), `contextlib.suppress`, and PEP 758 `except A, B:` (no parentheses) are the house style. Examples: `src/lintle/verify/checks.py:88`, `src/lintle/diff.py:233`.

## Import Organization

**Order (enforced by ruff `I`):**
1. Standard library (`import dataclasses`, `import contextlib`, `from pathlib import Path`).
2. First-party package (`from lintle import ...`, `from lintle.diagnostics import ...`).
3. No third-party imports outside `rich`, `humanize`, `sgp4`, and the test tools (`pytest`, `hypothesis`).

Example from `src/lintle/pipeline.py`:
```python
import dataclasses
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from lintle import (
    BROKEN_DIRNAME,
    chunking,
    repair,
    report,
    stem,
    tle,
)
from lintle.chunking import CHUNK_RECORDS_DEFAULT
from lintle.diagnostics import Diagnostic, RuleID, diagnostic
```

**Patterns:**
- Import modules, not names, when the module is a collaborator: `from lintle import tle` then `tle.validate_record(...)`. Name imports are used for constants and small types.
- Shared constants come from the package root: `from lintle import CLEANED_SUFFIX, BROKEN_DIRNAME`. The root `src/lintle/__init__.py` owns every output filename and directory name. Do not re-encode a suffix in another module.
- Use `from lintle import config as user_config` to avoid a name clash in `src/lintle/cli.py`.

**Path Aliases:**
- Not used. Imports are absolute (`lintle.*`). Source lives under `src/` (`[tool.ruff] src = ["src"]`).

**Barrel files:**
- `src/lintle/__init__.py` is the only re-export surface. It holds `__version__` and the shared naming constants. It does not re-export the submodules. `src/lintle/verify/__init__.py` holds the `run()` orchestration.

## Error Handling

**Hard rule:** one bad record must not kill the run. Per-record failures become quarantine rows (`Diagnostic`), not exceptions. Example in `src/lintle/pipeline.py`:
```python
except Exception as exc:  # one bad record must not kill the run
```

**Patterns:**
- Validation returns data, not exceptions. `tle.validate_body(...)` returns `list[str]` of error messages (empty means valid). `tle.checksum_error(...)` returns `str | None`. Callers test truthiness.
- Domain errors subclass the nearest built-in or a module-local `Error`, and carry a message meant for users. Examples: `ChunkSetError` in `src/lintle/chunking.py`, `DiffError` in `src/lintle/diff.py`, `ExtractError` in `src/lintle/extract.py`.
- Wrap low-level errors with `from exc` so the traceback keeps the cause: `raise DiffError(...) from exc` (`src/lintle/diff.py:63`).
- Parsing errors at the CLI boundary become `argparse.ArgumentTypeError` with the bad value echoed as `{value!r}`. Pattern in `src/lintle/cli.py`:
  ```python
  try:
      n = int(value)
  except ValueError:
      raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
  ```
- Invariant violations raise `RuntimeError` or `ValueError` at import time, so a broken registry fails loudly. Examples: `src/lintle/diagnostics.py:147`, `src/lintle/categories.py:81`, `src/lintle/explain_examples.py:249`.
- The CLI maps errors to exit codes in one place: `src/lintle/cli.py` catches `fsutil.LockHeldError`, `OSError`, `ValueError`, and `KeyboardInterrupt` near the entry point.
- Silent swallowing is limited to a few justified spots: `src/lintle/config.py:26` (`except OSError, ValueError:` on an optional config file) and `src/lintle/cli_progress.py:294` (a transient render glitch must not stop the progress drain).
- Decode errors on input are caught at the byte boundary: `src/lintle/tle.py:411` (`except UnicodeDecodeError:`).

## Logging

**Framework:** None. No `logging` module. Output goes through two helpers.

**Patterns:**
- Human messages to stderr go through `src/lintle/term.py`: `term.error(...)`, `term.warning(...)`, `term.note(...)`, `term.prompt(...)`, and `term.emit(Severity, ...)`. Output off a TTY is plain text with no ANSI codes.
- Structured or machine output goes to stdout through `print(...)` or the `stdout_console` in `term.py`. It must be byte-deterministic. Do not add styling to it.
- Rich styling (`rich.console`) is reserved for a real terminal. The live tables live in `src/lintle/cli_progress.py` and `src/lintle/summary.py`.
- Never use rich markup for user-supplied text. Paths can contain `[` and `]`. `tests/test_term.py` pins this.

## Comments

**When to comment:**
- Explain the *why* a reader cannot see from the code. Example: the reason `tle.py` avoids `str.isdigit()` (Unicode digits) is written above `_DIGIT`.
- Name the invariant a line protects. Example: `# Fail loudly at collection time if either constant was mistranscribed.` in `tests/conftest.py`.
- Cite the critical rule or issue number when a design choice follows from it: `(#106)`, `(#199)`, `Critical Rules #1/#2`.

**Shortcut markers:**
- A shortcut with a known limit gets a comment marker. The marker in use is `ponytail:` (seven in `src/lintle/`: `chunking.py` 1, `verify/orbit.py` 4, `dedup.py` 2; for example `src/lintle/chunking.py:22`). No `shortcut:` marker exists in the code yet.

**JSDoc/TSDoc (docstrings):**
- Every public module, function, and class has a concise one-paragraph docstring, triple double quotes, on the first line or the line after `"""`. Match this style.
- Do not expand docstrings to `Args:` / `Returns:` / `Raises:` blocks. The house style is prose. Backticks wrap names in prose: ``"""True if ``field`` is non-empty ...``.
- Module docstrings state the layer and its constraints. Example: `src/lintle/tle.py` says "Pure functions only — no I/O." `src/lintle/chunking.py` says "Stdlib-only leaf ... never ``sgp4``."
- Private helpers may skip the docstring when the name and body are clear.

## Function Design

**Size:** Small, pure where possible. `src/lintle/tle.py` keeps validation as pure functions over strings. Large orchestrators (`src/lintle/cli.py`, 1014 lines; `src/lintle/pipeline.py`, 628 lines) delegate to leaf modules rather than grow.

**Parameters:**
- Take plain values (`str`, `bytes`, `Path`, `int`). Pass the collaborator module, not an injected object: `pipeline` imports `report_writers` directly.
- Keyword-only arguments are common for flags.
- Mutable default arguments are banned. The one exception is annotated with `# noqa: B006 — read-only, never mutated` in `src/lintle/verify/report.py`.

**Return Values:**
- Pure validators return `list[str]` (empty means valid) or `str | None`.
- Dataclasses carry multi-field results: `FileStats`, `RunPlan`, `Diagnostic`.
- Byte boundaries return `bytes` or write atomically through `fsutil.durable_replace` (`src/lintle/fsutil.py`).

## Module Design

**Exports:**
- Each module exposes plain module-level functions and dataclasses. No class is used just to group functions.
- Cross-module calls use `module.function`, which keeps tests able to `monkeypatch` the module attribute (see `tests/test_cli.py`).

**Import direction (structural rule):**
- The graph is one-way: `cli.py → pipeline.py → repair.py → tle.py`. `tle.py` and the data modules (`diagnostics.py`, `categories.py`, `explain_examples.py`) carry no I/O.
- `sgp4` and `lintle.verify` are never imported by the clean, validate, or repair path (`pipeline`, `repair`, `tle`, and the clean branch of `cli`). Only `src/lintle/verify/orbit.py` imports `sgp4`, lazily. An import-graph test enforces this.
- Do not create a cycle. If a new module needs a shared shape, put it in the leaf module (`report.py` dataclasses, `diagnostics.py`), not in the caller.

**Barrel Files:**
- Only `src/lintle/__init__.py` acts as a barrel, for constants. Sub-packages do not re-export.

## Modification Checklist for New Code

- Add a dataclass? Use `@dataclasses.dataclass(slots=True)`, plus `frozen=True` if immutable.
- Three or more shape cases? Use `match`.
- Tally a count? Use `collections.Counter`. Convert with `dict()` before writing bytes.
- New output file name? Add the constant to `src/lintle/__init__.py`. Do not hard-code it.
- New validation rule? Add it to `src/lintle/tle.py` only. Never add a second validator path (Critical Rule #4).
- Long literal? Add `# noqa: E501 — <reason>` on that line.

---

*Convention analysis: 2026-11-02*
