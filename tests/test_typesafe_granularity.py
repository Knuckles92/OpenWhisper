"""Verify the granularity experiment preserves question/evidence bindings."""

from benchmarks.typesafe_granularity_probe import requests_for, summarize


def fixture_request():
    return {
        "state": {"checks": [{"claim": {"text": f"claim {i}"}, "cited": [{"id": f"sg_{i}"}]} for i in range(10)]},
        "questions": {f"c{i}_{field}": {"type": "noul", "instructions": f"Check `checks[{i}].claim.{field}` against `checks[{i}].cited`."}
                      for i in range(10) for field in ("text", "owner")},
    }


def test_single_claim_jobs_exclude_unrelated_claims_and_keep_related_questions():
    original = fixture_request()
    jobs = requests_for(original, 1)
    assert len(jobs) == 10
    for i, job in enumerate(jobs):
        assert job["state"]["checks"] == [original["state"]["checks"][i]]
        assert set(job["questions"]) == {f"c{i}_text", f"c{i}_owner"}
        if i:
            assert all(f"checks[{i}]" not in q["instructions"] for q in job["questions"].values())
        assert all("checks[0].cited" in q["instructions"] for q in job["questions"].values())


def test_batching_and_single_questions_preserve_every_original_question_once():
    original = fixture_request()
    for size, split in ((8, False), (1, True)):
        jobs = requests_for(original, size, split)
        keys = [key for job in jobs for key in job["questions"]]
        assert len(keys) == len(set(keys)) == len(original["questions"])
        assert set(keys) == set(original["questions"])
        if split:
            assert all(len(job["questions"]) == len(job["state"]["checks"]) == 1 for job in jobs)
        else:
            assert jobs[1]["state"]["checks"] == original["state"]["checks"][8:]
            assert "checks[0].claim" in jobs[1]["questions"]["c8_text"]["instructions"]


def test_failed_calls_remain_in_accuracy_denominator():
    summary = summarize([{"seconds": .1, "completed_at_seconds": .1, "error": "HTTPStatusError"}], {"c0_text": "supported"}, .2)
    assert summary["text_correct"] == summary["text_completed"] == 0
    assert summary["text_total"] == summary["errors"] == 1
    assert summary["first_text_result_seconds"] is None
