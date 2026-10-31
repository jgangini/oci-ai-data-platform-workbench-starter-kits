"""Bake verified Git dates into the release image; runtime never needs Git."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps/backend'))
from app.lab_packs import lab_catalog


def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def version_metadata():
    commit = git('rev-parse', 'HEAD')
    entries = {}
    for pack in lab_catalog():
        path = 'apps/backend/app/labs/' + pack.lab_id + '/lab.json'
        try:
            recorded = json.loads(git('show', commit + ':' + path))
        except (subprocess.CalledProcessError, ValueError):
            continue
        if recorded.get('pack_sha256') != pack.pack_sha256:
            continue
        entries[pack.lab_id] = {'pack_version': pack.pack_version, 'pack_sha256': pack.pack_sha256,
                               'updated_at': git('log', '-1', '--format=%cI', commit, '--', path)}
    return {'schema_version': 1, 'commit_sha': commit, 'packages': entries}


if __name__ == '__main__':
    target = ROOT / 'apps/backend/app/labs/package-releases.json'
    target.write_text(json.dumps(version_metadata(), indent=2) + '\n', encoding='utf-8')
    print('Prepared commit-bound package version dates.')
