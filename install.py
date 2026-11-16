#!/usr/bin/env python3
"""Install the accessing-blocked-sites skill into an agent harness.

    python install.py                      # auto-detect harness
    python install.py --target claude      # dsh | claude | cursor | agents
    python install.py --dir /path/to/skills
    python install.py --no-venv --dry-run

The skill is self-contained: one SKILL.md and three scripts. The installer
copies it, provisions a `.venv` with Playwright next to the scripts, and
prints the commands to verify it. It never touches a real browser profile.

Standard library only. Network access only for the optional Playwright install.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILL = "accessing-blocked-sites"
SKILL_SRC = ROOT / "skills" / SKILL

TARGETS = {
    "dsh": Path.home() / ".dsh" / "skills",
    "claude": Path.home() / ".claude" / "skills",
    "cursor": Path.home() / ".cursor" / "skills",
    "agents": Path.home() / ".agents" / "skills",
}

IGNORE = shutil.ignore_patterns(".venv", "__pycache__", "*.pyc", "*.pyo", ".DS_Store", ".install.json")


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def log(message: str) -> None:
    print(message, flush=True)


def detect_target() -> str | None:
    for name in ("dsh", "claude", "cursor", "agents"):
        if TARGETS[name].parent.exists():
            return name
    return None


def resolve_target(target: str, explicit_dir: str | None) -> tuple[Path, str]:
    if explicit_dir:
        return Path(explicit_dir).expanduser().resolve(), explicit_dir
    name = detect_target() if target == "auto" else target
    if not name:
        die("no harness found in ~/.dsh, ~/.claude, ~/.cursor or ~/.agents.\n"
            "  Pass --target <name> or --dir <skills directory>.")
    if name not in TARGETS:
        die(f"unknown target {name!r}; choose from {', '.join(TARGETS)}")
    return TARGETS[name], name


MARKER = ".install.json"


def already_ours(dest: Path) -> bool:
    return (dest / MARKER).is_file()


def stamp(dest: Path, origin: str, skill: str) -> None:
    import datetime
    meta = {"skill": skill, "origin": origin,
            "installed_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    (dest / MARKER).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def py_exe(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def has_module(python: Path, module: str) -> bool:
    try:
        return subprocess.run([str(python), "-c", f"import {module}"],
                              capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


def playwright_version(python: Path) -> str:
    try:
        out = subprocess.run(
            [str(python), "-c",
             "import importlib.metadata as m; print(m.version('playwright'))"],
            capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def setup_venv(skill_dir: Path, dry_run: bool) -> None:
    venv = skill_dir / ".venv"
    python = py_exe(venv)
    if venv.exists() and python.exists() and has_module(python, "playwright"):
        log(f"  python        {python} (playwright {playwright_version(python)})")
        return
    if dry_run:
        log(f"  python        would create {venv} and install playwright")
        return

    uv = shutil.which("uv")
    try:
        if uv:
            subprocess.run([uv, "venv", "--python", "3.12", str(venv)],
                           check=True, capture_output=True, timeout=300)
            subprocess.run([uv, "pip", "install", "--python", str(python), "playwright"],
                           check=True, capture_output=True, timeout=600)
        else:
            subprocess.run([sys.executable, "-m", "venv", str(venv)],
                           check=True, capture_output=True, timeout=300)
            subprocess.run([str(python), "-m", "pip", "install", "--quiet", "playwright"],
                           check=True, capture_output=True, timeout=600)
    except Exception as exc:
        log(f"  python        FAILED to provision Playwright: {exc}")
        log(f"                install it yourself: {sys.executable} -m pip install playwright")
        return

    if has_module(python, "playwright"):
        log(f"  python        {python} (playwright {playwright_version(python)})")
    else:
        log("  python        Playwright installed but not importable; check the venv")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", default="auto", choices=("auto",) + tuple(TARGETS))
    parser.add_argument("--dir", default=None, help="explicit skills directory")
    parser.add_argument("--force", action="store_true", help="replace an existing copy not created by this installer")
    parser.add_argument("--no-venv", action="store_true", help="skip the Playwright venv")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not SKILL_SRC.is_dir():
        die(f"missing skill source: {SKILL_SRC}")

    target_root, target_name = resolve_target(args.target, args.dir)
    dest = target_root / SKILL

    log(f"source   {SKILL_SRC}")
    log(f"target   {target_root}  ({target_name})")
    if args.dry_run:
        log(f"  would install  {SKILL}")
    else:
        if dest.exists() and not already_ours(dest) and not args.force:
            die(f"{dest} exists and was not installed by this installer.\n"
                "  Move it away, or re-run with --force to replace it.")
        target_root.mkdir(parents=True, exist_ok=True)
        existed = dest.exists()
        shutil.copytree(SKILL_SRC, dest, dirs_exist_ok=True, ignore=IGNORE)
        stamp(dest, str(ROOT), SKILL)
        log(f"  {'updated' if existed else 'installed'}  {dest}")

    if not args.no_venv:
        log("python")
        setup_venv(dest, args.dry_run)

    log("")
    log("next")
    log(f"  health check   python {dest}/scripts/check_egress.py")
    log(f"  one page       python {dest}/scripts/fetch_page.py <url> out --headed")
    log(f"  a list         python {dest}/scripts/batch_fetch.py urls.txt -j 3")
    log("  restart the agent so it picks up the new skill")
    return 0


if __name__ == "__main__":
    sys.exit(main())
