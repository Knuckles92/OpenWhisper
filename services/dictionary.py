"""The personal dictionary: words OpenWhisper should always get right.

Terms reach the speech model where an engine supports hints, replace their
"sounds like" variants in every transcript, and go into the cleanup prompt.
Terms are user content: never log them, or anything read from other apps.

The dictionary is a list of ``{id, term, starred, heard, learned, new,
protected}`` objects under ``SettingsKey.DICTATION_DICTIONARY``, newest first. Loading is
tolerant: malformed entries are skipped, duplicates (ignoring case) keep the
first, and the list is capped at ``config.MAX_DICTIONARY_TERMS``. The
mutators here edit a settings mapping in place, for
``settings_manager.mutate_settings``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import uuid
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Callable, Mapping, Optional, Sequence

from config import config
from services import focus_context
from services.settings import (
    SettingsKey,
    resolve_app_context_read_text,
    resolve_dictionary_learn_enabled,
    resolve_dictionary_steer_recognition,
    resolve_transcript_cleanup_rules,
    settings_manager,
)
from services.vocabulary import replace_terms

logger = logging.getLogger(__name__)

#: Longest term or "sounds like" variant, in characters.
MAX_TERM_CHARS = 64
#: "Sounds like" variants per term.
MAX_HEARD = 5
#: The cleanup prompt lists at most this much of the dictionary, starred
#: first, so a large dictionary still fits a 4k-context local model.
PROMPT_MAX_TERMS = 200
PROMPT_MAX_CHARS = 2400
#: How long after a paste the field is read again for corrections.
LEARN_DELAY_S = 4.0
#: Sightings of the same correction before it is learned.
LEARN_SIGHTINGS = 2
#: Words the cleanup prompt tells AI cleanup never to change.
PROMPT_MAX_PROTECTED = 50

_KEY = SettingsKey.DICTATION_DICTIONARY


class DictionaryError(ValueError):
    """A change that can't be saved; the message is shown to the user."""


@dataclass(frozen=True)
class DictionaryTerm:
    id: str
    term: str
    starred: bool = False
    #: User-entered "sounds like" variants replaced by ``term``.
    heard: tuple[str, ...] = ()
    learned: bool = False
    #: Learned since the user last reviewed the Dictionary page.
    new: bool = False
    #: AI cleanup changed this word and the user put it back, so the cleanup
    #: prompt tells it never to change the word again.
    protected: bool = False


def _clean(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _stable_id(term: str) -> str:
    # Entries saved without an id (hand edits) keep the same id on every load.
    return "t" + hashlib.sha1(term.casefold().encode("utf-8")).hexdigest()[:11]


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _variants(raw, term: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    found: list[str] = []
    seen = {term.casefold()}
    for value in raw:
        variant = _clean(value)
        if not variant or len(variant) > MAX_TERM_CHARS or variant.casefold() in seen:
            continue
        seen.add(variant.casefold())
        found.append(variant)
        if len(found) >= MAX_HEARD:
            break
    return tuple(found)


def _parse(raw) -> list[DictionaryTerm]:
    if not isinstance(raw, list):
        return []
    terms: list[DictionaryTerm] = []
    seen: set[str] = set()
    ids: set[str] = set()
    for item in raw:
        if len(terms) >= config.MAX_DICTIONARY_TERMS:
            break
        if not isinstance(item, Mapping):
            continue
        term = _clean(item.get("term"))
        if not term or len(term) > MAX_TERM_CHARS or term.casefold() in seen:
            continue
        learned = item.get("learned") is True
        saved_id = item.get("id")
        term_id = saved_id.strip() if isinstance(saved_id, str) and saved_id.strip() else _stable_id(term)
        while term_id in ids:
            term_id = _stable_id(term_id + term)
        seen.add(term.casefold())
        ids.add(term_id)
        terms.append(DictionaryTerm(
            id=term_id,
            term=term,
            starred=item.get("starred") is True,
            # Learned terms never replace text: a correction seen twice is
            # not proof that every occurrence of the old word was wrong.
            heard=() if learned else _variants(item.get("heard"), term),
            learned=learned,
            new=learned and item.get("new") is True,
            protected=item.get("protected") is True,
        ))
    return terms


def _as_dict(term: DictionaryTerm) -> dict:
    saved = {
        "id": term.id,
        "term": term.term,
        "starred": term.starred,
        "heard": list(term.heard),
        "learned": term.learned,
        "new": term.new,
    }
    if term.protected:
        saved["protected"] = True
    return saved


def _store(settings: dict, terms: Sequence[DictionaryTerm]) -> None:
    settings[_KEY] = [_as_dict(term) for term in terms]


def load_dictionary(settings: Mapping) -> list[DictionaryTerm]:
    """The valid saved terms, in their saved order (newest first)."""
    return _parse((settings or {}).get(_KEY))


def _ranked(terms: Sequence[DictionaryTerm]) -> list[DictionaryTerm]:
    return [term for term in terms if term.starred] + [term for term in terms if not term.starred]


def recognition_phrases(settings: Mapping) -> tuple[str, ...]:
    """Speech-model hints: starred terms first, cut to the engine budget.

    Empty while "Steer the speech model" is off.
    """
    if not resolve_dictionary_steer_recognition(settings or {}):
        return ()
    ranked = _ranked(load_dictionary(settings))
    return tuple(term.term for term in ranked[: config.MAX_RECOGNITION_PHRASES])


def apply_replacements(text: str, terms: Sequence[DictionaryTerm]) -> str:
    """``text`` with every "sounds like" variant replaced by its term.

    Whole words, any case, longest variant first, in one pass. Only variants
    the user typed count; learned terms have none.
    """
    rules: dict[str, str] = {}
    for term in _ranked(terms):
        if term.learned:
            continue
        for variant in term.heard:
            rules.setdefault(variant, term.term)
    return replace_terms(text, rules)


def prompt_block(terms: Sequence[DictionaryTerm]) -> str:
    """The cleanup-prompt block listing the terms, or ""."""
    names: list[str] = []
    used = 0
    for term in _ranked(terms):
        cost = len(term.term) + 2
        if len(names) >= PROMPT_MAX_TERMS or used + cost > PROMPT_MAX_CHARS:
            break
        names.append(term.term)
        used += cost
    if not names:
        return ""
    block = (
        "Vocabulary — when one of these is what was said, including near-miss "
        "mishearings, spell it exactly: " + ", ".join(names) + "."
    )
    kept = [term.term for term in _ranked(terms) if term.protected][:PROMPT_MAX_PROTECTED]
    if kept:
        block += (
            " The speaker put these back after an earlier cleanup changed them; "
            "never replace or respell them: " + ", ".join(kept) + "."
        )
    return block


def summary(settings: Mapping) -> str:
    """"12 words · 2 new", for the Settings rail."""
    terms = load_dictionary(settings)
    if not terms:
        return "No words yet"
    text = f"{len(terms)} word{'' if len(terms) == 1 else 's'}"
    fresh = sum(term.new for term in terms)
    return f"{text} · {fresh} new" if fresh else text


# ---- mutators, for settings_manager.mutate_settings ------------------------


def parse_heard(text: str) -> tuple[str, ...]:
    """"Sounds like" variants typed into one field, split at commas or semicolons."""
    return tuple(part for part in (_clean(piece) for piece in re.split(r"[,;\n]", text or "")) if part)


def _checked_term(term: str) -> str:
    text = _clean(term)
    if not text:
        raise DictionaryError("Type a word or name first.")
    if len(text) > MAX_TERM_CHARS:
        raise DictionaryError(f"Keep words under {MAX_TERM_CHARS} characters.")
    return text


def _checked_heard(heard: Sequence[str], term: str) -> tuple[str, ...]:
    cleaned = [_clean(variant) for variant in heard]
    if any(len(variant) > MAX_TERM_CHARS for variant in cleaned):
        raise DictionaryError(f"Keep each “sounds like” under {MAX_TERM_CHARS} characters.")
    distinct = {variant.casefold() for variant in cleaned if variant} - {term.casefold()}
    if len(distinct) > MAX_HEARD:
        raise DictionaryError(f"Up to {MAX_HEARD} “sounds like” spellings per word.")
    return _variants(cleaned, term)


def _index(terms: Sequence[DictionaryTerm], term_id: str) -> int:
    for index, term in enumerate(terms):
        if term.id == term_id:
            return index
    raise DictionaryError("That word is no longer in your dictionary.")


def _index_of_text(terms: Sequence[DictionaryTerm], text: str, *, skip: str = "") -> int:
    key = text.casefold()
    for index, term in enumerate(terms):
        if term.id != skip and term.term.casefold() == key:
            return index
    return -1


def add_term(settings: dict, term: str, *, heard: Sequence[str] = (), starred: bool = False) -> DictionaryTerm:
    """Add ``term`` at the top, or merge into the entry it duplicates.

    Re-adding a word keeps its place, takes the new spelling and any new
    "sounds like" variants, and makes a learned word the user's own.

    Raises:
        DictionaryError: The term is blank or too long, it is already there
            unchanged, or the dictionary is full.
    """
    text = _checked_term(term)
    variants = _checked_heard(heard, text)
    terms = load_dictionary(settings)
    index = _index_of_text(terms, text)
    if index >= 0:
        existing = terms[index]
        merged = _checked_heard(existing.heard + variants, text)
        updated = replace(
            existing, term=text, heard=merged, learned=False, new=False,
            starred=existing.starred or starred,
        )
        if updated == existing:
            raise DictionaryError("That's already in your dictionary.")
        terms[index] = updated
    else:
        if len(terms) >= config.MAX_DICTIONARY_TERMS:
            raise DictionaryError(f"Your dictionary is full ({config.MAX_DICTIONARY_TERMS} words).")
        updated = DictionaryTerm(_new_id(), text, starred=starred, heard=variants)
        terms.insert(0, updated)
    _store(settings, terms)
    return updated


def update_term(settings: dict, term_id: str, *, term: str, heard: Sequence[str]) -> DictionaryTerm:
    """Replace one entry's spelling and "sounds like" variants.

    An edited learned word becomes the user's own and loses its New badge.
    """
    text = _checked_term(term)
    variants = _checked_heard(heard, text)
    terms = load_dictionary(settings)
    index = _index(terms, term_id)
    if _index_of_text(terms, text, skip=term_id) >= 0:
        raise DictionaryError("That's already in your dictionary.")
    terms[index] = replace(terms[index], term=text, heard=variants, learned=False, new=False)
    _store(settings, terms)
    return terms[index]


def set_starred(settings: dict, term_id: str, starred: bool) -> DictionaryTerm:
    terms = load_dictionary(settings)
    index = _index(terms, term_id)
    terms[index] = replace(terms[index], starred=bool(starred), new=False)
    _store(settings, terms)
    return terms[index]


def delete_term(settings: dict, term_id: str) -> Optional[DictionaryTerm]:
    """Remove one entry; returns it, or None when it was already gone."""
    terms = load_dictionary(settings)
    for index, term in enumerate(terms):
        if term.id == term_id:
            del terms[index]
            _store(settings, terms)
            return term
    return None


def undo_learned(settings: dict, term_id: str) -> Optional[DictionaryTerm]:
    """Remove a learned entry; a word the user added or edited stays."""
    terms = load_dictionary(settings)
    for index, term in enumerate(terms):
        if term.id == term_id and term.learned:
            del terms[index]
            _store(settings, terms)
            return term
    return None


def clear_new(settings: dict) -> int:
    """Keep every new learned word and drop its New badge; returns how many."""
    terms = load_dictionary(settings)
    fresh = sum(term.new for term in terms)
    if fresh:
        _store(settings, [replace(term, new=False) for term in terms])
    return fresh


def add_learned(settings: dict, word: str, *, protected: bool = False) -> Optional[DictionaryTerm]:
    """Add a word learned from a correction, unless learning is off or it is known.

    ``protected`` marks a word the user put back after AI cleanup changed it;
    a known word gains that mark (and is returned) once.
    """
    if not resolve_dictionary_learn_enabled(settings):
        return None
    text = _clean(word)
    if not text or len(text) > MAX_TERM_CHARS:
        return None
    terms = load_dictionary(settings)
    index = _index_of_text(terms, text)
    if index >= 0:
        if not protected or terms[index].protected:
            return None
        terms[index] = replace(terms[index], protected=True)
        _store(settings, terms)
        return terms[index]
    if len(terms) >= config.MAX_DICTIONARY_TERMS:
        return None
    learned = DictionaryTerm(_new_id(), text, learned=True, new=True, protected=protected)
    terms.insert(0, learned)
    _store(settings, terms)
    return learned


def find_term(settings: Mapping, text: str) -> Optional[DictionaryTerm]:
    """The saved entry spelled ``text`` (ignoring case), or None."""
    terms = load_dictionary(settings)
    index = _index_of_text(terms, _clean(text))
    return terms[index] if index >= 0 else None


# ---- spelling rules taught on the Learned rules page -----------------------

_QUOTED = re.compile(r"[\"“”]([^\"“”]{1,%d})[\"“”]" % MAX_TERM_CHARS)
_SPELL = re.compile(r"\bspell(?:s|ed|ing)?\b", re.IGNORECASE)
# A condition makes the rule more than a spelling; it stays a rule.
_CONDITIONAL = re.compile(r"\b(?:except|unless|when|whenever|if|only|but)\b", re.IGNORECASE)
_SPELLING_RULE_MAX_CHARS = 120


def spelling_rule(rule: str) -> Optional[tuple[str, tuple[str, ...]]]:
    """The ``(term, heard)`` a plain spelling rule teaches, or None.

    Matches short, unconditional rules such as ``Always spell my name "Alex
    Rivera"``, ``Spell "jon" as "John"`` and ``Spell it "Ksenia", not
    "Sonia"``.
    """
    if not isinstance(rule, str) or len(rule) > _SPELLING_RULE_MAX_CHARS:
        return None
    if not _SPELL.search(rule) or _CONDITIONAL.search(rule):
        return None
    quoted = [_clean(text) for text in _QUOTED.findall(rule)]
    if any(not text for text in quoted):
        return None
    if len(quoted) == 1:
        return quoted[0], ()
    if len(quoted) == 2 and re.search(r"\bas\b", rule, re.IGNORECASE):
        return quoted[1], (quoted[0],)
    if len(quoted) == 2 and re.search(r"\bnot\b", rule, re.IGNORECASE):
        return quoted[0], (quoted[1],)
    return None


def spelling_rules(settings: Mapping) -> list[str]:
    """Learned cleanup rules that are plain spellings, which belong here."""
    return [rule for rule in resolve_transcript_cleanup_rules(dict(settings or {}))
            if spelling_rule(rule) is not None]


def move_spelling_rules(settings: dict) -> int:
    """Turn plain spelling rules into dictionary words; returns how many moved.

    A rule whose word cannot be added (the dictionary is full) stays a rule.
    """
    rules = resolve_transcript_cleanup_rules(settings)
    kept: list[str] = []
    moved = 0
    for rule in rules:
        parsed = spelling_rule(rule)
        if parsed is None:
            kept.append(rule)
            continue
        term, heard = parsed
        try:
            add_term(settings, term, heard=heard)
        except DictionaryError:
            if _index_of_text(load_dictionary(settings), _clean(term)) < 0:
                kept.append(rule)
                continue
        moved += 1
    if moved:
        settings[SettingsKey.TRANSCRIPT_CLEANUP_RULES] = kept
    return moved


# ---- learning from corrections ----------------------------------------------

#: Words too common to be a personal spelling, even when the user fixed one.
COMMON_WORDS = frozenset("""
a about above after again against all also am an and any are as at be because been
before being below between both but by can could did do does doing down during each
few for from further had has have having he her here hers herself him himself his how
i if in into is it its itself just me more most my myself no nor not now of off on
once only or other our ours ourselves out over own same she should so some such than
that the their theirs them themselves then there these they this those through to too
under until up very was we were what when where which while who whom why will with
would you your yours yourself yourselves yes ok okay hi hey hello thanks thank please
sorry well like know think want need get got make made go going went come came see
saw say said tell told ask asked use used work works good great new old first last
next one two three four five six seven eight nine ten hundred thousand today tomorrow
yesterday morning afternoon evening night week month year time day days
monday tuesday wednesday thursday friday saturday sunday january february march april
may june july august september october november december
""".split())

_WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")
# Punctuation a word may carry in running text; anything else (an
# underscore, a slash, an @) keeps the token whole so it is never learned.
_EDGE = ".,!?;:\"“”'‘’()[]{}<>«»…"
_learned_callback: Optional[Callable[[str], None]] = None
_sightings: dict[str, int] = {}
_sightings_lock = threading.Lock()


def set_learned_callback(callback: Optional[Callable[[str], None]]) -> None:
    """Call ``callback(term)`` (on a worker thread) whenever a word is learned."""
    global _learned_callback
    _learned_callback = callback


def _tokens(text: str) -> list[str]:
    return [token for token in (raw.strip(_EDGE) for raw in (text or "").split()) if token]


def _bare(word: str) -> str:
    return re.sub(r"['’-]", "", word).casefold()


def _learnable(old: str, new: str) -> bool:
    if len(new) < 3 or not _WORD.fullmatch(new) or not any(ch.isalpha() for ch in new):
        return False
    if _bare(new) == _bare(old):
        return False  # only case, an apostrophe or a hyphen changed
    return new.casefold() not in COMMON_WORDS


def learned_correction(pasted: str, field: str) -> Optional[str]:
    """The word the user put in place of one pasted word, or None.

    ``field`` is the text around the caret read a few seconds after the
    paste. The pasted words are aligned with it word by word; only a field
    that still holds the pasted text with exactly one pasted word swapped
    for one other word counts, with at least two unchanged words to anchor
    it. Text typed before or after the pasted span is ignored.
    """
    found = _correction(pasted, field)
    return found[1] if found else None


def reverted_cleanup(before_cleanup: Optional[str], replaced: str, restored: str) -> bool:
    """Whether a correction put back a word AI cleanup had changed.

    The restored word was in the text before cleanup and the word the user
    replaced was not, so cleanup brought it in.
    """
    spoken = {word.casefold() for word in _tokens(before_cleanup or "")}
    return restored.casefold() in spoken and replaced.casefold() not in spoken


def _correction(pasted: str, field: str) -> Optional[tuple[str, str]]:
    """``(pasted word, the user's word)``; see ``learned_correction``."""
    said = _tokens(pasted)
    seen = _tokens(field)
    if len(said) < 3 or not seen:
        return None
    matcher = SequenceMatcher(
        None, [word.casefold() for word in said], [word.casefold() for word in seen],
        autojunk=False,
    )
    changes = []
    anchors = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            anchors += i2 - i1
        elif tag == "insert" and i1 in (0, len(said)):
            continue  # the field's own text before or after the paste
        else:
            changes.append((tag, i1, i2, j1, j2))
    if len(changes) != 1 or anchors < 2:
        return None
    tag, i1, i2, j1, j2 = changes[0]
    if tag != "replace" or i2 - i1 != 1 or j2 - j1 != 1:
        return None
    old, new = said[i1], seen[j1]
    return (old, new) if _learnable(old, new) else None


def _run_later(delay: float, function, *args) -> None:
    timer = threading.Timer(delay, function, args=args)
    timer.daemon = True
    timer.name = "dictionary-learning"
    timer.start()


def schedule_learning(job, pasted_text: str, before_cleanup: Optional[str] = None) -> None:
    """Look for a correction of the pasted text later; Qt thread, never blocks.

    Runs only for a dictation whose start captured the text around the
    caret, so the field can be read again. The read happens on the capture
    service's own thread. ``before_cleanup`` is the transcript before AI
    cleanup changed it, if it did: a correction that puts back one of its
    words is learned at once and protected from cleanup.
    """
    if not pasted_text or not pasted_text.strip() or job is None:
        return
    snapshot = job.snapshot(timeout=0)
    identity = snapshot.identity if snapshot is not None else None
    context = snapshot.text if snapshot is not None else None
    if identity is None or identity.is_self or context is None or not context.caret_known:
        return
    _run_later(LEARN_DELAY_S, _reread, identity, pasted_text, before_cleanup)


def _reread(identity, pasted_text: str, before_cleanup: Optional[str] = None) -> None:
    try:
        settings = settings_manager.load_all_settings()
        if not (resolve_dictionary_learn_enabled(settings) and resolve_app_context_read_text(settings)):
            return
        focus_context.get_service().reread(
            identity, lambda context: _on_reread(context, pasted_text, before_cleanup)
        )
    except Exception:
        logger.debug("Dictionary learning could not read the field again", exc_info=True)


def _on_reread(context, pasted_text: str, before_cleanup: Optional[str] = None) -> None:
    """The capture service's callback; keeps that thread free of file writes."""
    try:
        if context is None or not context.caret_known:
            return
        field = f"{context.before}{context.selected}{context.after}"
        found = _correction(pasted_text, field)
        if found is None:
            return
        replaced, word = found
        if reverted_cleanup(before_cleanup, replaced, word):
            # Undoing an AI edit is deliberate; one sighting is enough.
            _run_later(0, _learn, word, True)
            return
        key = word.casefold()
        with _sightings_lock:
            count = _sightings.get(key, 0) + 1
            if count < LEARN_SIGHTINGS:
                _sightings[key] = count
                return
            _sightings.pop(key, None)
        _run_later(0, _learn, word)
    except Exception:
        logger.debug("Dictionary learning failed", exc_info=True)


def _learn(word: str, protected: bool = False) -> None:
    try:
        learned = settings_manager.mutate_settings(
            lambda settings: add_learned(settings, word, protected=protected)
        )
    except Exception:
        logger.warning("Could not save a learned dictionary word", exc_info=True)
        return
    if learned is None:
        return
    logger.info("Dictionary learned a word from a correction")
    callback = _learned_callback
    if callback is not None:
        try:
            callback(learned.term)
        except Exception:
            logger.debug("Learned-word listener raised", exc_info=True)
