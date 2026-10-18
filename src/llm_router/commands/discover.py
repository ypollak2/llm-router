"""Inspect cached discovery or explicitly refresh it."""
from __future__ import annotations

import json
import sys

from llm_router import discovery_rescan, model_discovery, seats

_USAGE = "usage: llm-router discover [--rescan] [--json]"


def cmd_discover(args) -> int:
    if any(arg in ("-h", "--help") for arg in args):
        print(_USAGE)
        return 0
    if any(arg not in ("--rescan", "--json") for arg in args):
        print(_USAGE, file=sys.stderr)
        return 2
    if "--rescan" in args:
        result = discovery_rescan.rescan(force=True)
        if "--json" in args:
            print(json.dumps(result.to_dict(), sort_keys=True))
            return 0
        for label, names in (
            ("Models added", result.ollama_added),
            ("Models removed", result.ollama_removed),
            ("Models modified", result.ollama_modified),
            ("Keys added", result.keys_added),
            ("Keys removed", result.keys_removed),
            ("Seats changed", result.seats_changed),
        ):
            print(f"{label}: {len(names)}" + (f" ({', '.join(names)})" if names else ""))
        if not result.changed:
            print("no changes")
        if not result.ollama_reachable:
            print("Ollama unreachable; previous cache retained.")
        return 0

    models, _age = model_discovery._read_cache()
    keys = discovery_rescan.key_names_present()
    state = discovery_rescan._read_state()
    cached_seats = seats.load_seats()
    if "--json" in args:
        view = discovery_rescan.RescanResult().to_dict()
        view.update({
            "models": models, "key_names": keys,
            "checked_at": state["checked_at"] if state else None,
            "seats": discovery_rescan._seat_state(cached_seats) if cached_seats else None,
        })
        print(json.dumps(view, sort_keys=True))
        return 0
    print("Models: " + (", ".join(models) or "(none cached)"))
    print("Key names: " + (", ".join(keys) or "(none)"))
    print(f"Last checked: {state['checked_at'] if state else 'never'}")
    if cached_seats is not None:
        print(cached_seats.summary_line())
    print("Run llm-router discover --rescan to refresh.")
    return 0
