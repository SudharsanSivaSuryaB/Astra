# Desktop Companion Starter (Windows)

A small desktop AI assistant prototype with an original, holographic interface and concise, professional voice. It draws its visual with Qt, so no external artwork or model downloads are required. The style takes inspiration from cinematic AI but does not use movie character artwork or dialogue.

## Features
- Transparent, borderless, always-on-top overlay
- Drag the companion around the screen
- Animated AI-core indicator
- Starts hidden and appears when it hears “Astra”
- Conversation mode after saying “Astra”; no repeated wake word is needed during the conversation
- Time-aware greetings and local preset replies for common requests
- Voice commands to greet, tell the current time, hide, show, or exit with “goodbye”
- Voice-controlled Notepad with separate open, dictate, stop, and exit commands
- Voice commands for browsers, Windows apps, tabs, focused-app editing, and window management
- Uses the installed Windows Aria voice first for low-latency speech, with Microsoft's online Aria voice as a fallback
- Microphone input and speech recognition through SpeechRecognition and Google Speech Recognition (internet required)
- Tray menu to show or exit

## Requirements
- Windows 10/11
- Python 3.10 or newer
- Internet connection to install Python packages and for speech recognition

## Run it
1. Install Python from https://www.python.org/downloads/ (enable "Add Python to PATH").
2. Extract this ZIP into a folder.
3. Open that folder in VS Code.
4. Open Terminal and run:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   python main.py
   ```

If PowerShell blocks activation, use Command Prompt instead:

   ```bat
   py -m venv .venv
   .venv\Scripts\activate.bat
   pip install -r requirements.txt
   python main.py
   ```

## Voice Command Reference

Say “Astra” to wake the assistant. After it responds, continue speaking without repeating the wake word. Commands can be chained in order, for example “scroll down, then close tab.” Treat `type` as a text-entry command: everything after it in that utterance is typed literally.

### Assistant and notes
- `goodbye` exits Astra. `hide` / `go away` / `dismiss` hides the overlay; `show` / `come back` / `appear` brings it back. Ask `what time is it` for the current time.
- `open Notepad`, `open notes`, or `launch Notepad` focuses the existing Notepad window, or opens Notepad if none is open. `take notes` / `start notes` begins dictation; `stop notes` pauses it; say `take notes` again to resume; `exit notes`, `exit Notepad`, or `close Notepad` exits the notes session.
- Suggested checks: `open Notepad`; `take notes`; dictate a short sentence; `stop notes`; `take notes` to resume; then `exit notes` while paused. If Notepad asks to save, say `cancel save` to keep it open, or `don't save` to close without saving.
- To check that an existing document is reused, open Notepad and leave a document visible, then say `open Notepad` or `open notes`; Astra should focus that window. Also try `exit Notepad` and `close Notepad` after pausing notes.
- `save notes` saves the document. If Astra asks for a filename, dictate it. If a save prompt appears, say `select save`, `don't save`, or `cancel save` as appropriate.

### Browsers and tabs
- `open Chrome`, `open Edge`, `open Brave`, `open Firefox`, or `open Opera` opens that browser. Then `search for cats` searches in the browser you opened. You can name a browser directly with `search in Brave for cats` or `search for cats in Brave`.
- `search for ...` / `search ...` searches in the active browser, then the last browser opened, or Chrome if no browser is selected. `search it` / `search for it` uses the last text Astra typed.
- `new tab`, `close tab`, `next tab`, and `previous tab` (also `prev tab`) act on the active app. `reload`, `go back`, `go forward`, and `exit Chrome` control Chrome. `scroll up` and `scroll down` act on the active app.
- `type hello world` types the entire remainder literally into the focused field: for example, `type paste`, `type delete`, and `type new tab` type those words instead of running those commands. To run another action, say it in a separate utterance after typing. Say `type` by itself to have Astra prompt for text. `enter`, `press enter`, `hit enter`, or `submit` submits the focused field.

### Focused-app editing
These commands send keyboard shortcuts to the currently focused application or field, including Notepad:

- `select all` / `select everything`, then `delete`, removes the selected text in the focused app. `copy` and `cut` use the focused app selection; `paste` (also recognized as `payst` or `pay st`) inserts the current clipboard contents into the focused app. `undo` and `redo` also act on the focused app, including Notepad.
- `delete word` or `delete last three words` removes words from the end of a Chrome text field. Bare `delete` without a recent `select all` still asks which words to remove.

### Window and app controls
- `minimize` / `minimise` minimizes the active app. Add an app name to target it, such as `minimize Notepad` or `minimise Chrome`. `minimize all` minimizes visible application windows while leaving Astra available.
- `maximize Chrome` / `maximise Chrome` maximizes a named app. Astra restores a minimized window first, reports if it is already maximized, and says the app is not opened if no matching window is found.
- `open Recycle Bin`, `open Control Panel`, `open This PC`, `open Downloads`, `open Documents`, `open Pictures`, `open Music`, `open Videos`, `open Task Manager`, `open Calculator`, `open Paint`, and `open Snipping Tool` open common Windows locations and tools. `open files` / `open File Explorer`, `open Store`, and `open settings` are also supported.
- Say `open` followed by an installed app's name to launch it; Astra searches Start Menu shortcuts, registered app paths, and `PATH`. Say `close` followed by an app name to close its window.
- App name aliases include `VS` → Visual Studio; `VS Code`, `VSC`, or `vscode` → Visual Studio Code; `MS Word` → Word; `MS Excel` → Excel; `MS PowerPoint`, `MS PPT`, or `PPT` → PowerPoint; `MS Outlook` → Outlook; `MS OneNote` → OneNote; `MS Teams` → Teams; `VLC` → VLC Media Player; and `OBS` → OBS Studio.

### Chaining commands
Astra runs recognized commands in the order spoken. You can connect them with `and`, `then`, `and then`, `after that`, `afterwards`, `next`, `also`, `followed by`, `subsequently`, `finally`, `plus`, `but`, `or`, `so`, or `yet`. Recognized commands can also follow one another without a connector, such as `scroll down close tab`. Dictated phrases such as `search for cats and dogs` stay together unless the next phrase starts a recognized command.

- Drag the character, speech bubble, or blank parts of the overlay to move it. Astra stays within the current screen's usable area.
- Right-click the character for options. Use the tray menu > Exit to close Astra.

## Notes
- The first version uses an original holographic assistant graphic drawn in code.
- Astra uses an installed Windows voice with “Aria” in its name first, which avoids waiting for online speech synthesis. If Windows does not expose Aria to the app, Astra uses Microsoft's `en-US-AriaNeural` female voice through `edge-tts`. If neither can speak, the overlay displays the failure instead of switching to a different default voice. Online Aria needs internet access but no API key. [Microsoft lists Aria as a female voice](https://learn.microsoft.com/en-us/azure/ai-services/Speech-Service/language-support).
- The wake word and basic voice commands are enabled. Speech recognition uses Google's online service, so an internet connection and microphone access are required.
- Astra uses local preset replies for greetings, time, date, thanks, and common controls. It does not use an API key or generate open-ended answers. Use “search for …” to open Google results in the selected browser.
- Speech recognition is set to Indian English (`en-IN`). If you speak another English locale, change `RECOGNITION_LANGUAGE` near the top of `main.py` (for example, `en-US` or `en-GB`). For better recognition, select the correct Windows input microphone, allow microphone access in Windows Privacy settings, and speak clearly in a quiet room.
- To exit if needed, right-click the tray icon and choose Exit.

## Extending voice commands
Compound commands are split by registered command-start patterns. They can be chained with connectors such as “and,” “then,” “after that,” and “next,” or spoken as adjacent recognized commands. Dictated text stays together unless the following phrase starts a recognized command.
