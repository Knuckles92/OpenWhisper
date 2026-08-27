"""Unit tests for the fuzzy string matching and scoring utilities."""
from ui_qt.utils.fuzzy_match import fuzzy_match, fuzzy_score


def test_empty_query_matches_everything():
    assert fuzzy_match("", "google/gemini-3.7-flash") is True
    assert fuzzy_score("", "google/gemini-3.7-flash") == 0
    assert fuzzy_match("   ", "anything") is True
    assert fuzzy_score("   ", "anything") == 0


def test_empty_target_does_not_match_non_empty_query():
    assert fuzzy_match("gemini", "") is False
    assert fuzzy_score("gemini", "") is None


def test_exact_match():
    score = fuzzy_score("openai/gpt-4o", "openai/gpt-4o")
    assert score is not None
    assert score >= 10000


def test_case_insensitivity():
    assert fuzzy_match("GEMINI", "google/gemini-3.7-flash") is True
    assert fuzzy_match("gemini", "GOOGLE/GEMINI-3.7-FLASH") is True


def test_space_vs_hyphen_delimiter_matching():
    # User types 'gemini 3.7' to find 'google/gemini-3.7-flash' or 'gemini-3.7'
    assert fuzzy_match("gemini 3.7", "google/gemini-3.7-flash") is True
    assert fuzzy_match("gemini 3.7", "gemini-3.7") is True
    assert fuzzy_match("gpt 4o", "openai/gpt-4o") is True
    assert fuzzy_match("gpt 4o mini", "openai/gpt-4o-mini") is True
    assert fuzzy_match("claude 3.5 sonnet", "anthropic/claude-3.5-sonnet") is True


def test_multi_token_out_of_order_matching():
    # Tokens 'claude' and 'sonnet' both appear in target
    assert fuzzy_match("claude sonnet", "anthropic/claude-3.5-sonnet") is True
    assert fuzzy_match("sonnet claude", "anthropic/claude-3.5-sonnet") is True
    assert fuzzy_match("llama 70b", "meta-llama/llama-3.1-70b-instruct") is True
    assert fuzzy_match("70b llama", "meta-llama/llama-3.1-70b-instruct") is True


def test_stripped_delimiter_matching():
    # Missing delimiters like 'gpt4o' or 'gemini3.7'
    assert fuzzy_match("gpt4o", "openai/gpt-4o") is True
    assert fuzzy_match("gemini3.7", "google/gemini-3.7-flash") is True
    assert fuzzy_match("o3mini", "openai/o3-mini") is True


def test_subsequence_fuzzy_matching():
    # Initialisms / acronyms / sequential fuzzy characters
    assert fuzzy_match("g3.7", "google/gemini-3.7-flash") is True
    assert fuzzy_match("c35s", "anthropic/claude-3.5-sonnet") is True
    assert fuzzy_match("dskr1", "deepseek/deepseek-r1") is True


def test_negative_matches():
    assert fuzzy_match("gemini 3.7", "google/gemini-2.5-flash") is False
    assert fuzzy_match("gemini 3.7", "openai/gpt-4o") is False
    assert fuzzy_match("nonexistent", "google/gemini-3.7-flash") is False
    assert fuzzy_match("xyz123", "anthropic/claude-3.5-sonnet") is False


def test_regex_special_characters_do_not_error():
    # Characters like (), [], +, *, ?, ^, $, \ should not raise re.error
    assert fuzzy_match("gemini (3.7)", "google/gemini-3.7-flash") is True
    assert fuzzy_match("gpt+4o", "openai/gpt-4o") is True
    assert fuzzy_match("claude-3.5*", "anthropic/claude-3.5-sonnet") is True
    assert fuzzy_match("[test]", "test/model") is True
    assert fuzzy_match("???", "google/gemini-3.7-flash") is False


def test_score_ranking_relevance():
    # Exact match > prefix/contiguous match > multi-token match > loose subsequence
    exact = fuzzy_score("gemini-3.7", "gemini-3.7")
    substring = fuzzy_score("gemini-3.7", "google/gemini-3.7-flash")
    normalized = fuzzy_score("gemini 3.7", "google/gemini-3.7-flash")
    subsequence = fuzzy_score("g37", "google/gemini-3.7-flash")

    assert exact is not None and substring is not None
    assert normalized is not None and subsequence is not None
    assert exact > substring
    assert substring >= normalized
    assert normalized > subsequence
