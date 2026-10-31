# Walkthroughs and screenshots

Explain something out loud while you take screenshots, then get one document:
your words with each screenshot right after what you said about it, copied
so you can paste it into an agent. Outside a walkthrough the same screenshot
tool copies a single annotated screenshot.

Code: `services/runtime/walkthrough.py` (what each shortcut press does),
`services/runtime/transcription.py` (the `walkthrough` job),
`services/walkthrough/` (`session`, `sections`, `finish`, `document`,
`storage`), `ui_qt/snap/` (screen capture and the drawing overlay),
`ui_qt/widgets/walkthrough_panel.py` and `walkthrough_snap_menu.py` (the
column at the screen's edge and its Snap menu) and
`ui_qt/widgets/walkthrough_ready.py` (the card that says it's ready).

## Using it

The main window's **Walkthrough** tab (between Upload File and Meeting Mode)
has a **Start Walkthrough** button. While one records it reads **Finish
Walkthrough**, a **Cancel** button joins it, and the window keeps the
Walkthrough tab in front; a walkthrough started in compact mode stays there.
While a walkthrough records, the column at the screen's edge (below) has
**Snap**, **Done** and the rest of its controls, so it works without any
shortcut. On Wayland, where there is no column, the recording overlay offers
**Snap** and **Done** instead. Both shortcuts start unassigned: the tab shows them (or **Not set**)
above the button, and **Walkthrough settings** opens **Settings → Dictation →
Walkthroughs** to set them. The tray menu has **Start Walkthrough** and
**Take Screenshot** too. Hovering the button shows its shortcut; the badge at
its right end (**New** until your first walkthrough, then **?**) says what it
does.

The tab also holds the choices people change between walkthroughs, saved to
the same settings as **Settings → Dictation → Walkthroughs**:

- **Copy when it finishes**: **PDF**, **PDF + Markdown**, **Markdown** or
  **Word** (see below).
- **AI write-up**: a switch, and one card each for Claude Code, Codex and
  OpenCode saying whether the last scan of this computer found it. Picking an
  agent turns the write-up on.
- **Prompt**, under the agents while the write-up is on: the words the agent
  is given, which you can edit in place, and **Start from**, a menu of
  presets that fill them in: **Walkthrough** (the default), **Step-by-step
  guide**, **Bug report**, **Design feedback** and **Task for an agent**.
  Once you have changed the words it says **Your own**, and choosing that
  again brings your words back after trying a preset. **Follow notes I leave
  for the agent** sits under the prompt.

While the tab is open, the side panel on the right lists **Walkthroughs**
instead of History, the way Meeting Mode lists Past Meetings there: one card
per walkthrough, newest first, with its title (the write-up's heading, or
its first words), its first screenshot, its length and an **AI write-up** or
**Interrupted** label. The search box matches titles and everything said.
Clicking a card shows it in the tab's storyboard (below); **Open library**
opens Saved Walkthroughs. The panel shows the newest 60; the library has
them all.

Below the start button, the tab shows the newest walkthrough as a
**storyboard**: each screenshot beside the words that led to it, with
**Copy PDF**, **Copy Markdown**, **Copy Word**, the PDF as a chip to drag into
any app, and **Open PDF**. It is the same editor as Saved Walkthroughs (see
[Review](#review-recovery-and-storage)). A walkthrough that finishes shows
there unless you have unsaved edits to another. With a walkthrough to show,
the copy choice, **Minimize to tray** and the AI write-up card fold away under
**Setup**, which says what's chosen; with none yet, they show as before.

The window widens for the storyboard (to about 930 pixels, or as far as the
screen allows) and gives the width back when you switch to another tab; a
window you've already made wider stays as it is. Screenshots grow with the
room each row has, and in a narrow window **Open PDF** moves below the copy
buttons, so nothing is cut off at the right edge.

- **Walkthrough** starts recording. Talk as you would to a colleague looking
  over your shoulder. The column's header says how long it has recorded and
  how many screenshots you have kept (on Wayland, the recording overlay
  does).
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
it, with its number and when you took it (**2 · 0:35**), so you can see what
the document will hold. What you are saying now sits in an open, dashed
slot, **Talking… Snap to close this section**, until the next screenshot
closes it. Under it are the walkthrough's controls:

- **Snap** takes a screenshot. Its **▾** opens a menu beside the column that
  picks what Snap takes: a **Region** you draw (the overlay below), the
  **Window under the pointer**, the **Whole screen**, or a region **In 3
  seconds**, which counts down on the button and then freezes the screen,
  so you can open a menu or hover something first. Picking one takes it at
  once, and from then on Snap and the Screenshot shortcut repeat it, in
  later walkthroughs too. A window or the whole screen is kept straight
  away, without the drawing overlay. From the column's button or menu, "the
  window" is the one in front, since clicking the column leaves that app in
  front; from the shortcut it is the one under the pointer. **Window under
  the pointer** is offered on Windows and macOS. Outside a walkthrough the
  Screenshot shortcut always draws a region.
- **Hold for note**: hold it while you talk, and what you say goes to the AI
  write-up as an instruction, not into the document (see AI write-up). It
  shows as a purple row, **Note for the agent · not in the document**. It
  works only while **AI write-up** is on, since nothing else reads a note.
- **Undo** takes the newest screenshot out. What you said before it stays,
  in the section you are speaking.
- **Pause** stops the recording until you press it again; nothing is
  recorded meanwhile, and the screenshots you keep after it still line up
  with your words. Hold for note waits for you to resume.
- **Cancel** throws the walkthrough away. It stays, filling its row, while
  the document is written.
- **Done** finishes it.

Hovering a screenshot shows three buttons: move it up, move it down, or
remove it. Its words always go with it, since a section is what you said and
then the screenshot it led to; a removed screenshot's words join the section
after it, and its file is deleted. What you are saying now always stays last.

At **Done** the ticks close up into lines while the document is written, the
header says its length and how many screenshots it has, and the column fades
once the document is ready. It never takes focus: clicks and drags reach it,
and the app you are explaining keeps the keyboard. It never appears in
screenshots. Drag it aside by anything that isn't a button, and scroll it
with the mouse wheel to reach older screenshots; the next walkthrough opens
it at the edge again. Compositor-placed desktops (Wayland) don't show it.

Drawing tools, with their keys: arrow (A), box (B), pen (P), highlighter
(H), text (T) and numbered steps (N); colours 1–4; undo Ctrl+Z, redo
Ctrl+Shift+Z or Ctrl+Y. Pressing **Screenshot** again while drawing keeps
the screenshot. The overlay never appears in its own screenshots, and on
Windows neither does the recording overlay while a walkthrough records.
A snap freezes every monitor, so you can select on any of them. Starting a
selection on one monitor clears the others. On Hyprland a snap covers only
the focused monitor.

While the column is on screen the recording overlay stays hidden: the
column already shows the recording and its write-up. Notices, such as a
screenshot that couldn't be saved, still appear at the pointer. On Wayland,
where there is no column, the overlay is part of the main window and shows
the walkthrough with its **Snap** and **Done**.

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
- The recording is always cut in the order you kept the screenshots; the
  sections are then put in the column's order, each with its words. Speech
  that ends up next to itself in one section, as after **Undo**, is one
  stretch of audio and transcribed whole, so no word is split where a
  removed screenshot was.
- What you say while holding **Hold for note** is cut out of its section
  exactly where you pressed and let go, with no search for a pause, and
  transcribed on its own as a note, never into the document.
- A pause leaves nothing in the recording, so the frame count, and every
  moment read from it, carries on from where you paused.
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
| `walkthrough.docx` | The same as the PDF, for Word: same words, screenshots, fonts and colours, laid out on the same paper. Word does the page breaks, keeping each screenshot with its words and caption. |
| `walkthrough.json` | The sections, screenshot names and times, for tools (and a later AI write-up) to read, with any notes you held for the agent. |
| `shot-N.png` | Each annotated screenshot, full resolution. |
| `write-up.pdf`, `write-up.md`, `write-up.docx` | Your agent's write-up, when it is turned on (see below). |

**Settings → Dictation → Walkthroughs** holds everything about walkthroughs:
its two shortcuts, and under **Document** how the PDF, Word document and Markdown look:
the title (empty for none), the line with the date, length and screenshot
count, the note on how to read the document, the screenshot captions, and
the paper size of the PDF and Word document (Letter or A4 by your region unless you choose).
Your words and screenshots are always in, and `walkthrough.json` always
keeps everything. Changes apply to the next walkthrough.

Under **Copy**, the **Default copy** dropdown chooses what goes on the
clipboard when a walkthrough finishes: **PDF copy** (the default),
**PDF and Markdown copy**, **Markdown copy**, or **Word document copy**. Every
document is always saved, and the ready card and library still let you copy
each format separately.

With **PDF and Markdown copy**, the clipboard gets two things at once:

- **The PDF as a file.** Apps that take files on paste attach it, and so
  does a folder. Claude reads both the words and the pictures in a PDF of up
  to 100 pages.
- **The Markdown as text, with each screenshot's full path.** A terminal
  pastes this, which is what agents such as Claude Code and Codex need: they
  read the words straight from the prompt and open the full-resolution
  screenshots by path.

A card in the corner of the screen says it's ready, with **Open**, **Show in
folder**, and the PDF as a chip you can drag into any app. Dragging works
even where pasting a file doesn't. **Review / Retry write-up** opens the
Walkthrough tab on the saved document; **Copy PDF** copies only the file, **Copy Word document** copies the
`.docx` file to paste into an email or a folder, and **Copy Markdown** copies only
the Markdown with a file link to each screenshot. **Open Word document** opens it
in Word, or whatever opens `.docx` files, to edit. These actions are also available later
from the **Walkthroughs** side panel next to the **Walkthrough** tab, after
the card disappears or the clipboard has been overwritten.

## Review, recovery, and storage

**Saved Walkthroughs…** lists completed documents and interrupted drafts,
beside the same storyboard the Walkthrough tab shows. Each row is a section:
its screenshot (click it to open the full picture) beside its words. Edit the
words in place, switch off an accidental screenshot, or drag a row by its grip
(or press Alt+Up / Alt+Down in its words) to move a screenshot and its words
together, then choose **Save changes** to regenerate the PDF, Word document,
Markdown, and manifest in that order. A switched-off screenshot stays listed,
faded, so a later save can include it again. Nothing is shared by opening or
saving; use a copy button when ready. The original PNGs remain, and the first
revision's manifest is kept as `walkthrough.original.json`. History remains
the transcript recorded at completion; saving changes the saved documents,
not that historical entry. A note you held for the agent shows in purple on
the row it was said in, and moves with that row; it is never in the
documents, only kept for a write-up.

A walkthrough with an AI write-up opens on the write-up, with a switch to
**As spoken**. The write-up's rows are its Markdown cut at each screenshot
line; edit the words or drag rows, and **Save changes** rebuilds the
write-up's PDF and Word document (a look asked for in a spoken note isn't
kept, since it's never saved). Copy and Open use whichever version shows.
Saving edits to the words as spoken offers **Save and redo write-up**, which
writes it up again from the new words, or **Save without write-up**, which
drops the old write-up because it described the old words.

**Retry AI write-up** uses the saved as-spoken document, the notes you held
for the agent, and your current agent and instructions, without recording or
transcribing again. A failed
retry leaves existing documents available. It runs in the background;
**Cancel operation** requests cancellation and waits for safe completion.

While recording, `draft.json` checkpoints screenshot order, timing, when you
held **Hold for note**, and the identity of the recorder's crash-safe audio
journal. Once stopped, `audio.wav`
keeps an independent recording beside the screenshots. After an interruption,
restart OpenWhisper so startup recovery can join the recovered audio to its
draft, then choose **Recover draft**. It transcribes the saved sections using
your current voice model. A screenshot you removed while recording stays out,
and a note you held is cut out of the document and goes to the write-up; the
order you moved screenshots into is not kept, so a recovered draft has them
in the order taken. Screenshots whose writes never completed are left
out; a draft without recoverable audio keeps its screenshots and explains
why it cannot yet be finished. **Refresh** updates an already-open list.

Retention defaults to **Forever**. In Saved Walkthroughs, set a number of days
and confirm **Apply retention…** to delete expired completed walkthroughs now
and at future startups. Interrupted drafts, standalone screenshots, linked
folders, and folders containing unexpected user files are never automatically
deleted. **Delete…** removes a selected walkthrough only after confirmation.
Deleting a walkthrough does not remove its separate History entry or audio.

Backups include `captures` when **Include saved recordings and captures** is
selected (the default), including documents, screenshots, and recovery drafts.
Text-only backups omit captures. Clipboard image paths are rebuilt for the
current data folder when reopening a restored document.

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
`write-up.pdf`, `write-up.docx` and `write-up.md`, and those are what get copied. The card
and the status say **Write-up copied**.

- **The instructions are yours.** By default the agent is asked to organize
  your words and screenshots into a simple but good-looking walkthrough
  document with a short title, tidy the wording, and put each screenshot
  where it helps most. It chooses the layout: steps, sections or short
  paragraphs. The tab's **Prompt** (also **Instructions** in Settings)
  replaces that, from a preset or in your own words; leave it empty for the
  default.
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
  agent, unless you turn on **Follow notes left for the agent** (off by
  default; it is greyed out while the write-up is off). Then you can talk to
  the agent mid-walkthrough, such as "note for the agent: make the text
  purple" or "for whoever writes this up, skip the setup part". It follows
  the note and leaves the note itself out of the write-up. A note can only
  change what the write-up says and how it looks. The as-spoken files keep
  every word, notes included.
- **Notes you hold for the agent** (**Hold for note** on the column) are
  never in the as-spoken files. They come after the walkthrough in the
  agent's message, each with where you said it ("About screenshot 2"), and
  the agent follows them under the same rule: a note can only change what
  the write-up says and how it looks. They are followed whether or not
  **Follow notes left for the agent** is on. They are saved with the
  walkthrough in `walkthrough.json`, so **Retry AI write-up** and **Save and
  redo write-up** follow them too, placed by the screenshots the document
  still shows.
- **Colours and a font.** Markdown has neither, so when a note or your
  instructions ask for them, the agent starts its reply with a small block
  (`text color`, `heading color`, `font`). OpenWhisper applies it to
  `write-up.pdf` and `write-up.docx` and keeps it out of `write-up.md`. Colours are CSS names or
  `#rrggbb`. A colour Qt can't read is ignored, and a font you don't have
  falls back to the usual one. Links keep their own colour.
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
- **Linux X11:** uses Qt's screen grab (the overlay hides for a moment).
  Capture, annotation, review, and PDF generation were smoke-tested on Jed's
  isolated Xvfb X11 server. Native Xorg desktop/hotkey behavior still needs
  interactive release testing.
- **Hyprland:** uses `grim`, which must be installed. Capture, annotation,
  review, and PDF generation were smoke-tested on Jed's live Hyprland
  desktop. Recording and global-shortcut behavior still need interactive
  release testing.
- **macOS:** yes, once OpenWhisper has Screen Recording permission. The
  first snap asks for it; allow OpenWhisper in **System Settings → Privacy &
  Security → Screen & System Audio Recording**, then reopen OpenWhisper.
  Later snaps without it open that page. Snap briefly hides the recording
  overlay and walkthrough column while capturing, then restores them while
  you draw. Other apps' screen recordings and shares can include these
  overlays. Snap and Done take clicks without taking the focus from the app
  you're in. See the [Mac validation report](walkthrough-macos-validation-2026-10-23.md)
  for the tested flows and remaining release checks.
- **Other Wayland desktops:** not yet; the shortcuts say so.

## Not in this version

- A write-up with a model of your choosing; each agent uses its own default
  model.
- Pause, Hold for note and the Snap menu are on the column, so Wayland,
  which has none, has only Snap and Done.
- A paired host stores a walkthrough's History entry and audio like a
  dictation's, but not its screenshots.
- Whether a particular chat app attaches a file pasted from the clipboard
  depends on the app; drag the chip from the card where it doesn't.
