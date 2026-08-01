"""Optional project-local config that remembers the source and output
directories, so ``lintle clean``/``verify``/``report`` and the interactive
wizard can run without repeating paths. Stored as stdlib JSON in
``./.lintle.json`` — no new dependency, and never an authority over an explicit
CLI argument (precedence is always: explicit arg > stored config > built-in
default). Only the two known string keys are read or written, so a hand-edited
file can never inject surprises."""

import json
from pathlib import Path

CONFIG_FILENAME = ".lintle.json"
_KEYS = ("source", "output")


def config_path(base: str = ".") -> Path:
    """Path to the project-local config file under ``base`` (cwd by default)."""
    return Path(base) / CONFIG_FILENAME


def load(base: str = ".") -> dict[str, str]:
    """Return the stored config, or ``{}`` when the file is absent, unreadable,
    corrupt, or not an object. Only the known string keys survive."""
    try:
        raw = json.loads(config_path(base).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: raw[k] for k in _KEYS if isinstance(raw.get(k), str) and raw[k]}


def save(config: dict[str, str], base: str = ".") -> Path:
    """Write the known non-empty keys to ``./.lintle.json`` (sorted, trailing
    newline) and return the path."""
    data = {k: config[k] for k in _KEYS if config.get(k)}
    path = config_path(base)
    # newline="\n" for the same reason every other writer pins it: on Windows the
    # default would translate "\n" to "\r\n" and the file would differ by platform.
    # (Skipping the durable-commit path is deliberate — this is a convenience file,
    # not a run artifact — but LF is not part of that trade.)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path
