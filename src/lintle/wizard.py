"""Interactive ``lintle`` wizard (rich): the front-end shown when ``lintle`` is
run with no subcommand on a TTY. It remembers the source/output directories via
:mod:`lintle.config`, re-checks they still exist on start, and dispatches
configure / clean / verify / report actions. A thin presentation leaf — it
constructs an argv and hands it to the ``dispatch`` callable ``cli`` passes in
(dependency inversion: the wizard never imports ``cli``, so the dependency edge
is strictly one-way, cli -> wizard). Off a TTY
it never runs; ``cli`` falls back to printing help. rich styling is fine here
because the wizard only ever runs on a TTY."""

from pathlib import Path

from rich.panel import Panel
from rich.prompt import Prompt

from lintle import config, term

# menu key -> (action, description)
_MENU = {
    "1": ("clean", "Clean the source directory -> output"),
    "2": ("verify", "Verify a previous clean run's output"),
    "3": ("report", "View the last run's report"),
    "4": ("configure", "Set the source / output directories"),
    "5": ("quit", "Exit"),
}
_DEFAULTS = {"source": "data/source", "output": "data/output"}


def _ask(label: str, default: str) -> str:
    return Prompt.ask(label, default=default, console=term.stderr_console).strip()


def _configure(cfg: dict[str, str]) -> dict[str, str]:
    """Prompt for both directories, persist them, and return the updated config."""
    cfg = dict(cfg)
    cfg["source"] = _ask(
        "Source directory (original TLEs)", cfg.get("source") or _DEFAULTS["source"]
    )
    cfg["output"] = _ask(
        "Output directory (cleaned/broken + report)",
        cfg.get("output") or _DEFAULTS["output"],
    )
    saved = config.save(cfg)
    term.note(f"saved configuration to {saved}")
    return cfg


def _ensure_paths(cfg: dict[str, str]) -> dict[str, str]:
    """Make sure both directories are known and still present, prompting only for
    any that are unset or have vanished since the last run."""
    gone = [k for k in ("source", "output") if cfg.get(k) and not Path(cfg[k]).exists()]
    for key in gone:
        term.warning(f"configured {key} path no longer exists: {cfg[key]}")
    if gone or not all(cfg.get(k) for k in ("source", "output")):
        return _configure(cfg)
    return cfg


def _dispatch(cfg: dict[str, str], action: str, dispatch) -> int:
    """Run one action by handing an explicit argv to ``dispatch`` (``cli.main``)."""
    match action:
        case "clean":
            return dispatch(["clean", cfg["source"], "--out-dir", cfg["output"]])
        case "verify":
            return dispatch(["verify", cfg["output"], "--source", cfg["source"]])
        case "report":
            return dispatch(["report", cfg["output"]])
    return 0


def run(dispatch) -> int:
    """Run the interactive menu loop, handing each chosen action's argv to
    ``dispatch`` (``cli.main``, injected so the wizard never imports cli).
    Returns the exit code of the last dispatched command (0 if none was run);
    Ctrl-C / EOF exits cleanly."""
    try:
        cfg = _ensure_paths(config.load())
        last = 0
        while True:
            body = "\n".join(
                f"  [bold]{key}[/bold]  {desc}" for key, (_, desc) in _MENU.items()
            )
            subtitle = f"source: {cfg['source']}   output: {cfg['output']}"
            term.stderr_console.print(
                Panel.fit(body, title="lintle", subtitle=subtitle, border_style="cyan")
            )
            choice = Prompt.ask(
                "Choose", choices=list(_MENU), default="1", console=term.stderr_console
            )
            action = _MENU[choice][0]
            if action == "quit":
                return last
            if action == "configure":
                cfg = _configure(cfg)
                continue
            last = _dispatch(cfg, action, dispatch)
    except KeyboardInterrupt, EOFError:
        term.note("")
        return 130
