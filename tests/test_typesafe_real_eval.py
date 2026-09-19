"""Offline invariants for the real-meeting TypeSafe experiments."""
from pathlib import Path

from benchmarks.meeting_mode.typesafe_real_eval import advice, save, selected_signal, signal_payload
from benchmarks.meeting_mode.typesafe_real_writer import check_payload, make_state


def segment(i, start=None, end=None):
    return {'id':f'sg_{i}','start_s':i*5 if start is None else start,
            'end_s':i*5+4 if end is None else end,'text':f'utterance {i}'}


def test_signal_context_excludes_future_and_old_evidence():
    rows=[segment(0,0,5),segment(1,100,110),segment(2,200,210),segment(3,150,160)]
    payload=signal_payload(rows,3)
    assert payload['state']['prior_context']==[rows[1]]
    assert payload['state']['focus']==rows[3]
    assert 'reference' not in payload['state']


def test_signal_context_has_a_bounded_size():
    rows=[segment(i,i,i+.5) for i in range(100)]
    state=signal_payload(rows,99)['state']
    assert len(state['prior_context'])==20
    assert state['prior_context'][0]['id']=='sg_79'


def test_uncertain_commitment_does_not_pass_the_frozen_threshold():
    run={'segment_id':'sg_1','response':{'answers':{
        'commitment':{'choice':'accepted','confidence':.79},
        'assumption_broken':{'noul':.1},'actionable':{'noul':.1},'decision':{'noul':.1}}}}
    assert not selected_signal(run)
    assert advice([run,{'error':'HTTPStatusError'}])==[]
    run['response']['answers']['commitment']['confidence']=.8
    assert selected_signal(run)
    assert advice([run])[0]['segment_id']=='sg_1'


def test_verifier_rejects_missing_and_unknown_evidence_locally():
    by_id={'sg_1':segment(1)}
    assert check_payload({'evidence':[]},by_id) is None
    assert check_payload({'evidence':['sg_unknown']},by_id) is None
    item={'text':'a claim','card':'key_points','evidence':['sg_1']}
    payload=check_payload(item,by_id)
    assert payload['state']['cited_segments']==[by_id['sg_1']]


def test_prototype_state_preserves_suggestion_status_and_evidence():
    draft={'topic':'t','summary':'s','items':[{'card':'key_points','text':'Maybe try X',
                                           'evidence':['sg_1'],'commitment_status':'suggested'}]}
    state=make_state('IN1009',draft)
    item=state['cards']['key_points'][0]
    assert item['evidence']==['sg_1']
    assert item['data']['commitment_status']=='suggested'
    assert state['cards']['action_items']==[]


def test_atomic_save_retries_transient_windows_read_lock(tmp_path,monkeypatch):
    target=tmp_path/'result.json'
    target.write_text('{"old":true}',encoding='utf-8')
    original=Path.replace
    attempts=[]
    def transient(self,path):
        attempts.append(1)
        if len(attempts)==1:
            assert target.read_text()=='{"old":true}'
            raise PermissionError('transient read lock')
        return original(self,path)
    monkeypatch.setattr(Path,'replace',transient)
    monkeypatch.setattr('benchmarks.meeting_mode.typesafe_real_eval.time.sleep',lambda _:None)
    save(target,{'complete':True})
    assert len(attempts)==2
    assert '"complete": true' in target.read_text()
    assert not target.with_suffix('.json.partial').exists()


def test_audit_validator_accepts_complete_nested_labels():
    from benchmarks.meeting_mode.typesafe_real_audit import schema, validate_schema
    shape = schema({'labels': schema({'case': schema({
        'verdict': {'type': 'string', 'enum': ['supported', 'unclear']},
        'accepted': {'type': 'boolean'},
    })})})
    validate_schema({'labels': {'case': {'verdict': 'supported', 'accepted': False}}}, shape)


def test_audit_validator_rejects_empty_case_instead_of_scoring_it():
    import pytest
    from benchmarks.meeting_mode.typesafe_real_audit import schema, validate_schema
    shape = schema({'labels': schema({'case': schema({
        'verdict': {'type': 'string', 'enum': ['supported', 'unclear']},
    })})})
    with pytest.raises(ValueError, match='Missing'):
        validate_schema({'labels': {'case': {}}}, shape)


def test_audit_validator_rejects_boolean_as_integer_rating():
    import pytest
    from benchmarks.meeting_mode.typesafe_real_audit import validate_schema
    with pytest.raises(ValueError, match='integer'):
        validate_schema(True, {'type': 'integer', 'enum': [1, 2, 3, 4, 5]})
