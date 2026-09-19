"""Offline correctness checks for chronological live-state probe inputs."""
import json
import pytest
from benchmarks.meeting_mode.typesafe_live_state_probe import apply_prediction, request_for


def inputs():
    episode={'candidate':'Share the papers','criteria':{'absent':'No task','accepted':'Accepted task'}}
    step={'at_s':20,'new_speech':[{'id':'new','end_s':20,'text':'I will send them'}],
          'prior_context':[],'expected_statuses':['accepted']}
    return episode,step


def test_request_excludes_expected_labels_and_freezes_memory():
    episode,step=inputs()
    memory=[{'id':'old','end_s':10,'text':'Maybe share them'}]
    payload=request_for(episode,step,'proposed',memory)
    assert 'expected_statuses' not in json.dumps(payload)
    memory[0]['text']='Later correction'
    assert payload['state']['memory_evidence'][0]['text']=='Maybe share them'


def test_request_rejects_future_memory():
    episode,step=inputs()
    with pytest.raises(AssertionError):
        request_for(episode,step,'absent',[{'end_s':21,'text':'Future words'}])


def test_confirmed_lane_abstains_without_promoting_low_confidence():
    prediction={'choice':'accepted','confidence':.79}
    assert apply_prediction('proposed',prediction,'jev-confirmed')=='proposed'
    assert apply_prediction('proposed',prediction,'jev-provisional')=='accepted'
    prediction['confidence']=.8
    assert apply_prediction('proposed',prediction,'jev-confirmed')=='accepted'
