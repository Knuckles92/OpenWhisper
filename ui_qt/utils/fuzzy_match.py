"""Fuzzy string matching and scoring utilities for dropdowns and search inputs."""
import re
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
        return 10000

    # Strategy 1: Exact contiguous substring in raw target.
    pos = t.find(q)
    if pos != -1:
        prefix_bonus = 0
        if pos == 0 or (pos > 0 and t[pos - 1] == "/"):
            prefix_bonus = 1000
        elif pos > 0 and t[pos - 1] in " -_:.":
            prefix_bonus = 500
        pos_penalty = min(100, pos * 2)
        return 3000 + prefix_bonus - pos_penalty

    # Strategy 2: Normalized substring (all delimiters collapsed to spaces).
    q_norm = _normalize_delimiters(q)
    t_norm = _normalize_delimiters(t)
    if q_norm and q_norm in t_norm:
        norm_pos = t_norm.find(q_norm)
        prefix_bonus = 0
        if norm_pos == 0:
            prefix_bonus = 800
        elif norm_pos > 0 and t_norm[norm_pos - 1] == " ":
            prefix_bonus = 500
        pos_penalty = min(100, norm_pos * 2)
        return 2200 + prefix_bonus - pos_penalty

    # Strategy 3: Multi-token match (all query tokens matched against target tokens).
    q_tokens = _split_tokens(q)
    t_tokens = _split_tokens(t)
    if len(q_tokens) > 1 and t_tokens:
        matched_token_indices = []
        exact_count = 0
        all_tokens_found = True

        for q_tok in q_tokens:
            matched_idx = -1
            # Prefer exact token match first, then prefix, then substring (if >= 3 chars)
            for idx, t_tok in enumerate(t_tokens):
                if idx in matched_token_indices:
                    continue
                if t_tok == q_tok:
                    matched_idx = idx
                    exact_count += 1
                    break
            if matched_idx == -1:
                for idx, t_tok in enumerate(t_tokens):
                    if idx in matched_token_indices:
                        continue
                    if t_tok.startswith(q_tok):
                        matched_idx = idx
                        break
            if matched_idx == -1 and len(q_tok) >= 3:
                for idx, t_tok in enumerate(t_tokens):
                    if idx in matched_token_indices:
                        continue
                    if q_tok in t_tok:
                        matched_idx = idx
                        break

            if matched_idx != -1:
                matched_token_indices.append(matched_idx)
            else:
                all_tokens_found = False
                break

        if all_tokens_found:
            in_order = matched_token_indices == sorted(matched_token_indices)
            order_bonus = 200 if in_order else 0
            exact_bonus = exact_count * 50
            return 1800 + order_bonus + exact_bonus

    # Strategy 4: Delimiter-stripped substring match (e.g., 'gpt4o' -> 'openai/gpt-4o').
    q_stripped = _strip_delimiters(q)
    t_stripped = _strip_delimiters(t)
    if q_stripped and q_stripped in t_stripped:
        stripped_pos = t_stripped.find(q_stripped)
        pos_penalty = min(100, stripped_pos * 2)
        return 1500 - pos_penalty

    # Strategy 5: Subsequence match (all query chars appear in order in target).
    if q_stripped and len(q_stripped) >= 2:
        target_idx = 0
        consecutive = 0
        consecutive_bonus = 0
        boundary_bonus = 0
        first_match_idx = -1
        last_match_idx = -1

        for q_char in q_stripped:
            found = False
            while target_idx < len(t):
                if t[target_idx] == q_char:
                    if first_match_idx == -1:
                        first_match_idx = target_idx
                    last_match_idx = target_idx

                    if target_idx == 0 or (target_idx > 0 and t[target_idx - 1] in " -_/:."):
                        boundary_bonus += 40

                    if consecutive > 0:
                        consecutive_bonus += consecutive * 20
                    consecutive += 1

                    target_idx += 1
                    found = True
                    break
                else:
                    consecutive = 0
                    target_idx += 1

            if not found:
                return None

        span = last_match_idx - first_match_idx
        span_penalty = min(200, span * 3)
        score = 700 + consecutive_bonus + boundary_bonus - span_penalty
        return max(100, score)

    return None


def fuzzy_match(query: str, target: str) -> bool:
    """Return True if ``query`` matches ``target`` according to fuzzy rules."""
    return fuzzy_score(query, target) is not None
