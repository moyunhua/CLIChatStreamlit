import os
import sys
import shlex
import time
import threading
import queue
import platform
import subprocess
from pathlib import Path

import streamlit as st

APP_ROOT = Path(__file__).parent.resolve()
UPLOAD_DIR = APP_ROOT / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# --------------------- Helpers ---------------------

def is_windows() -> bool:
    return platform.system().lower().startswith("win")


def default_shell_and_args(cwd: Path):
    """Return (executable, args_list) for cross-platform shell execution.
    We will pass a single string to the shell's -Command/-c so that user input is executed in a shell context.
    """
    if is_windows():
        exe = "powershell.exe"
        # -NoProfile for speed; -ExecutionPolicy Bypass to allow scripts; -Command to run.
        base = [
            exe,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
        ]
        return exe, base
    else:
        # Prefer bash if available; fall back to sh
        exe = "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"
        base = [exe, "-lc"]  # login shell semantics for path/env
        return exe, base


def format_prompt(cwd: Path) -> str:
    home = Path.home()
    try:
        rel = f"~/{cwd.relative_to(home)}" if cwd.is_relative_to(home) else str(cwd)
    except Exception:
        rel = str(cwd)
    return rel


def init_state():
    if "messages" not in st.session_state:
        st.session_state.messages = []  # list of {role: 'user'|'assistant'|'system', 'content': str, 'meta': dict}
    if "cwd" not in st.session_state:
        st.session_state.cwd = str(APP_ROOT)
    if "prev_cwd" not in st.session_state:
        st.session_state.prev_cwd = None
    if "running" not in st.session_state:
        st.session_state.running = False
    if "proc" not in st.session_state:
        st.session_state.proc = None
    if "queue" not in st.session_state:
        st.session_state.queue = queue.Queue()
    if "stop_token" not in st.session_state:
        st.session_state.stop_token = threading.Event()


# --------------------- Process handling ---------------------

def enqueue_output(stream, q: queue.Queue, stop_evt: threading.Event):
    try:
        for line in iter(stream.readline, b""):
            if stop_evt.is_set():
                break
            if not line:
                break
            try:
                q.put(line)
            except Exception:
                break
    finally:
        try:
            stream.close()
        except Exception:
            pass


def run_command_stream(command_str: str, cwd: Path):
    """Start a shell subprocess that executes the user's command string and stream its output.
    Returns a generator yielding chunks as they become available.
    """
    exe, base = default_shell_and_args(cwd)

    if is_windows():
        # Ensure UTF-8 output for external commands and PowerShell itself
        prelude = (
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
            "$OutputEncoding=[System.Text.UTF8Encoding]::new(); "
        )
        shell_cmd = prelude + command_str
        popen_args = base + [shell_cmd]
        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
    else:
        # For bash/sh, ensure UTF-8 locale
        prelude = "export LANG=C.UTF-8; export LC_ALL=C.UTF-8;"
        shell_cmd = f"{prelude} {command_str}"
        popen_args = base + [shell_cmd]
        creationflags = 0

    # Environment: enforce UTF-8 where possible
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        popen_args,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=False,
        env=env,
        creationflags=creationflags,
    )

    st.session_state.proc = proc
    st.session_state.running = True

    q = st.session_state.queue
    stop_evt = st.session_state.stop_token
    stop_evt.clear()

    t = threading.Thread(target=enqueue_output, args=(proc.stdout, q, stop_evt), daemon=True)
    t.start()

    # Yield from queue
    try:
        while True:
            if stop_evt.is_set():
                break
            try:
                chunk = q.get(timeout=0.1)
                if chunk:
                    yield chunk
            except queue.Empty:
                if proc.poll() is not None:
                    # drain remaining
                    while True:
                        try:
                            chunk = q.get_nowait()
                            yield chunk
                        except queue.Empty:
                            break
                    break
                continue
    finally:
        # Wait for process to end if not already
        if proc.poll() is None:
            # Try graceful terminate, then force kill; also reap children if possible
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        # Best-effort: kill child processes using psutil if available
        try:
            import psutil  # type: ignore
            try:
                p = psutil.Process(proc.pid)
                for child in p.children(recursive=True):
                    try:
                        child.terminate()
                    except Exception:
                        pass
                _, alive = psutil.wait_procs(p.children(recursive=True), timeout=1)
                for a in alive:
                    try:
                        a.kill()
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass
        st.session_state.running = False
        st.session_state.proc = None

        # Report exit code
        rc = proc.returncode
        yield f"\n[exit {rc}]".encode()


# --------------------- Builtins ---------------------

def handle_builtin(cmd: str, cwd: Path):
    parts = shlex.split(cmd, posix=not is_windows()) if cmd.strip() else []
    if not parts:
        return None, cwd

    name = parts[0].lower()
    if name in {"cd"}:
        target = parts[1] if len(parts) > 1 else str(Path.home())
        if target == "-":
            # signal to switch to previous cwd; actual swap handled by caller
            return {"type": "cd-prev"}, cwd
        # Build new path
        cand = Path(target).expanduser()
        new_path = cand if cand.is_absolute() else (Path(cwd) / cand)
        new_path = new_path.resolve() if new_path.exists() else new_path
        if not new_path.exists():
            return {"type": "error", "message": f"cd: no such file or directory: {new_path}"}, cwd
        if not new_path.is_dir():
            return {"type": "error", "message": f"cd: not a directory: {new_path}"}, cwd
        return {"type": "cd", "to": str(new_path)}, new_path
    if name in {"pwd"}:
        return {"type": "pwd", "path": str(cwd)}, cwd
    if name in {"clear", "cls"}:
        return {"type": "clear"}, cwd

    return None, cwd


# --------------------- UI ---------------------

def main():
    st.set_page_config(page_title="CLI Chat", page_icon="🖥️", layout="wide")
    init_state()

    # Sidebar
    with st.sidebar:
        st.title("CLI Chat")
        st.caption("Chat-driven shell • Windows & Linux/macOS")
        st.divider()
        st.write("Working directory:")
        cwd = Path(st.session_state.cwd)
        st.code(str(cwd), language="bash")

        col1, col2 = st.columns([1,1])
        with col1:
            if st.session_state.running:
                if st.button("Stop", type="secondary", use_container_width=True):
                    st.session_state.stop_token.set()
            else:
                st.button("Stop", type="secondary", use_container_width=True, disabled=True)
        with col2:
            if st.button("Clear Chat", type="secondary", use_container_width=True):
                st.session_state.messages = []

        st.divider()
        up = st.file_uploader("Upload file(s)", accept_multiple_files=True)
        if up:
            saved_paths = []
            for f in up:
                dest = UPLOAD_DIR / f.name
                with open(dest, "wb") as out:
                    out.write(f.read())
                saved_paths.append(str(dest.resolve()))
                # Echo a message into chat with absolute path
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": f"📁 Uploaded to: {dest.resolve()}",
                    "meta": {"type": "upload", "path": str(dest.resolve())}
                })
            st.success(f"Saved {len(saved_paths)} file(s)")

    # Main chat area
    st.markdown(
        """
        <style>
        .stChatMessage pre, .stChatMessage code { font-size: 0.9rem; }
        .prompt { color: #7ee787; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Render history
    for m in st.session_state.messages:
        with st.chat_message(m.get("role", "assistant")):
            st.markdown(m.get("content", ""))

    # Input
    prompt = st.chat_input(f"{format_prompt(Path(st.session_state.cwd))} >")

    if prompt is not None:
        # Push user message
        st.session_state.messages.append({"role": "user", "content": prompt, "meta": {}})

        # Handle builtins first
        builtin, new_cwd = handle_builtin(prompt, Path(st.session_state.cwd))
        if builtin:
            if builtin["type"] == "cd":
                st.session_state.prev_cwd = st.session_state.cwd
                st.session_state.cwd = str(Path(builtin["to"]))
                with st.chat_message("assistant"):
                    st.markdown(f"Changed directory to `{st.session_state.cwd}`")
                st.rerun()
            elif builtin["type"] == "cd-prev":
                if st.session_state.prev_cwd:
                    current = st.session_state.cwd
                    st.session_state.cwd = st.session_state.prev_cwd
                    st.session_state.prev_cwd = current
                    with st.chat_message("assistant"):
                        st.markdown(f"Changed directory to `{st.session_state.cwd}`")
                else:
                    with st.chat_message("assistant"):
                        st.markdown("No previous directory.")
                st.rerun()
            elif builtin["type"] == "pwd":
                with st.chat_message("assistant"):
                    st.code(str(st.session_state.cwd))
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": f"`{st.session_state.cwd}`",
                    "meta": {"type": "pwd"}
                })
                st.rerun()
            elif builtin["type"] == "clear":
                st.session_state.messages = []
                st.rerun()
            elif builtin["type"] == "error":
                with st.chat_message("assistant"):
                    st.markdown(builtin.get("message", "Error"))
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": builtin.get("message", "Error"),
                    "meta": {"type": "error"}
                })
                st.rerun()
            return

        # Execute shell command with streaming output
        with st.chat_message("assistant"):
            out_area = st.empty()
            content = b""

            for chunk in run_command_stream(prompt, Path(st.session_state.cwd)):
                if isinstance(chunk, bytes):
                    content += chunk
                else:
                    content += str(chunk).encode()
                # Try to decode incrementally
                try:
                    rendered = content.decode("utf-8", errors="replace")
                except Exception:
                    # Fallback to system preferred encoding if something odd happens
                    import locale
                    rendered = content.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")
                out_area.code(rendered, language="bash")
                # Small yield to UI
                time.sleep(0.01)

            # Persist the final output to history
            try:
                final_text = content.decode("utf-8", errors="replace")
            except Exception:
                import locale
                final_text = content.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")
            st.session_state.messages.append({
                "role": "assistant",
                "content": f"```bash\n{final_text}\n```",
                "meta": {"type": "output"}
            })

        st.rerun()


if __name__ == "__main__":
    main()
