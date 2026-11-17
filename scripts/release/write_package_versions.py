"""Bake verified Git dates into the release image; runtime never needs Git."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'apps/backend'))
from app.packages.catalog import package_catalog


def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def version_metadata():
    commit = git('rev-parse', 'HEAD')
    entries = {}
    paths = [path for path in git('ls-tree', '-r', '--name-only', commit, '--', 'apps/backend/app').splitlines()
             if path.endswith('/lab.json')]
    recorded = []
    for path in paths:
        try:
            value = json.loads(git('show', commit + ':' + path))
        except (subprocess.CalledProcessError, ValueError):
            continue
        if isinstance(value, dict):
            recorded.append((path, value))
    for pack in package_catalog():
        matches = [(path, value) for path, value in recorded
                   if value.get('lab_id') == pack.lab_id and value.get('pack_sha256') == pack.pack_sha256]
        if len(matches) != 1:
            continue
        path, _ = matches[0]
        entries[pack.lab_id] = {'pack_version': pack.pack_version, 'pack_sha256': pack.pack_sha256,
                               'updated_at': git('log', '-1', '--follow', '--diff-filter=AM', '--format=%cI', commit, '--', path)}
    return {'schema_version': 1, 'commit_sha': commit, 'packages': entries}


if __name__ == '__main__':
    target = ROOT / 'apps/backend/app/packages/package-releases.json'
    target.write_text(json.dumps(version_metadata(), indent=2) + '\n', encoding='utf-8')
    print('Prepared commit-bound package version dates.')
