"""Protect benchmark labels, routing and production-baseline semantics."""
import copy
import json
import pytest
from benchmarks.typesafe_comparison import public_cases, response_schema, typesafe_payload, validate_choices, deterministic, summarize
from benchmarks.typesafe_comparison_cases import comparison_cases
from benchmarks.typesafe_hybrid import should_escalate


def test_api_payloads_and_schema_exclude_expected_labels():
    row = copy.deepcopy(comparison_cases()[0])
    row['expected'] = {'answer': 'SECRET_GOLD_SENTINEL'}
    row['family'] = 'SECRET_FAMILY_SENTINEL'
    for payload in (public_cases([row]), typesafe_payload([row]), response_schema([row])):
        assert 'SECRET_GOLD_SENTINEL' not in json.dumps(payload)
        assert 'SECRET_FAMILY_SENTINEL' not in json.dumps(payload)


def test_batch_questions_bind_to_their_own_state():
    rows = comparison_cases()[:2]
    payload = typesafe_payload(rows)
    for index, row in enumerate(rows):
        instruction = payload['questions'][row['id']+'__answer']['instructions']
        assert f'cases[{index}].state' in instruction


def test_unconsumed_owner_uncertainty_does_not_trigger_escalation():
    row = next(r for r in comparison_cases() if r['family'] == 'event')
    answers = {'answer': {'choice': 'none', 'confidence': .99}, 'owner': {'confidence': .01}, 'deadline': {'confidence': .01}}
    assert not should_escalate(row, answers)
    answers['answer']['choice'] = 'action'
    assert should_escalate(row, answers)


def test_missing_chat_field_is_rejected_instead_of_silently_scored():
    row = comparison_cases()[0]
    with pytest.raises(KeyError):
        validate_choices([row], {row['id']: {'question_name': 'supported'}})


def test_schema_uses_actual_question_names_and_disallows_extra_fields():
    row = next(r for r in comparison_cases() if r['family'] == 'event')
    schema = response_schema([row])['json_schema']['schema']
    spec = schema['properties']['answers']['properties'][row['id']]
    assert set(spec['required']) == {'answer','owner','deadline'}
    assert not spec['additionalProperties']
    assert set(spec['properties']['answer']['enum']) == {'action','decision','question','none'}


def test_failed_batch_remains_in_accuracy_denominator():
    row = comparison_cases()[0]
    result = summarize([{**row, 'error': 'timeout'}])['citation']
    assert result['cases'] == 1 and result['primary_correct'] == 0 and result['completed'] == 0


def test_keyword_baseline_calls_production_duplicate_function(monkeypatch):
    from meeting.state import patches
    seen = []
    monkeypatch.setattr(patches, '_item_text_too_similar', lambda a,b: seen.append((a,b)) or True)
    row = next(r for r in comparison_cases() if r['family'] == 'dedup')
    assert deterministic(row) == 'duplicate'
    assert seen == [(row['state']['left'],row['state']['right'])]
