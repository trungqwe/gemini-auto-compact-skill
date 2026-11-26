#!/usr/bin/env python3
"""Safe installer for the auto-compact skill (no recursive deletes, atomic file copies)."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from pathlib import Path

FORBIDDEN_FILENAMES = {"README.md", "CHANGELOG.md", ".DS_Store", "Thumbs.db"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo"}
FORBIDDEN_DIRS = {"__pycache__", ".pytest_cache", ".git"}


def _reconfigure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except OSError:
                pass


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.",
        suffix=".tmp",
        dir=str(dest.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, dest)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def collect_skill_files(source_dir: Path) -> list[Path]:
    """Validate source skill tree and return relative paths of allowed files."""
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Skill source directory not found: {source_dir}")
    if not (source_dir / "SKILL.md").is_file():
        raise FileNotFoundError(f"Missing SKILL.md in {source_dir}")

    files: list[Path] = []
    for root, dirs, filenames in os.walk(source_dir):
        root_path = Path(root)
        # Skip cache directories instead of copying them
        dirs[:] = sorted(d for d in dirs if d not in FORBIDDEN_DIRS)
        for fname in sorted(filenames):
            if fname in FORBIDDEN_FILENAMES:
                raise ValueError(
                    f"Forbidden file inside skill directory (violates skill-creator rules): "
                    f"{root_path / fname}"
                )
            fpath = root_path / fname
            if fpath.suffix.lower() in FORBIDDEN_SUFFIXES:
                continue
            files.append(fpath.relative_to(source_dir))
    return sorted(files)


def scan_forbidden_in_target(target_dir: Path) -> list[str]:
    """Return list of forbidden files/directories found inside an installed target."""
    if not target_dir.exists():
        return []
    violations: list[str] = []
    for root, dirs, filenames in os.walk(target_dir):
        root_path = Path(root)
        for d in dirs:
            if d in FORBIDDEN_DIRS:
                violations.append(str(root_path / d))
        for fname in filenames:
            fpath = root_path / fname
            if fname in FORBIDDEN_FILENAMES or fpath.suffix.lower() in FORBIDDEN_SUFFIXES:
                violations.append(str(fpath))
    return sorted(violations)


def verify_target(source_dir: Path, target_dir: Path) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not target_dir.is_dir():
        return False, [f"Target directory does not exist: {target_dir}"]

    forbidden = scan_forbidden_in_target(target_dir)
    for item in forbidden:
        errors.append(f"Forbidden artifact in target: {item}")

    rel_files = collect_skill_files(source_dir)
    for rel in rel_files:
        src = source_dir / rel
        dst = target_dir / rel
        if not dst.is_file():
            errors.append(f"Missing installed file: {dst}")
            continue
        if _sha256_file(src) != _sha256_file(dst):
            errors.append(f"SHA-256 mismatch for installed file: {dst}")

    return len(errors) == 0, errors


def install_skill(
    source_dir: Path,
    target_dir: Path,
    dry_run: bool = False,
) -> list[Path]:
    rel_files = collect_skill_files(source_dir)
    forbidden = scan_forbidden_in_target(target_dir)
    if forbidden:
        raise ValueError(
            "Target directory already contains forbidden files/dirs "
            f"(move them manually to safe staging first): {', '.join(forbidden)}"
        )

    copied: list[Path] = []
    for rel in rel_files:
        src = source_dir / rel
        dst = target_dir / rel
        if not dry_run:
            _atomic_write_bytes(dst, src.read_bytes())
        copied.append(dst)

    if not dry_run:
        ok, errors = verify_target(source_dir, target_dir)
        if not ok:
            raise RuntimeError("Post-install verification failed: " + "; ".join(errors))

    return copied


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely install skills/auto-compact into ~/.gemini/config/skills/auto-compact."
    )
    parser.add_argument(
        "--target-dir",
        default=None,
        help="Explicit destination directory (overrides default ~/.gemini/config/skills/auto-compact).",
    )
    parser.add_argument(
        "--also-agents",
        action="store_true",
        help="Also install to ~/.agents/skills/auto-compact (or --agents-dir).",
    )
    parser.add_argument(
        "--agents-dir",
        default=None,
        help="Optional agents skills base directory (default: ~/.agents/skills); installs skill there when provided or with --also-agents.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and list planned copies without writing to disk.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify destination(s) match source SHA-256 and contain no forbidden files.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent
    source_dir = repo_root / "skills" / "auto-compact"

    targets: list[Path] = []
    if args.target_dir:
        targets.append(Path(args.target_dir).expanduser().resolve())
    else:
        targets.append((Path.home() / ".gemini" / "config" / "skills" / "auto-compact").resolve())

    if args.also_agents or args.agents_dir is not None:
        agents_base = (
            Path(args.agents_dir).expanduser().resolve()
            if args.agents_dir is not None
            else (Path.home() / ".agents" / "skills").resolve()
        )
        agents_target = (
            agents_base if agents_base.name == "auto-compact" else (agents_base / "auto-compact")
        )
        if agents_target not in targets:
            targets.append(agents_target)

    try:
        if args.verify:
            all_ok = True
            for target in targets:
                ok, errors = verify_target(source_dir, target)
                if ok:
                    print(f"[PASS] Verified: {target}")
                else:
                    all_ok = False
                    print(f"[FAIL] {target}", file=sys.stderr)
                    for err in errors:
                        print(f"  - {err}", file=sys.stderr)
            return 0 if all_ok else 2

        for target in targets:
            copied = install_skill(source_dir, target, dry_run=args.dry_run)
            mode = "DRY-RUN" if args.dry_run else "INSTALLED"
            print(f"[{mode}] {target} ({len(copied)} files)")
            for item in copied:
                print(f"  - {item}")
        return 0
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
