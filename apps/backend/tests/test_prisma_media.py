import httpx

from app.prisma.core import build_snapshot, normalize_event, simulation_events
from app.prisma.media import photos
from app.prisma.x import fetch_page


def test_x_photos_are_expanded_and_unsafe_urls_are_discarded():
    observed = []
    def respond(request):
        observed.append(request)
        return httpx.Response(200, json={
            'data': [{'id': '123', 'text': 'Inundación en Kennedy, Bogotá', 'created_at': '2026-10-05T14:00:00Z',
                      'attachments': {'media_keys': ['photo', 'unsafe', 'video']}}],
            'includes': {'media': [
                {'media_key': 'photo', 'type': 'photo', 'url': 'https://pbs.twimg.com/media/example.jpg', 'alt_text': 'Vía anegada'},
                {'media_key': 'unsafe', 'type': 'photo', 'url': 'https://pbs.twimg.com.evil.test/media/a.jpg'},
                {'media_key': 'video', 'type': 'video', 'url': 'https://pbs.twimg.com/media/video.mp4'},
            ]}, 'meta': {'newest_id': '123'}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        events, cursor = fetch_page(client, 'test-only', '#Bogota', {}, 1791208800)
    event = normalize_event(events[0])
    assert event['media'] == [{'type': 'photo', 'url': 'https://pbs.twimg.com/media/example.jpg', 'alt_text': 'Vía anegada'}]
    assert 'attachments.media_keys' in observed[0].url.params['expansions']
    assert cursor['since_id'] == '123'
    for url in ('http://pbs.twimg.com/media/a', 'https://secret@pbs.twimg.com/media/a',
                'https://127.0.0.1/media/a', 'javascript:alert(1)', 'https://pbs.twimg.com:8443/media/a'):
        assert photos('x', [{'type': 'photo', 'url': url}]) == []


def test_reposts_and_one_author_do_not_raise_corroboration_or_confirm_reality():
    seed = simulation_events(0)[0]
    original = normalize_event({**seed, 'raw_metadata': {'author_id': 'alice'}})
    copied = normalize_event({**seed, 'platform': 'facebook', 'source_id': 'copy',
                             'text': seed['text'] + '!', 'raw_metadata': {'author_id': 'bob'}})
    same_author = normalize_event({**seed, 'source_id': 'followup', 'text': 'Varios autos varados por el agua en Kennedy',
                                  'raw_metadata': {'author_id': 'alice'}})
    snapshot = build_snapshot([original, copied, same_author], {}, 'v1', 'now')
    incident = snapshot['incidents'][0]
    assert incident['independent_source_count'] == 1 and incident['corroboration_score'] == 0
    assert incident['mode'] == 'simulation' and incident['review_status'] == 'pending'
    independent = normalize_event({**seed, 'platform': 'sensor', 'source_id': 'river',
                                   'text': 'Nivel del río en ascenso, inundación en Kennedy',
                                   'raw_metadata': {'author_id': 'station-1'}})
    incident = build_snapshot([original, copied, independent], {}, 'v2', 'now')['incidents'][0]
    assert incident['independent_source_count'] == 2 and incident['corroboration_score'] == 25
    assert incident['corroboration_status'] == 'multiple_sources'
    assert incident['review_status'] == 'pending' and incident['mode'] == 'simulation'


def test_continuous_sources_correlate_without_counting_copies_and_replays_stay_isolated():
    fixtures = {event['platform']: event for event in simulation_events(60)}
    events = [normalize_event({**fixtures[platform], 'raw_metadata': {**fixtures[platform]['raw_metadata'],
        'capture_run_id': platform + '-capture', 'scenario_run_id': platform + '-capture:0'}})
        for platform in ('x', 'facebook', 'sensor')]
    copied = build_snapshot(events[:2], {}, 'copies', 'now')
    assert len(copied['incidents']) == 1
    assert copied['incidents'][0]['independent_source_count'] == 1
    combined = build_snapshot(events, {}, 'continuous', 'now')
    assert len(combined['incidents']) == 1
    incident = combined['incidents'][0]
    assert incident['independent_source_count'] == 2 and incident['corroboration_score'] == 25
    assert set(incident['evidence_ids']) == {event['id'] for event in events}
    assert incident['mode'] == 'simulation' and incident['review_status'] == 'pending'
    assert len({event['raw_metadata']['capture_run_id'] for event in combined['evidence']}) == 3
    replays = [{**event, 'raw_metadata': {key: value for key, value in event['raw_metadata'].items()
        if key != 'capture_run_id'}} for event in events]
    assert len(build_snapshot(replays, {}, 'replays', 'now')['incidents']) == 3
