"""Scenario-local checks for the SYNTHETIC example-trippy scenario — pure
functions of the run log. The party is invented: 2 adults, children aged 9
and 5. Every option spelling `trippy search --help` documents is accepted:
`--children 9,5`, `--child 9 --child 5`, and a comma list given to `--child`.
"""

from __future__ import annotations

import shlex

from aish.replay_checks import RunRecord, Verdict, command_text, failed_commands

PARTY_ADULTS = 2
PARTY_CHILDREN = [5, 9]
MAX_DETAIL_IDS = 3
_TRUE = {"true", "1", "yes", ""}


def _options(command: str) -> tuple[list[str], list[tuple[str, str | None]]]:
    """(positional words, [(option, value-or-None)]) for one trippy command."""
    tokens = shlex.split(command)
    words: list[str] = []
    options: list[tuple[str, str | None]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("--"):
            if "=" in token:
                key, value = token.split("=", 1)
                options.append((key, value))
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                options.append((token, tokens[i + 1]))
                i += 1
            else:
                options.append((token, None))
        else:
            words.append(token)
        i += 1
    return words, options


def _trippy(run: RunRecord, sub: str) -> list[tuple[str, list[tuple[str, str | None]]]]:
    found = []
    for call in run.commands():
        command = command_text(call)
        try:
            words, options = _options(command)
        except ValueError:
            continue
        if words[:2] == ["trippy", sub]:
            found.append((command, options))
    return found


def _party(options: list[tuple[str, str | None]]) -> tuple[int, list[int]]:
    adults = 2  # trippy's documented default when --adults is absent
    children: list[int] = []
    for key, value in options:
        if key == "--adults" and value is not None:
            adults = int(value)
        elif key in ("--child", "--children") and value is not None:
            children += [int(age) for age in value.split(",") if age.strip()]
    return adults, sorted(children)


def trippy_party(run: RunRecord) -> Verdict:
    name = "trippy_party"
    searches = _trippy(run, "search")
    if not searches:
        return Verdict(name, None, "no trippy search was proposed")
    wrong = []
    for command, options in searches:
        try:
            party = _party(options)
        except ValueError:
            wrong.append(command)
            continue
        if party != (PARTY_ADULTS, PARTY_CHILDREN):
            wrong.append(command)
    if not wrong:
        return Verdict(name, True, f"{len(searches)} searches, all --adults 2 + children "
                                   "9,5")
    return Verdict(name, False, f"{len(wrong)} of {len(searches)} searches with another party; "
                                f"first: {wrong[0]}")


def _single_unit(options: list[tuple[str, str | None]]) -> bool:
    for key, value in options:
        if key == "--single-unit":
            return True
        if key == "--filter" and value:
            for pair in value.split(","):
                field, _, setting = pair.partition("=")
                if field.strip() == "single_unit_only" and setting.strip().lower() in _TRUE:
                    return True
    return False


def single_unit_only(run: RunRecord) -> Verdict:
    name = "single_unit_only"
    searches = _trippy(run, "search")
    if not searches:
        return Verdict(name, None, "no trippy search was proposed")
    without = [command for command, options in searches if not _single_unit(options)]
    if not without:
        return Verdict(name, True, f"{len(searches)} searches, all single-unit")
    return Verdict(name, False, f"{len(without)} of {len(searches)} searches without "
                                f"single_unit_only; first: {without[0]}")


def details_max_three_ids(run: RunRecord) -> Verdict:
    name = "details_max_three_ids"
    details = _trippy(run, "details")
    if not details:
        return Verdict(name, None, "no trippy details was proposed")
    over = []
    for command, options in details:
        ids = [i for key, value in options if key == "--property-id" and value
               for i in value.split(",") if i.strip()]
        if len(ids) > MAX_DETAIL_IDS:
            over.append((len(ids), command))
    if not over:
        return Verdict(name, True, f"{len(details)} details calls, each with ≤{MAX_DETAIL_IDS} ids")
    count, command = over[0]
    return Verdict(name, False, f"{len(over)} of {len(details)} details calls over "
                                f"{MAX_DETAIL_IDS} ids; first ({count} ids): {command}")


def no_failed_trippy(run: RunRecord) -> Verdict:
    return failed_commands(run, prefix="trippy ", name="no_failed_trippy")


CHECKS = {
    "trippy_party": trippy_party,
    "single_unit_only": single_unit_only,
    "details_max_three_ids": details_max_three_ids,
    "no_failed_trippy": no_failed_trippy,
}
