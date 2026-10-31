"""Run the CLI on tracked and new source files without modifying Git's index."""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

EXCLUDED = {'.git', '.source', '.aidp', '.codex', '.oci', '.aws', '.tmp',
            'node_modules', 'dist', '.venv', '.upstream', 'graphify-out'}


def snapshot(root: Path, destination: Path) -> int:
    result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                            cwd=root, check=True, capture_output=True)
    names = set(filter(None, result.stdout.decode('utf-8').split('\0')))
    # The baseline/rules remain inputs even when an operator has not added them to Git.
    names.update(name for name in ('.sentrux/baseline.json', '.sentrux/rules.toml')
                 if (root / name).exists())
    count = 0
    for name in sorted(names):
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name:
            raise ValueError('Source path leaves the repository')
        if EXCLUDED.intersection(relative.parts):
            continue
        source = root.joinpath(*relative.parts)
        resolved = source.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise ValueError('Source symlink leaves the repository')
        if EXCLUDED.intersection(resolved.relative_to(root.resolve()).parts):
            continue
        if not source.is_file():
            continue
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        count += 1
    return count


def run(root: Path, command: str, save: bool = False) -> int:
    scanner = shutil.which('sentrux')
    if scanner is None:
        raise RuntimeError('Sentrux CLI is required')
    baseline = root / '.sentrux/baseline.json'
    if save and (command != 'gate' or baseline.exists()):
        raise ValueError('Initialize only a missing baseline; existing references are preserved')
    with TemporaryDirectory(prefix='aidp-sentrux-') as temporary:
        directory = Path(temporary)
        count = snapshot(root, directory)
        print(f'Sentrux source scope: {count} files, including non-ignored new files.', flush=True)
        args = [scanner, command, *(['--save'] if save else []), str(directory)]
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace')
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
        if result.returncode or 'DEGRADED' in result.stdout:
            return result.returncode or 1
        if save:
            baseline.parent.mkdir(exist_ok=True)
            with baseline.open('xb') as output:
                output.write((directory / '.sentrux/baseline.json').read_bytes())
    return 0


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['gate', 'check'])
    parser.add_argument('--save', action='store_true')
    args = parser.parse_args()
    return run(Path(__file__).resolve().parents[1], args.command, args.save)


if __name__ == '__main__':
    raise SystemExit(main())
