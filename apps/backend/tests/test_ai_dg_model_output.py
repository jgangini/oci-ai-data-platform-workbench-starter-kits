import pytest

from app.ai_data_governance.model_output import model_json
from app.ai_data_governance.prompts import documentary_prompts


def test_only_one_bounded_json_object_is_accepted():
    assert model_json('{"result":"ok"}') == {"result": "ok"}
    assert model_json([{"type": "text", "text": '{"result":'}, {"type": "text", "text": '"ok"}'}]) == {"result": "ok"}


@pytest.mark.parametrize("content", [
    '```json\n{"result":"ok"}\n```', '{"result":1,"result":2}', '{"value":NaN}',
    '{"value":Infinity}', '[]', '{"result":1} extra', None,
    [{"type": "reasoning", "text": "must not become an answer"}],
])
def test_malformed_duplicate_nonfinite_and_nontext_results_are_rejected(content):
    with pytest.raises((ValueError, TypeError)):
        model_json(content)


def test_utf8_byte_limit_includes_multibyte_text():
    with pytest.raises(ValueError, match="bound"):
        model_json('{"text":"ááá"}', max_bytes=10)


def test_documentary_prompts_load_the_versioned_source_without_a_second_definition():
    prompts = documentary_prompts()
    assert set(prompts) == {"D0", "D1", "D2"}
    assert "Preserva cualquier descripción no vacía" in prompts["D0"]
    assert "DocumentReviews" in prompts["D2"]
