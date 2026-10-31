"""Version dates from the immutable image's commit-bound package metadata."""
import json
from datetime import datetime, timezone
from pathlib import Path


METADATA = Path(__file__).with_name('labs') / 'package-releases.json'


def package_version_dates(commit_sha, packs, path=METADATA):
    try:
        if path.stat().st_size > 131072:
            return {}
        metadata = json.loads(path.read_text(encoding='utf-8'))
        if metadata.get('schema_version') != 1 or metadata.get('commit_sha') != commit_sha:
            return {}
        entries = metadata['packages']
        result = {}
        for pack in packs:
            entry = entries.get(pack.lab_id, {})
            if (entry.get('pack_version'), entry.get('pack_sha256')) != (pack.pack_version, pack.pack_sha256):
                continue
            value = entry.get('updated_at')
            if not isinstance(value, str):
                continue
            try:
                stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                continue
            if stamp.tzinfo is not None:
                try:
                    result[pack.lab_id] = stamp.astimezone(timezone.utc).isoformat()
                except OverflowError:
                    continue
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}
