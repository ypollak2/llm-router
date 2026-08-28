"""CLI integration for TUI dashboard (v0.3.3+).

Provides the --watch flag for `llm_router route` to launch interactive dashboard.
"""

from __future__ import annotations

from typing import Any, Coroutine

import typer

from llm_router.types import TaskType


def maybe_launch_tui(
    task_type: TaskType,
    prompt: str,
    watch: bool = False,
    **routing_kwargs: Any,
) -> Coroutine[Any, Any, None]:
    """Launch TUI dashboard if --watch flag is set.

    If Textual is not installed, falls back to classic text output.

    Args:
        task_type: Task type for routing
        prompt: User prompt
        watch: Whether to launch interactive dashboard
        **routing_kwargs: Arguments for route_and_stream()

    Returns:
        Coroutine for the routing operation (TUI or text-based)
    """

    if not watch:
        # Classic text-based output
        return _run_classic_mode(task_type, prompt, **routing_kwargs)

    # Try to import TUI components
    try:
        from llm_router.tui import run_dashboard

        return run_dashboard(
            task_type=task_type,
            prompt=prompt,
            **routing_kwargs,
        )
    except ImportError:
        typer.echo(
            "⚠️  Textual not installed. Falling back to classic output.",
            err=True,
        )
        typer.echo(
            "   Install with: pipx inject llm-routing textual",
            err=True,
        )
        return _run_classic_mode(task_type, prompt, **routing_kwargs)


async def _run_classic_mode(
    task_type: TaskType,
    prompt: str,
    **routing_kwargs: Any,
) -> None:
    """Run classic text-based routing output."""
    from llm_router.router import route_and_call

    response = await route_and_call(
        task_type=task_type,
        prompt=prompt,
        **routing_kwargs,
    )

    print("\n" + "=" * 60)
    print("RESPONSE")
    print("=" * 60)
    print(response.content)
    print("\n" + "=" * 60)
    print("METRICS")
    print("=" * 60)
    print(f"Model: {response.model}")
    print(f"Tokens: {response.input_tokens} → {response.output_tokens}")
    print(f"Cost: ${response.cost_usd:.6f}")
    print(f"Latency: {response.latency_ms:.1f}ms")
    print("=" * 60)
