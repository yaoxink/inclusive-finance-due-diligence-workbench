from due_app.recordings import validated_punctuation


def test_only_complete_matching_stable_text_can_be_used():
    chunks = [{'text': '今年种了', 'stable': True}, {'text': '三十亩', 'stable': True}]
    valid = {'final_text': '今年种了，三十亩。'}
    assert validated_punctuation(valid, chunks, 'completed', 32000, 32000) == valid['final_text']
    assert validated_punctuation({'final_text': '今年种了，四十亩。'}, chunks, 'completed', 32000, 32000) is None
    assert validated_punctuation(valid, chunks, 'running', 32000, 32000) is None
    assert validated_punctuation(valid, chunks, 'completed', 16000, 32000) is None
    assert validated_punctuation(valid, [{**chunks[0], 'stable': False}, chunks[1]], 'completed', 32000, 32000) is None
