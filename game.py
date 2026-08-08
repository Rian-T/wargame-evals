"""Shared game mechanics for the 1v1 experiments."""

COUNTRIES = ["Alpha", "Beta"]
TOTAL_TURNS = 10

# Percent of oranium won per level of pressure advantage.
GAIN_PER_LEVEL = 5

# Length of the public message echoed in the console log.
MESSAGE_PREVIEW_CHARS = 60


def transfer(control: dict, winner: str, loser: str, gain: float) -> None:
    """Move `gain` percent of oranium to the winner, from the pool then the loser."""
    from_pool = min(gain, control["pool"])
    from_loser = min(gain - from_pool, control[loser])
    control["pool"] -= from_pool
    control[loser] -= from_loser
    control[winner] += from_pool + from_loser


def resolve_conventional(control: dict, alpha_level: int, beta_level: int) -> None:
    """Apply one round of non-nuclear resolution in place."""
    if alpha_level > beta_level:
        transfer(control, "Alpha", "Beta", (alpha_level - beta_level) * GAIN_PER_LEVEL)
    elif beta_level > alpha_level:
        transfer(control, "Beta", "Alpha", (beta_level - alpha_level) * GAIN_PER_LEVEL)


def simulate(history: list) -> dict:
    """Play a scenario history and return the resulting control state.

    `history` is a list of (alpha_level, alpha_target, beta_level, beta_target)
    tuples. The control state a scenario presents to the model is always
    computed here, never written by hand, so it cannot drift from the history.
    """
    control = {"Alpha": 0.0, "Beta": 0.0, "pool": 100.0}
    for alpha_level, _, beta_level, _ in history:
        resolve_conventional(control, alpha_level, beta_level)
    return control


def format_history(history: list) -> list[str]:
    """Render the last round in full plus a compact log of every round."""
    if not history:
        return []
    parts = []
    for country, entry in history[-1].items():
        target = entry.get("target") or "none"
        parts.append(f'- {country}: "{entry["message"]}" '
                     f'→ level {entry["action_level"]}, target: {target}')
    parts.append("")
    parts.append("Full history:")
    for i, round_entries in enumerate(history, 1):
        summary = ", ".join(
            f"{c}={e['action_level']}→{e.get('target') or 'none'}"
            for c, e in round_entries.items())
        parts.append(f"Round {i}: {summary}")
    parts.append("")
    return parts


def state_line(control: dict) -> str:
    return (f"Control: Alpha={control['Alpha']:.1f}%, Beta={control['Beta']:.1f}%, "
            f"pool={control['pool']:.1f}%")
