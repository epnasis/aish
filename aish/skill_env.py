"""A third-party skill's Python packages (epic #467 slice 2b).

A skill's own code imports packages — the anthropics `pdf` skill imports
`pypdf` and `reportlab` — and tells its reader to `pip install` them. aish
never follows that line: the packages are derived from the skill's import
STATEMENTS (`ast`, never prose), shown on the import's review card, and
installed only when the owner approves the import, into an environment of the
skill's own outside its reviewed folder:
`<state>/skill-envs/<name>/`.

Never at run time. That is not a promise this module keeps by being careful:
the runner's Tier S profile denies the network and does not let the runner
write the environment (`sandbox.render_skill_profile`), so nothing a skill
does while it runs can install anything.

Wheels only (`--only-binary :all:`). Building a source distribution runs the
package's own build code with the owner's rights at import time — exactly the
third-party execution the runner exists to confine.

Slice 1 is scoped to an explicit table (`PACKAGES`) for the names the
anthropics `pdf` skill imports. An import the table does not know gets nothing
installed for it and the card says so, never a guess: an import name is not a
package name (`PIL` is `pillow`, `fitz` is `pymupdf`), and a guessed one would
install whatever a stranger published under the guess. The card does not claim
such an import will be missing — another package may bring it in — only that
aish installed nothing for it. Imports that drive a program aish does not
provide (`PROGRAMS`) are named with that program.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .paths import state_home

# Import name -> the PyPI package aish installs for it. Exactly the names the
# anthropics `pdf` skill imports that ship wheels (the orchestrator ran `needs`
# on the staged skill, 2026-11-12); growing it is a reviewed change, never a lookup.
PACKAGES = {
    "pypdf": "pypdf",
    "reportlab": "reportlab",
    "PIL": "pillow",
    "pdfplumber": "pdfplumber",
    "pypdfium2": "pypdfium2",
    "pandas": "pandas",
    "numpy": "numpy",
}

# Imports that are thin wrappers over a program outside Python, which aish
# does not install (slice 1 excludes them): the import -> the program.
PROGRAMS = {
    "pdf2image": "poppler",
    "pytesseract": "tesseract",
}

INSTALL_TIMEOUT_S = 300

_PYTHON_BLOCK = re.compile(r"^```(?:python|py)[ \t]*\n(.*?)^```", re.M | re.S)


@dataclass(frozen=True)
class Needs:
    """What a skill's code asks for: whether it carries Python at all, the
    packages aish will install, and the imports it will not, with why."""

    python: bool
    packages: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    renamed: tuple[tuple[str, str], ...] = ()
    programs: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Environment:
    """A skill's installed environment, as the sandbox profile needs it."""

    dir: Path
    interpreter: str  # real path of its python
    prefix: str  # the interpreter's own installation (stdlib, libpython)


def _python_sources(files: Iterable[tuple[str, str]]) -> list[str]:
    sources = []
    for rel, text in files:
        if rel.endswith(".py"):
            sources.append(text)
        elif rel.endswith(".md"):
            sources.extend(m.group(1) for m in _PYTHON_BLOCK.finditer(text))
    return sources


def _top_level_imports(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def needs(files: Iterable[tuple[str, str]]) -> Needs:
    """Read the packages out of a skill's `.py` files and its Markdown's
    python code blocks. The standard library and the skill's own modules are
    not packages; everything else is in `PACKAGES` or reported as unknown."""
    files = list(files)
    sources = _python_sources(files)
    if not sources:
        return Needs(python=False)
    own = {Path(rel).stem for rel, _ in files if rel.endswith(".py")}
    own |= {Path(rel).parts[0] for rel, _ in files if "/" in rel}
    imported: set[str] = set()
    for source in sources:
        imported |= _top_level_imports(source)
    foreign = sorted(
        name for name in imported
        if name not in sys.stdlib_module_names and name not in own and name != "__future__"
    )
    packages = tuple(sorted({PACKAGES[n] for n in foreign if n in PACKAGES}))
    renamed = tuple((n, PACKAGES[n]) for n in foreign if n in PACKAGES and PACKAGES[n] != n)
    programs = tuple((n, PROGRAMS[n]) for n in foreign if n in PROGRAMS)
    unknown = tuple(n for n in foreign if n not in PACKAGES and n not in PROGRAMS)
    return Needs(python=True, packages=packages, unknown=unknown, renamed=renamed,
                 programs=programs)


def needs_of_folder(folder: Path) -> Needs:
    """`needs` of an installed skill, read back from disk: the bytes that
    were approved are the bytes on disk, and these are what will run."""
    files = []
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.suffix in (".py", ".md"):
            try:
                files.append((str(path.relative_to(folder)), path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                continue
    return needs(files)


def card_flags(found: Needs) -> list[str]:
    """The lines the import's review shows about packages — what will be
    installed on approval, and what will not."""
    if not found.python:
        return []
    flags = [
        "installs a Python environment for this skill"
        + (
            f" with packages {', '.join(found.packages)} (wheels only, versions recorded)"
            if found.packages else " with no packages"
        )
    ]
    flags += [
        f"imports {imp!r}, installed as the package {pkg!r}" for imp, pkg in found.renamed
    ]
    flags += [
        f"imports {imp!r}, which drives the program {program!r}: aish installs "
        "neither, and does not provide that program"
        for imp, program in found.programs
    ]
    if found.unknown:
        flags.append(
            f"imports {', '.join(found.unknown)}, which aish's package table does not "
            "name: aish installs nothing for them"
        )
    return flags


def envs_root() -> Path:
    """Resolved at CALL time through `paths.state_home`, like the quarantine,
    so a run that moved its state tree installs into its own tree."""
    return state_home() / "skill-envs"


def env_dir(name: str) -> Path:
    return envs_root() / name


def _run_uv(argv: list[str]) -> subprocess.CompletedProcess:
    """The one process this module starts. Tests replace it: an install is a
    network fetch, which no test may make."""
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S, check=False,
        env={**os.environ, "UV_NO_CONFIG": "1"},
    )


def _uv() -> str | None:
    # aish-web runs under launchd, whose PATH need not hold uv's usual home.
    found = shutil.which("uv")
    fallback = Path.home() / ".local" / "bin" / "uv"
    return found or (str(fallback) if os.access(fallback, os.X_OK) else None)


def install(name: str, found: Needs) -> dict:
    """Make the skill's environment and install its packages; the record the
    lock keeps (`skill_import.record_import`'s `env`). Never raises for an
    operational reason — a failed install is a record that says so, and the
    skill stays installed with its runs having no Python."""
    if not found.python:
        return {}
    target = env_dir(name)
    uv = _uv()
    if uv is None:
        return {"installed": False, "error": "uv is not on this machine's PATH"}
    try:
        shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        steps = [[uv, "venv", "--python", sys.executable, str(target)]]
        python = str(target / "bin" / "python")
        if found.packages:
            steps.append([uv, "pip", "install", "--python", python,
                          "--only-binary", ":all:", *found.packages])
        for argv in steps:
            done = _run_uv(argv)
            if done.returncode != 0:
                shutil.rmtree(target, ignore_errors=True)
                tail = (done.stderr or done.stdout or "").strip()[-300:]
                return {"installed": False,
                        "error": f"{' '.join(argv[1:3])} exited {done.returncode}: {tail}"}
        versions: dict[str, str] = {}
        if found.packages:
            done = _run_uv([uv, "pip", "freeze", "--python", python])
            for line in (done.stdout or "").splitlines():
                package, sep, version = line.partition("==")
                if sep:
                    versions[package.strip().lower()] = version.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(target, ignore_errors=True)
        return {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"installed": True, "packages": versions}


def install_note(record: dict) -> str:
    """The one sentence both install paths say about the environment."""
    if not record:
        return ""
    if record.get("installed"):
        packages = record.get("packages") or {}
        listed = ", ".join(f"{p} {v}" for p, v in sorted(packages.items())) or "no packages"
        return f"its Python environment was installed ({listed})"
    return (
        f"its Python environment was NOT installed ({record.get('error', 'no reason recorded')}); "
        "its runs will have no Python"
    )


def locate(name: str) -> Environment | str | None:
    """The skill's environment as the sandbox needs it, None when it has
    none, or a sentence saying why the one on disk cannot be used.

    Read from `pyvenv.cfg`'s `home`, never by running the interpreter: with
    site enabled a `.pth` file in the environment would run a stranger's code
    with the owner's rights. The interpreter must live inside the
    installation that file names, and that installation must not hold the
    owner's home — the profile opens it for reading and mapping code."""
    target = env_dir(name)
    config = target / "pyvenv.cfg"
    if not config.is_file():
        return None
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return f"its environment's pyvenv.cfg cannot be read: {exc}"
    home = next(
        (v.strip() for k, _, v in (ln.partition("=") for ln in lines) if k.strip() == "home"),
        "",
    )
    if not home:
        return "its environment's pyvenv.cfg names no interpreter"
    prefix = os.path.realpath(os.path.join(home, os.pardir))
    interpreter = os.path.realpath(target / "bin" / "python")
    if not os.path.isfile(interpreter) or not interpreter.startswith(prefix.rstrip("/") + "/"):
        return f"its environment's python ({interpreter}) is not inside {prefix}"
    owner_home = os.path.realpath(Path.home())
    if prefix in ("/", "/Users", "/Volumes") or (owner_home + "/").startswith(
        prefix.rstrip("/") + "/"
    ):
        return f"its environment names {prefix} as its interpreter's installation"
    return Environment(dir=target, interpreter=interpreter, prefix=prefix)
