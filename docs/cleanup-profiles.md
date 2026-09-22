# On-demand cleanup profiles

Profiles turn dictated speech into a reusable output format. **Support ticket**
and **Email** are included as editable starters.

1. On **Quick Record**, check **AI cleanup** to show **Cleanup profile**, then
   click **Manage…**. You can also open **Settings → Profiles**.
2. Select a starter, or click **New profile**. Give it a name and instructions
   describing the structure, headings, tone, and details you want.
3. Optionally include your learned rules and set a dedicated recording shortcut.
   Click the shortcut field, press the keys, then release them. Escape cancels
   capture; **Clear** removes the shortcut. Shortcuts already assigned to another
   profile or a standard action cannot be saved.
4. Click **Save profile**. Choose it on Quick Record and click **Start Recording**,
   or press its shortcut from another app. Press the profile shortcut again to
   stop. **Settings → Hotkeys → Profile recording shortcuts…** opens the editor too.

Profile shortcuts always toggle recording. The standard record shortcut and tray
action use standard dictation, including its existing toggle or push-and-hold mode.
When a recording is already running, pressing a profile shortcut stops that
recording and keeps the format chosen at its start.

Profiles always run AI cleanup, even when cleanup is off for standard dictation.
They share the chat model configured in **Settings → AI cleanup**.
Their output instructions replace the standard cleanup prompt. Learned rules are
optional; the profile's format takes priority over conflicting formatting rules.
Uploads and meetings use their existing settings.

The app remembers the Quick Record selection. Editing or deleting a profile does
not change a recording already in progress. Duplicates start without a shortcut;
deleting a profile removes its shortcut. Deleting the selected profile returns
Quick Record to standard dictation.

Finished output uses the usual copy and auto-paste settings, and can be copied
from Quick Record. History records the profile name and retains the raw transcript
when cleanup changes it. If cleanup is unavailable or fails, the app keeps the raw
transcript and reports the formatting failure.
