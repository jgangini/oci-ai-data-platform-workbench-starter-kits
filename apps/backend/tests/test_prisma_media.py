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
