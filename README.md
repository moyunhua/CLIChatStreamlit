# CLI Chat (Streamlit)

A cross-platform CLI-over-chat app built with Streamlit. Chat like SSH: run shell commands, upload files, and see streaming output. Works on Windows (PowerShell) and Linux/macOS (bash/sh).

## Features
- Chat UI (`st.chat_message`) with persistent history
- Cross-platform command execution
  - Windows: PowerShell (v5+)
  - Linux/macOS: bash/sh
- Streaming stdout/stderr with exit code
- Built-ins: `cd`, `pwd`, `cls/clear` (clear chat), `cwd` display
- File upload; saved under `uploads/` and absolute path echoed into chat
- Cancel a running command (best-effort)
- Theming similar to modern terminals

## Quickstart

### 1) Install dependencies

```powershell
python -m venv .venv
. .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

On Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) Run the app

```powershell
streamlit run app.py
```

### 3) Use it
- Type commands like `pip --version`, `python --version`, `python script.py`, etc.
- Use `cd path/to/dir` to change working directory.
- Upload a file via the paperclip; its absolute path will be printed.
- Click Stop to cancel a long-running command.

## Notes
- For Windows, execution uses `powershell.exe`. The app detects the OS and uses the appropriate shell.
- Cancellation is best-effort; some processes may ignore termination.
- Uploaded files are stored in `uploads/` relative to the project root.
