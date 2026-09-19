"""Verify benchmark accounting and label isolation without network requests."""

import copy
import json

import httpx
import pytest

from benchmarks.typesafe_batch_probe import build_batch
from benchmarks.typesafe_cases import cases
from benchmarks.typesafe_experiments import (
    QUESTIONS,
    build_request,
    evaluate,
    summarize,
    validate_response,
)


def response(choice="supported", noul=0.99):
    options = QUESTIONS["citation"]["relation"]["criteria"]
    return {"model": "fixture", "usage": {"input_tokens": 10, "output_tokens": 2},
            "answers": {"relation": {"type": "choice", "choice": choice, "confidence": 1.0,
                                     "probabilities": {key: float(key == choice) for key in options}},
                        "acceptable": {"type": "noul", "noul": noul}}}


def test_expected_labels_and_splits_never_reach_api():
    row = copy.deepcopy(cases()[0])
    row["expected"] = "SECRET_LABEL_SENTINEL"
    row["split"] = "SECRET_SPLIT_SENTINEL"
    for request in (build_request(row), build_batch([row])):
        encoded = json.dumps(request)
        assert "SECRET_LABEL_SENTINEL" not in encoded
        assert "SECRET_SPLIT_SENTINEL" not in encoded


@pytest.mark.parametrize("mutation", ["missing", "unknown", "probability", "noul"])
def test_malformed_response_is_not_scored_as_a_success(mutation):
    body = response()
    if mutation == "missing":
        del body["answers"]["acceptable"]
    elif mutation == "unknown":
        body["answers"]["relation"]["choice"] = "invented"
    elif mutation == "probability":
        body["answers"]["relation"]["probabilities"]["supported"] = 0.2
    else:
        body["answers"]["acceptable"]["noul"] = float("nan")
    with pytest.raises((KeyError, AssertionError)):
        validate_response(body, QUESTIONS["citation"])


def test_false_accept_and_service_failure_are_visible():
    row = cases()[4]
    result = {**row, "baseline": "supported", "seconds": 0.2, "response": response()}
    failed = {**cases()[0], "error": "TimeoutException"}
    stats = summarize([result, failed])["citation"]
    assert stats["requested"] == 2 and stats["completed"] == 1 and stats["errors"] == 1
    assert stats["false_accepts"] == 1 and stats["choice_correct"] == 0


def test_http_failure_does_not_record_key_or_exception_text():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer secret-sentinel"
        return httpx.Response(401, json={"error": "secret-sentinel"})
    with httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer secret-sentinel"}) as client:
        result = evaluate(client, cases()[0], "fixture", 0)
    assert result["error"] == "HTTPStatusError"
    assert result["attempts"] == [{"status": 401}]
    assert "secret-sentinel" not in json.dumps(result)


def test_existing_evidence_check_accepts_all_citation_fixtures():
    from meeting.state.patches import OpContext, _check_evidence

    ctx = OpContext("agent", "probe", lambda segment_id: segment_id == "sg_probe")
    for row in cases():
        if row["experiment"] == "citation":
            assert _check_evidence({"evidence": row["state"]["evidence"]}, ctx) is None
