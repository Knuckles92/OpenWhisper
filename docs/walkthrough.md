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

Quick Record has a **Start Walkthrough** button (it reads **Finish
Walkthrough** while one records), and while a walkthrough records the
recording overlay offers **Snap** and **Done**, so it works without any
shortcut. Those two take clicks on Windows and X11, where clicking the
overlay leaves the app you are explaining in front. Both shortcuts start
unassigned: set them under **Settings → Dictation → Walkthroughs**. The tray
menu has **Start Walkthrough** and **Take Screenshot** too. Hovering the
button shows its shortcut, like the buttons above it; the badge at its
right end (**New** until your first walkthrough, then **?**) says what it does.

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

While a walkthrough records, a column at the right edge of the screen
writes it as you go: your voice shows as rows of ticks, like lines of
writing, and each screenshot you keep drops in below what you said before
it, so you can see what the document will hold. At **Done** the ticks close
up into lines while the document is written, the header says its length and
how many screenshots it has, and the column fades once the document is
ready. It never takes focus and never appears in screenshots. Drag it
aside if it covers something; the next walkthrough opens it at the edge
again. Compositor-placed desktops (Wayland) don't show it.

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
| `write-up.pdf`, `write-up.md` | Your agent's write-up, when it is turned on (see below). |

**Settings → Dictation → Walkthroughs** holds everything about walkthroughs:
its two shortcuts, and under **Document** how the PDF and Markdown look:
the title (empty for none), the line with the date, length and screenshot
count, the note on how to read the document, the screenshot captions, and
the PDF's paper size (Letter or A4 by your region unless you choose).
Your words and screenshots are always in, and `walkthrough.json` always
keeps everything. Changes apply to the next walkthrough.

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

## AI write-up

**AI write-up**, on the same page, can have your own coding agent write each
walkthrough up for you: Claude Code, Codex or OpenCode, whichever you choose
under **Agent**, if you have it installed and signed in. It is off by
default. Once the as-spoken documents are written, the agent replies with a
document in Markdown. OpenWhisper lays that out in the same style as
`write-up.pdf` and `write-up.md`, and those are what get copied. The card
and the status say **Write-up copied**.

- **The instructions are yours.** By default the agent is asked to organize
  your words and screenshots into a simple but good-looking walkthrough
  document with a short title, tidy the wording, and put each screenshot
  where it helps most. It chooses the layout: steps, sections or short
  paragraphs. **Instructions** in Settings replaces that; leave it empty for
  the default.
- **Claude Code reads only that walkthrough's folder.** It reads
  `walkthrough.pdf` (and the screenshots beside it, if it wants a closer
  look) with its Read tool alone, fenced to the folder (`--restricted
  --add-dir`), without your own MCP servers, hooks or settings files.
- **Codex and OpenCode read no files at all.** Codex's Windows sandbox
  can't keep reads inside one folder, so rather than fence them, both get
  what Claude Code would read in the message itself: `walkthrough.md` as
  text and the screenshots attached as images. They run with the same lockdown as for
  meetings: no shell, no file tools and none of your own MCP servers. Their
  model must take images; OpenCode's default model is the one in your own
  OpenCode settings.
- **Your words are passed as a transcript**, never as instructions to the
  agent.
- **It uses your own plan** and usually takes 10 to 60 seconds. Dictation
  waits for it, as for any job.
- **The as-spoken files always stay.** If the write-up fails (the agent
  missing or signed out, a timeout, or no usable reply), the walkthrough is
  copied as spoken and the status says why. **Cancel** during the write-up
  cancels the walkthrough, as it does at every other step.

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

- A write-up with a model of your choosing; each agent uses its own default
  model.
- Backups don't include `captures` yet, and walkthrough folders aren't
  pruned; delete them from the folder when you no longer need them.
- A paired host stores a walkthrough's History entry and audio like a
  dictation's, but not its screenshots.
- Whether a particular chat app attaches a file pasted from the clipboard
  depends on the app; drag the chip from the card where it doesn't.
