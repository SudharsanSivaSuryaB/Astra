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
- Voice-controlled Chrome: open, search in Google, reload, navigate back or forward, and exit
- Uses the installed Windows Aria voice first for low-latency speech, with Microsoft's online Aria voice as a fallback
- Microphone input and speech recognition through SpeechRecognition and Google Speech Recognition (internet required)
- Tray menu to show or exit

## Requirements
- Windows 10/11
- Python 3.10 or newer
- Internet connection only to install Python packages

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

## Controls
- Drag the character or blank parts of the overlay to move it.
- Say “Astra” to wake the assistant, then give a command, or say a command in the same phrase (for example, “Astra, what time is it?”). Wake detection also accepts “Astro”, “a star”, and “a straw”.
- After waking, continue speaking naturally without repeating “Astra”. Conversation mode returns to wake-word listening after 30 seconds of silence; say “Astra” again before other commands after that.
- Say “goodbye” at any time to hear a farewell and close the app. “Astra, hide” hides it; “Astra, show” restores it.
- Say “Astra, open Notepad” to open it without dictating. Say “take notes” to start typing dictated speech. Say “stop notes” (also recognized as “staff notes”, “top notes”, or “end notes”) to pause dictation while keeping Notepad open; say “take notes” again to resume. Say “exit notes” or “exit Notepad” to close Notepad and return to conversation mode. Say “save notes” to save and keep Notepad open; for a new document, dictate a filename when Astra asks. If exiting prompts to save, say “select save” and then dictate the filename; say “don't save” to discard or “cancel save” to keep editing.
- Say “open Chrome” to launch Chrome. Say “search for weather tomorrow” (or “search weather tomorrow”) to open Google results in Chrome. Focus a text field in Chrome, then say “type hello world” to enter text; or say “type” and dictate the text when Astra asks. Say “delete” and then say the words to remove from the end of the field, or say “delete last three words”; “delete word” removes one previous word. Say “enter” after typing to submit the field. Say “reload” to reload the current tab, “go back” or “go forward” to navigate its history, “new tab” or “close tab” to manage tabs, and “scroll down” or “scroll up” to move through the page. Say “exit Chrome” to close the Chrome window.
- There are no on-screen control buttons. If hidden, say “Astra, show” or use the tray icon menu > Show companion.
- Right-click the character for options.
- Use the tray menu > Exit to close it.

## Notes
- The first version uses an original holographic assistant graphic drawn in code.
- Astra uses an installed Windows voice with “Aria” in its name first, which avoids waiting for online speech synthesis. If Windows does not expose Aria to the app, Astra uses Microsoft's `en-US-AriaNeural` female voice through `edge-tts`. If neither can speak, the overlay displays the failure instead of switching to a different default voice. Online Aria needs internet access but no API key. [Microsoft lists Aria as a female voice](https://learn.microsoft.com/en-us/azure/ai-services/Speech-Service/language-support).
- The wake word and basic voice commands are enabled. Speech recognition uses Google's online service, so an internet connection and microphone access are required.
- Astra uses local preset replies for greetings, time, date, thanks, and common controls. It does not use an API key or generate open-ended answers.
- Speech recognition is set to Indian English (`en-IN`). If you speak another English locale, change `RECOGNITION_LANGUAGE` near the top of `main.py` (for example, `en-US` or `en-GB`). For better recognition, select the correct Windows input microphone, allow microphone access in Windows Privacy settings, and speak clearly in a quiet room.
- To exit if needed, right-click the tray icon and choose Exit.
