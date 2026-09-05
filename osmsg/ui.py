"""Rich console + progress helpers."""

from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

console = Console()
err_console = Console(stderr=True)


def info(message: str) -> None:
    console.print(message)


def warn(message: str) -> None:
    err_console.print(f"[yellow]warning[/yellow] {message}")


def error(message: str) -> None:
    err_console.print(f"[bold red]error[/bold red] {message}")


@contextmanager
def progress_bar(total: int, unit: str = "items", description: str = "processing"):
    # transient=False keeps a one-line summary so cron logs / file-redirected stdout retain context.
    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total} " + unit),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as bar:
        task = bar.add_task(description, total=total)

        def advance() -> None:
            bar.advance(task)

        yield advance


def render_table(rows: list[dict[str, Any]], columns: Iterable[str], title: str | None = None) -> None:
    table = Table(title=title, show_lines=False)
    cols = [c for c in columns if any(c in r for r in rows)]
    for col in cols:
        table.add_column(col)
    for r in rows:
        table.add_row(*(_fmt(r.get(c)) for c in cols))
    console.print(table)


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(v) for v in value[:3]) + ("…" if len(value) > 3 else "")
    if isinstance(value, dict):
        return f"{{{len(value)} keys}}"
    return str(value)
