# Walkthroughs and screenshots

Explain something out loud while you take screenshots, then get one document:
your words with each screenshot right after what you said about it, copied
so you can paste it into an agent. Outside a walkthrough the same screenshot
tool copies a single annotated screenshot.

Code: `services/runtime/walkthrough.py` (what each shortcut press does),
`services/runtime/transcription.py` (the `walkthrough` job),
`services/walkthrough/` (`session`, `sections`, `finish`, `document`,
`storage`), `ui_qt/snap/` (screen capture and the drawing overlay) and
`ui_qt/widgets/walkthrough_ready.py` (the card that says it's ready).

## Using it

Both shortcuts start unassigned: set them under **Settings → Hotkeys →
Walkthroughs**. The tray menu has **Start Walkthrough** (it reads **Finish
Walkthrough** while one records) and **Take Screenshot**.

- **Walkthrough** starts recording. Talk as you would to a colleague looking
  over your shoulder. The recording overlay says **Walkthrough**, then how
  many screenshots you have kept.
- **Screenshot** freezes the screen under the pointer. Drag out the part
  that matters (Enter or a double-click takes the whole screen), mark it up,
  and press Enter to keep it or Esc to throw it away. The recording keeps
  going the whole time, so you can keep talking while you draw.
- **Walkthrough** again finishes. If a screenshot is still open, it is kept
  first.
- **Cancel** throws the walkthrough away, screenshots included.

Drawing tools, with their keys: arrow (A), box (B), pen (P), highlighter
(H), text (T) and numbered steps (N); colours 1–4; undo Ctrl+Z, redo
Ctrl+Shift+Z or Ctrl+Y. Pressing **Screenshot** again while drawing keeps
the screenshot. The overlay never appears in its own screenshots, and on
Windows neither does the recording overlay while a walkthrough records.

## How your words line up with the screenshots

Each screenshot you keep closes a section: everything said since the
previous screenshot, up to the moment you keep this one, goes above it.
Whatever you say after the last screenshot comes last.

- The recorder counts every audio frame, and that count matches the saved
  recording exactly, so the moment you keep a screenshot is an exact place
  in the audio.
- When the walkthrough stops, the recording is cut there. The cut moves to
  the quietest tenth of a second within 0.75 s, but only if that stretch is
  at most half as loud as the moment itself, so a word you were finishing
  as you pressed Enter stays whole and steady speech is cut where you kept
  the screenshot.
- Each section is transcribed on its own with your usual voice model,
  through the same call a dictation uses, so every engine works, including
  a paired host. No engine has to report timestamps.
- Your dictionary, snippets and AI cleanup run on each section separately,
  so cleanup can never move a sentence across a screenshot. The app you
  started in and the text near your cursor play no part: a walkthrough
  becomes a document, not a paste.

## What you get

Each walkthrough gets a folder, `captures/<date>_<time>/` in the data
folder (next to `meetings`):

| File | What it is |
| --- | --- |
| `walkthrough.pdf` | The document as pages: title, date and length, then each section's words, its screenshot and a caption with when it was taken. Text is selectable; screenshots are embedded at full resolution. |
| `walkthrough.md` | The same as Markdown, linking `shot-1.png` and so on. |
| `walkthrough.json` | The sections, screenshot names and times, for tools (and a later AI write-up) to read. |
| `shot-N.png` | Each annotated screenshot, full resolution. |

The clipboard gets two things at once:

- **The PDF as a file.** Apps that take files on paste attach it, and so
  does a folder. Claude reads both the words and the pictures in a PDF of up
  to 100 pages.
- **The Markdown as text, with each screenshot's full path.** A terminal
  pastes this, which is what agents such as Claude Code and Codex need: they
  read the words straight from the prompt and open the full-resolution
  screenshots by path.

A card in the corner of the screen says it's ready, with **Open**, **Show in
folder**, and the PDF as a chip you can drag into any app. Dragging works
even where pasting a file doesn't.

History lists the walkthrough as **Walkthrough**, with `[Screenshot N]`
where each screenshot sits, and keeps its audio like a dictation's. If a
walkthrough fails, its screenshots stay in its folder and the status says
where.

A screenshot taken outside a walkthrough is saved in `captures/screenshots/`
and copied as a picture, a file and its path, so chat apps, folders and
terminals each take the form they want.

## Where it works

- **Windows:** yes. While a walkthrough records, the recording overlay is
  left out of every screen capture, including other apps' screen recordings
  and shares, so it is never in a screenshot.
- **Linux X11:** should work through Qt's screen grab (the overlay hides for
  a moment instead). Not yet tested on hardware.
- **Hyprland:** uses `grim`, which must be installed. Not yet tested on
  hardware.
- **macOS and other Wayland desktops:** not yet; the shortcuts say so.

## Not in this version

- An AI write-up that turns the sections into a cleaner document (the
  manifest already holds what it needs).
- Backups don't include `captures` yet, and walkthrough folders aren't
  pruned; delete them from the folder when you no longer need them.
- A paired host stores a walkthrough's History entry and audio like a
  dictation's, but not its screenshots.
- Whether a particular chat app attaches a file pasted from the clipboard
  depends on the app; drag the chip from the card where it doesn't.
