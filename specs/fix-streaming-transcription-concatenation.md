# Fix Streaming Transcription Concatenation Bug

## Problem Summary

The streaming transcription displays duplicated/concatenated text instead of replacing with each new cumulative transcription.

**Expected behavior:**
- C1: audio[0:t1] → display "hello world"
- C2: audio[0:t2] → display "hello world how are you" (REPLACES C1)
- C3: audio[0:t3] → display "hello world how are you doing" (REPLACES C2)

**Current buggy behavior:**
- C1: "hello world"
- C2: "hello world" + "hello world how are you" (APPENDS)
- C3: All three concatenated together

## Root Cause

The `streaming_transcriber.py` correctly implements **rolling re-transcription** - each transcription cycle processes ALL accumulated audio and emits the complete text with `is_final=True`.

However, `quick_record_tab.py` incorrectly **appends** each finalized chunk to a buffer instead of **replacing** it.

| Component | Behavior | Status |
|-----------|----------|--------|
| `services/streaming_transcriber.py` | Rolling re-transcription, emits complete text | ✅ Correct |
| `ui_qt/streaming_text_overlay.py` | Replaces text on `is_final=True` | ✅ Correct |
| `ui_qt/widgets/quick_record_tab.py` | Appends text on `is_final=True` | ❌ **BUG** |

## Code Analysis

### Correct Implementation (`streaming_text_overlay.py:348-367`)

```python
def update_streaming_text(self, text: str, is_final: bool):
    """Update the streaming transcription text.

    With rolling re-transcription, each update REPLACES the entire text
    (not appends), giving Whisper full context to self-correct.
    """
    if is_final:
        # Replace entire text with new complete transcription
        self._full_text = text.strip() if text else ""
        self._current_partial = ""
    else:
        self._current_partial = text.strip() if text else ""

    self._update_display_text()
```

### Buggy Implementation (`quick_record_tab.py:241-266`)

```python
def set_partial_transcription(self, text: str, is_final: bool):
    """Display partial transcription with visual indicator."""
    if is_final:
        # BUG: This chunk is finalized, add to buffer (APPENDS instead of REPLACES)
        self._partial_buffer.append(text)

    # Combine finalized chunks + current partial
    combined = " ".join(self._partial_buffer)
    if not is_final:
        if combined:
            combined += " "
        combined += text + " ..."

    self.transcription_text.setPlainText(combined)
```

## Fix Required

### File: `ui_qt/widgets/quick_record_tab.py`

**Line 250** - Change from append to replace:

```python
# CURRENT (buggy):
if is_final:
    self._partial_buffer.append(text)

# FIXED:
if is_final:
    # Rolling re-transcription: each finalized text is the COMPLETE transcription
    self._partial_buffer = [text] if text else []
```

This single-line change makes the quick record tab match the streaming overlay's behavior - replacing the buffer contents with the new complete transcription instead of appending.

## Implementation Steps

1. **Edit `ui_qt/widgets/quick_record_tab.py`**
   - Line 250: Change `self._partial_buffer.append(text)` to `self._partial_buffer = [text] if text else []`
   - Optional: Add clarifying comment about rolling re-transcription

## Verification

1. Run the application: `python app_qt.py`
2. Start a streaming recording
3. Speak continuously for 10+ seconds
4. Observe the transcription in the quick record tab:
   - Text should be coherent, not duplicated
   - Each update should replace the previous text
   - No repeated phrases or concatenation artifacts
5. Compare with streaming overlay behavior (should now match)

## Files to Modify

- `ui_qt/widgets/quick_record_tab.py` (1 line change at line 250)
