from meeting.finalization import failed_steps_message, make_step


def test_failure_summary_preserves_reason_and_multiple_failed_stages():
    polish = {**make_step("polish", "failed"), "detail": "Timed out after 60s."}
    summary = {**make_step("consolidation", "failed"), "detail": "Provider unavailable."}
    result = failed_steps_message([polish, summary, make_step("finalize", "completed")])
    assert "Transcript Cleanup, Summary & Action Items failed" in result
    assert "Timed out after 60s." in result
    assert "Provider unavailable." in result
    assert "recording and transcript were kept" in result
    assert "State Finalization:" not in result


def test_legacy_failure_without_detail_and_success():
    assert "polish failed" in failed_steps_message([{"id": "polish", "status": "failed"}])
    assert failed_steps_message([make_step("polish", "completed")]) == ""
