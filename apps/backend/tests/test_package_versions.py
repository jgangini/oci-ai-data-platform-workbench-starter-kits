import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from app.package_versions import package_version_dates


PACK = SimpleNamespace(lab_id='retail', pack_version='2.0.0', pack_sha256='a' * 64)
COMMIT = 'b' * 40


def metadata():
    return {'schema_version': 1, 'commit_sha': COMMIT, 'packages': {
        PACK.lab_id: {'pack_version': PACK.pack_version, 'pack_sha256': PACK.pack_sha256,
                      'updated_at': '2026-10-30T20:00:00-05:00'}}}


def test_version_date_requires_the_exact_image_and_package(tmp_path):
    path = tmp_path / 'releases.json'
    path.write_text(json.dumps(metadata()), encoding='utf-8')
    assert package_version_dates(COMMIT, [PACK], path) == {'retail': '2026-10-31T01:00:00+00:00'}
    assert package_version_dates('another-image', [PACK], path) == {}
    for change in ({'pack_version': '3.0.0'}, {'pack_sha256': 'c' * 64}):
        candidate = SimpleNamespace(**(vars(PACK) | change))
        assert package_version_dates(COMMIT, [candidate], path) == {}


@pytest.mark.parametrize('value', [None, 'invalid', '2026-10-31T01:00:00', '0001-01-01T00:00:00+23:00'])
def test_unusable_dates_do_not_hide_other_verified_packages(tmp_path, value):
    content = metadata()
    content['packages']['banking'] = dict(content['packages']['retail'], updated_at=value)
    path = tmp_path / 'releases.json'
    path.write_text(json.dumps(content), encoding='utf-8')
    banking = SimpleNamespace(**(vars(PACK) | {'lab_id': 'banking'}))
    assert package_version_dates(COMMIT, [banking, PACK], path) == {'retail': '2026-10-31T01:00:00+00:00'}


@pytest.mark.parametrize('content', ['{', '[]', '{"schema_version": 2}', 'x' * 131073],
                         ids=['malformed', 'wrong-type', 'wrong-schema', 'oversized'])
def test_unknown_metadata_is_not_replaced_with_filesystem_dates(tmp_path, content):
    path = tmp_path / 'releases.json'
    assert package_version_dates(COMMIT, [PACK], path) == {}
    path.write_text(content, encoding='utf-8')
    assert package_version_dates(COMMIT, [PACK], path) == {}


def test_release_build_uses_package_history_not_the_latest_application_commit(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location('write_package_versions', root / 'scripts/write_package_versions.py')
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    monkeypatch.setattr(writer, 'ROOT', tmp_path)
    monkeypatch.setattr(writer, 'lab_catalog', lambda: [PACK])

    def git(*args, date='2026-10-20T10:00:00+00:00'):
        env = dict(os.environ, GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.test',
                   GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.test',
                   GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        return subprocess.run(['git', '-c', 'core.hooksPath=', *args], cwd=tmp_path, env=env,
                              capture_output=True, text=True, check=True).stdout.strip()

    git('init')
    path = tmp_path / 'apps/backend/app/labs/retail/lab.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'pack_sha256': PACK.pack_sha256}), encoding='utf-8')
    git('add', '.')
    git('-c', 'commit.gpgsign=false', 'commit', '-m', 'package')
    (tmp_path / 'app.txt').write_text('new application without package changes', encoding='utf-8')
    git('add', '.')
    git('-c', 'commit.gpgsign=false', 'commit', '-m', 'application', date='2026-10-31T10:00:00+00:00')
    result = writer.version_metadata()
    assert result['commit_sha'] == git('rev-parse', 'HEAD')
    assert datetime.fromisoformat(result['packages']['retail']['updated_at'].replace('Z', '+00:00')) == datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(writer, 'lab_catalog', lambda: [SimpleNamespace(**(vars(PACK) | {'pack_sha256': 'c' * 64}))])
    assert writer.version_metadata()['packages'] == {}
