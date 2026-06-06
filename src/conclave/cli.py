"""conclave command-line interface.

Two commands:

* ``conclave ask "<prompt>" --council grok,gemini,claude --mode synthesize``
* ``conclave providers`` -- show which providers currently have a key (without
  ever printing key values).
"""

from __future__ import annotations

import json
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import load_config
from .council import Council
from .models import CouncilResult
from .registry import DEFAULT_MODELS, key_present, key_source

app = typer.Typer(
    add_completion=False,
    help="Bring-your-own-keys multi-model council. Fan a prompt to N models.",
)
console = Console()
err_console = Console(stderr=True)


def _result_to_dict(result: CouncilResult) -> dict:
    """Serialize a CouncilResult to a JSON-safe dict (no secrets included)."""
    return result.model_dump(mode="json")


def _render_human(result: CouncilResult) -> None:
    """Render raw answers + synthesis to the terminal with rich."""
    if result.skipped:
        err_console.print(
            f"[yellow]Skipped (no key): {', '.join(result.skipped)}[/yellow]"
        )

    for ans in result.answers:
        if ans.ok:
            usage = ans.usage.total_tokens if ans.usage else 0
            subtitle = f"{ans.model_id} · {ans.latency_s:.2f}s · {usage} tok"
            console.print(
                Panel(
                    ans.answer or "",
                    title=f"[bold]{ans.name}[/bold]",
                    subtitle=subtitle,
                    border_style="cyan",
                )
            )
        else:
            console.print(
                Panel(
                    f"[red]{ans.error}[/red]",
                    title=f"[bold]{ans.name}[/bold] (failed)",
                    subtitle=ans.model_id,
                    border_style="red",
                )
            )

    if result.synthesis is not None:
        console.print(
            Panel(
                result.synthesis,
                title=f"[bold green]SYNTHESIS[/bold green] "
                f"({result.synthesizer} · {result.synthesizer_model_id})",
                border_style="green",
            )
        )
    elif result.synthesis_error:
        err_console.print(f"[yellow]No synthesis: {result.synthesis_error}[/yellow]")


@app.command()
def ask(
    prompt: str = typer.Argument(..., help="The prompt to send to the council."),
    council: str = typer.Option(
        "default",
        "--council",
        "-c",
        help="Named council or comma-separated friendly names (e.g. grok,claude).",
    ),
    mode: str = typer.Option(
        "synthesize", "--mode", "-m", help="Run mode: 'synthesize' or 'raw'."
    ),
    synthesizer: Optional[str] = typer.Option(
        None, "--synthesizer", "-s", help="Override the synthesizer model name."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the full result as JSON instead of panels."
    ),
) -> None:
    """Fan PROMPT out to a council of models and (optionally) synthesize."""
    cfg = load_config()
    members = cfg.resolve_council(council)
    if not members:
        err_console.print(f"[red]No council members resolved from '{council}'.[/red]")
        raise typer.Exit(code=2)

    do_synth = mode.lower() == "synthesize"
    c = Council(models=members, synthesizer=synthesizer, config=cfg)
    result = c.ask_sync(prompt, synthesize=do_synth)

    if as_json:
        console.print_json(json.dumps(_result_to_dict(result)))
        return

    if not result.answers:
        err_console.print(
            "[red]No council members had keys available. "
            "Run 'conclave providers' to check.[/red]"
        )
        raise typer.Exit(code=1)

    _render_human(result)


@app.command()
def providers() -> None:
    """List known providers and whether a key is present (no values shown)."""
    cfg = load_config()
    table = Table(title="conclave providers")
    table.add_column("name", style="bold")
    table.add_column("model id")
    table.add_column("key", justify="center")
    table.add_column("env var")

    for name, model_id in sorted(cfg.models.items()):
        present = key_present(model_id)
        mark = "[green]✓[/green]" if present else "[red]✗[/red]"
        source = key_source(model_id) or "[dim]-[/dim]"
        table.add_row(name, model_id, mark, source)

    console.print(table)
    console.print(
        f"[dim]synthesizer default: {cfg.synthesizer} · conclave {__version__}[/dim]"
    )


def _builtin_default_note() -> str:
    """Return a short note listing built-in providers (used in --help footer)."""
    return ", ".join(DEFAULT_MODELS.keys())


if __name__ == "__main__":
    app()
