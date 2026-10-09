# Personalize dictation

**Settings → Personalize** gathers everything that makes dictation sound and
spell like you: Dictionary, Snippets, Styles, Learned rules, Profiles, and
Commands. Basic → Dictation shows the same features in a Personalize group.

## Dictionary

Add names, product terms and jargon on **Settings → Personalize → Dictionary**.
Star the words that matter most; starred words go first wherever space is
limited.

- **Sounds like:** add how the speech engine tends to write a word ("cube
  control" for *kubectl*). Those spellings are replaced in every transcript,
  even with AI cleanup off.
- **Speech model:** with Local Whisper, the OpenAI API, and a remote host
  running Whisper, the engine itself listens for your words. Parakeet,
  Moonshine and Parakeet MLX fix them after recognition instead. The page says
  which applies to your engine.
- **AI cleanup** always receives the list and spells those words your way.
- **Learning:** with **Read text near the cursor** on (see Styles), correcting
  the same dictated word twice in the app you dictated into adds it as a
  **Learned** word with a **New** badge. Keep it or undo it on the page.
  Changing a word back after AI cleanup changed it counts the first time: the
  word is added with a **Kept as said** badge, and AI cleanup is told never to
  replace or respell it.
- Learned rules that only fix a spelling can be moved here in one click.

## Snippets

Say a trigger phrase to insert saved text exactly, with no AI involved.

1. Open **Settings → Personalize → Snippets** and click **New snippet**.
2. Enter the trigger (for example *my calendar link*) and the text to insert.
3. Optionally turn on **Keep formatting** to write light Markdown (bold,
   italic, links, lists). Apps that accept rich text get the formatting; the
   rest get readable plain text.

Say the trigger on its own and the snippet replaces the whole dictation, with
AI cleanup skipped. Say it inside a sentence and the snippet is protected
during cleanup and inserted afterwards. Triggers ignore capitals and
punctuation. Snippets apply to live dictation only.

## Styles and app awareness

OpenWhisper notices which app you're dictating into, and in a browser sites
such as Gmail, Slack or Notion, using the operating system's own window
information. That stays on this computer.

On **Settings → Personalize → Styles** (the *Apps & styles* page):

- **Match tone to each app** gives Email, Work messages, Personal messages and
  Other their own tone: Formal, Casual or Very casual. Formal is the standard
  cleanup voice. Use **Choose apps for each style** to move an app or site
  to another group. Styles need AI cleanup to be on; existing setups start as Formal
  everywhere.
- Terminals never get line breaks, and code editors keep identifiers exactly
  as written, whatever the tone.
- **Read text near the cursor** (Windows, off by default) lets cleanup
  continue your sentence with the right spacing and capitals and spell names
  the way they already appear nearby. That text goes only to your AI cleanup
  provider with the dictation and is never saved. Password fields, terminals,
  OpenWhisper's own windows and apps on **Never read from** are never read.

## Cleanup levels

**Settings → AI cleanup → Cleanup level** chooses how much cleanup changes:

| Level | What it does |
| --- | --- |
| Light | Punctuation, capitals, filler words and obvious recognition errors. |
| Medium | Light, plus spoken corrections ("at 2, actually 3" → "at 3"), spoken lists, and paragraphs. |
| High | Medium, plus removing false starts and tightening the wording. |

The AI cleanup switch remains the on/off control. A saved custom prompt shows
as **Custom** and replaces the level's instructions.

## Command Mode and transforms

Command Mode rewrites selected text from a spoken instruction.

1. Set a shortcut on **Settings → Personalize → Commands** (or Settings →
   Hotkeys). Command Mode follows your record mode: hold to speak in
   push-and-hold, press to start and again to stop in toggle.
2. Select text in any app, use the shortcut, and say what to change ("make
   this more concise", "translate to Spanish").
3. The rewrite replaces the selection.

With nothing selected, say what to write and it's typed at the cursor (turn
this off on the Commands page). Command Mode uses your AI cleanup model even
when cleanup is off. It never sends a copy shortcut to a terminal, never reads
apps on **Never read from** or password fields, and pastes only when it can
confirm the app hasn't changed; otherwise the result is copied for you to
paste. Rewritten text is saved in History with the original.

**Transforms** are saved rewrites (Polish, Make concise, Fix grammar, Prompt
engineer, or your own) with optional shortcuts that rewrite the selected text
in any app. The Scratchpad can apply them too.

## Languages

Pick **Languages I dictate in** on **Settings → Voice model**. With two or
more, switch between them from the chip on the dictation overlay, the tray's
**Language** menu, a shortcut, or the Omarchy bar. A switch applies to the
dictation in progress and never reloads the engine.

## Scratchpad and Stats

- **Scratchpad** (View menu, tray, an optional shortcut, or the Omarchy bar)
  is a floating notepad. Dictation goes straight into it while it's focused;
  notes save automatically and are included in backups.
- **Stats** (View → Stats or the tray) shows words this week and all time,
  words per minute, words AI cleanup changed, your day streak, top apps and
  the last 14 days. Stats stay on this computer and survive Clear history;
  **Reset stats** starts over.
