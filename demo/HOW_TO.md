# OpenWhisper — show a friend

A desktop app for Windows, Mac, and Linux. It turns speech into text on your computer: dictate into any app, drop in an audio file, or record a whole meeting.

Site: https://openwhisper.fiorilabs.tech/

## Send them this

1. The video: `video/openwhisper-demo.mp4` (about 45 seconds, easy on chat)
2. Any picture in `pictures/cards/` if you just want one still
3. This file, or open `index.html` in a browser for the full walkthrough

The meeting titles in the pictures are sample copy, not real recordings.

## Install (Windows)

1. Download **OpenWhisper-Setup-2.6.0.exe** from [openwhisper.fiorilabs.tech](https://openwhisper.fiorilabs.tech/) or [GitHub Releases](https://github.com/Knuckles92/OpenWhisper/releases).
2. Run it. No Python, no admin prompt. It installs for your user only.
3. If Windows says “Windows protected your PC”, click **More info → Run anyway**. The installer is not code-signed yet.
4. Open **OpenWhisper** from the Start menu.

Mac: drag the Apple Silicon `.dmg` into Applications, then **Open Anyway** under Privacy & Security.  
Linux: install the `.deb` or `.pkg.tar.zst` from the same Releases page.

## First two minutes

1. Click **Start Recording**, talk, then **Stop**. Text lands in the Transcription panel.
2. Leave **AI cleanup** on if you want spelling and punctuation cleaned up after.
3. From another app, press `*` on the numpad (Windows). The overlay appears, you talk, you press `*` again, and the text pastes into whatever you were typing.

Change the hotkey anytime in **Settings → Hotkeys**.

## Meeting Mode

1. Open the **Meeting Mode** tab.
2. Leave **Cloud intelligence** checked only if you want live insights and a recap. Audio stays on this PC either way. Uncheck it for a local transcript only.
3. Click **Start Meeting**. Join your call as usual. OpenWhisper records your mic and the other side of the call.
4. **Open dashboard** follows the live transcript in a browser. **Copy guest link** shares that view on your network if you turned LAN sharing on in Settings.
5. Click **End** when the call is over. The app can re-transcribe, clean the text, and write a short report.
6. Open **Past Meetings** (the tab on the right edge) to search, copy, or delete a session.

## Setup people usually want

| Want | Where |
| --- | --- |
| Dark / light theme, bigger type | Settings → General |
| Microphone | Settings → Recording |
| Dictate hotkey | Settings → Hotkeys |
| Local vs cloud speech models | Model Manager |
| Cleanup model (Gemini, OpenRouter, …) | Model Manager → Text cleanup |
| API keys | Settings → API keys (saved in Windows Credential Manager) |
| What happens after End | Settings → After the meeting |
| Dashboard on the LAN | Settings → Dashboard |

New Windows installs default to **Parakeet**. The app will offer to download that model the first time.

## If a friend asks “is my audio uploaded?”

No, not for transcription. Speech can run entirely locally. Cloud intelligence, if they turn it on, sends **transcript text** to the model they configured — not the wav file — unless they later opt into an audio-upload feature.

## Already have this repo

```
venv\Scripts\activate
python app_qt.py
```
