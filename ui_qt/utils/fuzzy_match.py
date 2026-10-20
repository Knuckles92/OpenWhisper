"""Fuzzy string matching and scoring utilities for dropdowns and search inputs."""
import re
from itertools import pairwise
from typing import List, Optional

_DELIMITER_PATTERN = re.compile(r"[^\w]+", re.UNICODE)


def _split_tokens(text: str) -> List[str]:
    """Split text into lowercase non-empty tokens by whitespace and punctuation."""
    return [token for token in _DELIMITER_PATTERN.split(text.casefold()) if token]


def _normalize_delimiters(text: str) -> str:
    """Normalize text into space-separated lowercase tokens."""
    return " ".join(_split_tokens(text))


def _strip_delimiters(text: str) -> str:
    """Strip all whitespace and punctuation delimiters from lowercase text."""
    return "".join(_split_tokens(text))


# Base score for each strategy, best first. Bonuses and penalties below stay
# small enough that a strategy never outranks the one above it.
_EXACT_SCORE = 10000
_SUBSTRING_SCORE = 3000
_NORMALIZED_SUBSTRING_SCORE = 2200
_MULTI_TOKEN_SCORE = 1800
_STRIPPED_SUBSTRING_SCORE = 1500
_SUBSEQUENCE_SCORE = 700
_SUBSEQUENCE_MIN_SCORE = 100

_PATH_SEPARATOR = "/"
_WORD_DELIMITERS = " -_:."
_BOUNDARY_CHARS = _WORD_DELIMITERS + _PATH_SEPARATOR

# Shorter query tokens only match whole tokens or token prefixes, so that
# e.g. 'o' does not match the inside of every target token.
_MIN_INFIX_TOKEN_LEN = 3
_MIN_SUBSEQUENCE_LEN = 2


def _position_penalty(pos: int) -> int:
    """Penalize matches that start later in the target, capped at 100."""
    return min(100, pos * 2)


def _score_substring(q: str, t: str) -> Optional[int]:
    """Strategy 1: exact contiguous substring in the raw target."""
    pos = t.find(q)
    if pos == -1:
        return None
    if pos == 0 or t[pos - 1] == _PATH_SEPARATOR:
        prefix_bonus = 1000
    elif t[pos - 1] in _WORD_DELIMITERS:
        prefix_bonus = 500
    else:
        prefix_bonus = 0
    return _SUBSTRING_SCORE + prefix_bonus - _position_penalty(pos)


def _score_normalized_substring(q: str, t: str) -> Optional[int]:
    """Strategy 2: substring after collapsing all delimiters to spaces."""
    q_norm = _normalize_delimiters(q)
    if not q_norm:
        return None
    t_norm = _normalize_delimiters(t)
    pos = t_norm.find(q_norm)
    if pos == -1:
        return None
    if pos == 0:
        prefix_bonus = 800
    elif t_norm[pos - 1] == " ":
        prefix_bonus = 500
    else:
        prefix_bonus = 0
    return _NORMALIZED_SUBSTRING_SCORE + prefix_bonus - _position_penalty(pos)


def _match_token(q_tok: str, t_tokens: List[str], taken: List[int]) -> Optional[int]:
    """Return the index of the first free target token matching ``q_tok``.

    Prefers an exact token match, then a prefix, then (for longer query
    tokens) a substring.
    """
    free = [(idx, t_tok) for idx, t_tok in enumerate(t_tokens) if idx not in taken]
    for idx, t_tok in free:
        if t_tok == q_tok:
            return idx
    for idx, t_tok in free:
        if t_tok.startswith(q_tok):
            return idx
    if len(q_tok) >= _MIN_INFIX_TOKEN_LEN:
        for idx, t_tok in free:
            if q_tok in t_tok:
                return idx
    return None


def _score_multi_token(q: str, t: str) -> Optional[int]:
    """Strategy 3: every query token matches a distinct target token."""
    q_tokens = _split_tokens(q)
    t_tokens = _split_tokens(t)
    if len(q_tokens) < 2 or not t_tokens:
        return None

    matched: List[int] = []
    for q_tok in q_tokens:
        idx = _match_token(q_tok, t_tokens, matched)
        if idx is None:
            return None
        matched.append(idx)

    # _match_token tries exact matches first, so a matched token equal to
    # its query token is exactly the case where the exact pass found it.
    exact_count = sum(
        1 for q_tok, idx in zip(q_tokens, matched, strict=True) if t_tokens[idx] == q_tok
    )
    order_bonus = 200 if matched == sorted(matched) else 0
    return _MULTI_TOKEN_SCORE + order_bonus + exact_count * 50


def _score_stripped_substring(q: str, t: str) -> Optional[int]:
    """Strategy 4: substring with all delimiters removed ('gpt4o' -> 'openai/gpt-4o')."""
    q_stripped = _strip_delimiters(q)
    if not q_stripped:
        return None
    pos = _strip_delimiters(t).find(q_stripped)
    if pos == -1:
        return None
    return _STRIPPED_SUBSTRING_SCORE - _position_penalty(pos)


def _subsequence_positions(chars: str, t: str) -> Optional[List[int]]:
    """Return the leftmost positions of ``chars`` appearing in order in ``t``."""
    positions: List[int] = []
    start = 0
    for char in chars:
        pos = t.find(char, start)
        if pos == -1:
            return None
        positions.append(pos)
        start = pos + 1
    return positions


def _score_subsequence(q: str, t: str) -> Optional[int]:
    """Strategy 5: all query chars appear in order in the target."""
    q_stripped = _strip_delimiters(q)
    if len(q_stripped) < _MIN_SUBSEQUENCE_LEN:
        return None
    positions = _subsequence_positions(q_stripped, t)
    if positions is None:
        return None

    boundary_bonus = 40 * sum(
        1 for pos in positions if pos == 0 or t[pos - 1] in _BOUNDARY_CHARS
    )
    # Each match extending a run of adjacent matches earns 20 per earlier
    # match in that run, so longer runs are rewarded more than linearly.
    consecutive_bonus = 0
    run = 1
    for prev, pos in pairwise(positions):
        run = run + 1 if pos == prev + 1 else 1
        consecutive_bonus += (run - 1) * 20

    span_penalty = min(200, (positions[-1] - positions[0]) * 3)
    score = _SUBSEQUENCE_SCORE + consecutive_bonus + boundary_bonus - span_penalty
    return max(_SUBSEQUENCE_MIN_SCORE, score)


_STRATEGIES = (
    _score_substring,
    _score_normalized_substring,
    _score_multi_token,
    _score_stripped_substring,
    _score_subsequence,
)


def fuzzy_score(query: str, target: str) -> Optional[int]:
    """Score how well ``query`` matches ``target``.

    Higher scores indicate closer matches (e.g. exact match > prefix >
    contiguous substring > multi-token match > subsequence match).

    Args:
        query: User search text.
        target: Target candidate text (e.g., model name, id).

    Returns:
        Integer score (>= 0) if ``query`` matches ``target``, or ``None``
        if it does not match.
    """
    q_raw = query.strip()
    if not q_raw:
        return 0

    t_raw = target.strip()
    if not t_raw:
        return None

    q = q_raw.casefold()
    t = t_raw.casefold()

    if q == t:
        return _EXACT_SCORE

    # The first strategy that matches decides the score.
    for strategy in _STRATEGIES:
        score = strategy(q, t)
        if score is not None:
            return score
    return None


def fuzzy_match(query: str, target: str) -> bool:
    """Return True if ``query`` matches ``target`` according to fuzzy rules."""
    return fuzzy_score(query, target) is not None
