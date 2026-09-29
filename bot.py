#!/usr/bin/env python3
"""tg-voice-claude: talk to Claude Code from Telegram by voice.

Telegram voice note -> Groq Whisper (speech-to-text, any language)
  -> `claude -p` (Claude Code CLI, session resumed per chat)
  -> text reply + a short spoken summary via edge-tts (free, 300+ voices)

Design goals:
  * runs on a tiny VPS (1 vCPU / 1 GB): all heavy lifting is in the cloud
  * Python standard library only; edge-tts and ffmpeg are optional extras
  * one file, no framework, easy to read and audit
"""
import asyncio
import fcntl
import html
import json
import mimetypes
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("TVC_CONFIG", os.path.join(HERE, "config.json"))
STATE_PATH = os.environ.get("TVC_STATE", os.path.join(HERE, "state.json"))

POLL_TIMEOUT = 50          # Telegram long-poll seconds
TG_MSG_LIMIT = 4000        # Telegram hard limit is 4096
GROQ_API = "https://api.groq.com/openai/v1"
USER_AGENT = "tg-voice-claude/1.0"   # Groq's edge rejects the default urllib UA

# Environment variables that leak from an outer Claude Code session and would
# make the child `claude -p` think it is nested.
STRIP_ENV = (
    "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "AI_AGENT", "CLAUDE_EFFORT",
)

SPEAK_MARK = "🔊"
SPEAK_INSTRUCTION = (
    "You are being driven by voice from a phone. Keep answers concise. "
    "Reply in {lang}. At the very end of EVERY reply add one separate line "
    "that starts with \"{mark}\" followed by a spoken-style summary of your "
    "answer in {lang}, at most 80 characters, no markdown, no code, no URLs. "
    "That line is read aloud to the user."
)


def log(*a):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *a, flush=True)


# ---------------------------------------------------------------- config/state

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        sys.exit(f"config not found: {CONFIG_PATH} (copy config.example.json)")
    for key in ("bot_token", "groq_api_key", "allowed_users"):
        if not cfg.get(key):
            sys.exit(f"config.json is missing required field: {key}")
    cfg.setdefault("claude_bin", os.path.expanduser("~/.local/bin/claude"))
    cfg.setdefault("work_dir", os.path.expanduser("~"))
    cfg.setdefault("permission_mode", "acceptEdits")
    cfg.setdefault("model", "")
    cfg.setdefault("timeout_seconds", 900)
    cfg.setdefault("claude_extra_args", [])
    cfg.setdefault("claude_lock_file", "")      # shared with other bots
    cfg.setdefault("reply_language", "Chinese")
    cfg.setdefault("stt_model", "whisper-large-v3-turbo")
    cfg.setdefault("stt_language", "zh")        # "" = auto-detect
    cfg.setdefault("stt_translate", False)      # True = Whisper -> English
    cfg.setdefault("tts_enabled", True)
    cfg.setdefault("tts_voice", "zh-CN-XiaoxiaoNeural")
    cfg.setdefault("tts_rate", "+0%")
    cfg.setdefault("tts_max_chars", 400)
    # "voice": ogg voice bubble (tap to play). "video_note": round video with a
    # waveform, which Telegram clients autoplay with sound when it scrolls into view.
    cfg.setdefault("tts_format", "voice")
    cfg.setdefault("ffmpeg_bin", "ffmpeg")
    cfg.setdefault("echo_transcript", True)
    return cfg


_state_lock = threading.Lock()


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with _state_lock:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)


# ------------------------------------------------------------------ http utils

def multipart(fields, files):
    """Encode multipart/form-data with the standard library.

    fields: {name: str}; files: {name: (filename, bytes, content_type)}
    """
    boundary = "----tvc" + uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"{name}\"\r\n\r\n{value}\r\n").encode()
    for name, (fname, data, ctype) in files.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"{name}\"; filename=\"{fname}\"\r\n"
                 f"Content-Type: {ctype}\r\n\r\n").encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def http_json(url, data=None, headers=None, timeout=60):
    hdr = {"User-Agent": USER_AGENT}
    hdr.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# -------------------------------------------------------------------- telegram

class Telegram:
    def __init__(self, token):
        self.api = f"https://api.telegram.org/bot{token}"
        self.file_api = f"https://api.telegram.org/file/bot{token}"

    def call(self, method, **params):
        data = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}).encode()
        return http_json(f"{self.api}/{method}", data=data,
                         timeout=POLL_TIMEOUT + 20)

    def upload(self, method, field, path, content_type, **params):
        with open(path, "rb") as f:
            payload = f.read()
        body, ctype = multipart(
            {k: str(v) for k, v in params.items() if v is not None},
            {field: (os.path.basename(path), payload, content_type)})
        return http_json(f"{self.api}/{method}", data=body,
                         headers={"Content-Type": ctype}, timeout=120)

    def send(self, chat_id, text, reply_to=None):
        if not text:
            text = "(empty reply)"
        for chunk in split_text(text, TG_MSG_LIMIT):
            for attempt in range(3):
                try:
                    self.call("sendMessage", chat_id=chat_id, text=chunk,
                              reply_to_message_id=reply_to,
                              disable_web_page_preview="true")
                    break
                except urllib.error.HTTPError as e:
                    log(f"sendMessage failed ({e.code}): {e.read()[:200]}")
                    break
                except Exception as e:
                    log(f"sendMessage error: {e}")
                    time.sleep(1 + attempt)
            reply_to = None

    def action(self, chat_id, what="typing"):
        try:
            self.call("sendChatAction", chat_id=chat_id, action=what)
        except Exception:
            pass

    def download(self, file_id):
        info = self.call("getFile", file_id=file_id)
        path = info["result"]["file_path"]
        req = urllib.request.Request(f"{self.file_api}/{path}",
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read(), os.path.basename(path)


def split_text(text, limit):
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


# -------------------------------------------------------------- speech (Groq)

class Groq:
    def __init__(self, cfg):
        self.cfg = cfg
        self.headers = {"Authorization": "Bearer " + cfg["groq_api_key"]}

    ACCEPTED = ("flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "opus",
                "wav", "webm")

    def transcribe(self, audio, filename, language=None, translate=False):
        endpoint = "translations" if translate else "transcriptions"
        # Telegram voice notes arrive as .oga; Groq only accepts the
        # extensions listed above, so normalise the name before upload.
        stem, ext = os.path.splitext(filename)
        ext = ext.lstrip(".").lower()
        if ext not in self.ACCEPTED:
            filename = stem + ".ogg"
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        fields = {"model": self.cfg["stt_model"], "response_format": "json"}
        if language and not translate:
            fields["language"] = language
        body, ctype_hdr = multipart(fields, {"file": (filename, audio, ctype)})
        hdr = dict(self.headers, **{"Content-Type": ctype_hdr})
        r = http_json(f"{GROQ_API}/audio/{endpoint}", data=body,
                      headers=hdr, timeout=120)
        return (r.get("text") or "").strip()


# ----------------------------------------------------------- speech (edge-tts)

class TTS:
    def __init__(self, cfg):
        self.cfg = cfg
        self.available = False
        self.ffmpeg = None
        if not cfg["tts_enabled"]:
            return
        try:
            import edge_tts  # noqa: F401
            self.available = True
        except ImportError:
            log("edge-tts not installed; voice replies disabled "
                "(pip install edge-tts)")
            return
        ff = cfg["ffmpeg_bin"]
        for cand in (ff, os.path.expanduser("~/.local/bin/ffmpeg")):
            try:
                subprocess.run([cand, "-version"], capture_output=True,
                               timeout=10, check=True)
                self.ffmpeg = cand
                break
            except Exception:
                continue
        if not self.ffmpeg:
            log("ffmpeg not found; voice replies will be sent as MP3 audio "
                "instead of voice bubbles")

    def synth(self, text, voice=None):
        """Return (path, kind) where kind is 'voice' (ogg/opus) or 'audio' (mp3)."""
        import edge_tts
        voice = voice or self.cfg["tts_voice"]
        tmpdir = tempfile.mkdtemp(prefix="tvc-")
        mp3 = os.path.join(tmpdir, "reply.mp3")

        async def _run():
            com = edge_tts.Communicate(text, voice, rate=self.cfg["tts_rate"])
            await com.save(mp3)

        asyncio.run(_run())
        if not self.ffmpeg:
            return mp3, "audio"
        if self.cfg["tts_format"] == "video_note":
            mp4 = os.path.join(tmpdir, "reply.mp4")
            subprocess.run([self.ffmpeg, "-loglevel", "error", "-y", "-i", mp3,
                            "-filter_complex",
                            "[0:a]showwaves=s=384x384:mode=cline:rate=12:"
                            "colors=0x7FB3FF,format=yuv420p[v]",
                            "-map", "[v]", "-map", "0:a",
                            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "34",
                            "-r", "12", "-c:a", "aac", "-b:a", "48k",
                            "-shortest", "-t", "60", mp4],
                           check=True, timeout=120)
            os.unlink(mp3)
            return mp4, "video_note"
        ogg = os.path.join(tmpdir, "reply.ogg")
        subprocess.run([self.ffmpeg, "-loglevel", "error", "-y", "-i", mp3,
                        "-c:a", "libopus", "-b:a", "32k", "-vbr", "on",
                        "-application", "voip", ogg],
                       check=True, timeout=120)
        os.unlink(mp3)
        return ogg, "voice"


_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE = re.compile(r"`[^`]*`")
_URL = re.compile(r"https?://\S+")
_MD = re.compile(r"[*_#>|\\]+")


def extract_speech(text, max_chars):
    """Split a Claude reply into (display_text, text_to_speak).

    Prefers the trailing SPEAK_MARK line that the system prompt asks for;
    falls back to a cleaned-up prefix of the reply.
    """
    lines = text.rstrip().split("\n")
    for i in range(len(lines) - 1, max(-1, len(lines) - 4), -1):
        s = lines[i].strip()
        if s.startswith(SPEAK_MARK):
            spoken = s[len(SPEAK_MARK):].strip(" :：")
            display = "\n".join(lines[:i] + lines[i + 1:]).rstrip()
            if spoken:
                return display, spoken
    clean = _CODE_BLOCK.sub(" ", text)
    clean = _INLINE.sub(" ", clean)
    clean = _URL.sub(" ", clean)
    clean = _MD.sub(" ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return text, clean[:max_chars]


# ------------------------------------------------------------------ claude cli

class ClaudeRunner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None
        self.lock = threading.Lock()

    def build_env(self):
        env = os.environ.copy()
        for k in STRIP_ENV:
            env.pop(k, None)
        env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")
        return env

    def run(self, prompt, session_id, workdir, on_wait=None):
        cfg = self.cfg
        sys_prompt = SPEAK_INSTRUCTION.format(lang=cfg["reply_language"],
                                              mark=SPEAK_MARK)
        cmd = [cfg["claude_bin"], "-p", prompt,
               "--output-format", "json",
               "--permission-mode", cfg["permission_mode"],
               "--append-system-prompt", sys_prompt]
        if session_id:
            cmd += ["--resume", session_id]
        if cfg["model"]:
            cmd += ["--model", cfg["model"]]
        cmd += list(cfg["claude_extra_args"])

        lock_fd = None
        if cfg["claude_lock_file"]:
            lock_fd = os.open(cfg["claude_lock_file"], os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if on_wait:
                    on_wait()
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            return self._run(cmd, workdir)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)

    def _run(self, cmd, workdir):
        cfg = self.cfg
        os.makedirs(workdir, exist_ok=True)
        proc = subprocess.Popen(cmd, cwd=workdir, env=self.build_env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        with self.lock:
            self.proc = proc
        try:
            out, err = proc.communicate(timeout=cfg["timeout_seconds"])
        except subprocess.TimeoutExpired:
            self.kill()
            return {"error": f"timeout after {cfg['timeout_seconds']}s, killed"}
        finally:
            with self.lock:
                self.proc = None
        if proc.returncode == -signal.SIGTERM:
            return {"error": "cancelled"}
        stderr = err.decode("utf-8", "replace").strip()
        raw = out.decode("utf-8", "replace")
        try:
            data = json.loads(raw[raw.index("{"):])
        except (ValueError, IndexError):
            return {"error": f"claude returned non-JSON (exit={proc.returncode}):\n"
                             f"{stderr or raw[:800] or '(no output)'}"}
        if data.get("is_error") or data.get("subtype") != "success":
            reason = data.get("result") or data.get("api_error_status") or stderr
            return {"error": f"claude error: {reason}",
                    "session_id": data.get("session_id")}
        return {"text": data.get("result", ""),
                "session_id": data.get("session_id"),
                "cost": data.get("total_cost_usd", 0.0),
                "turns": data.get("num_turns", 0)}

    def kill(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                    return True
                except Exception as e:
                    log(f"kill failed: {e}")
        return False


# ------------------------------------------------------------------------- bot

HELP = """tg-voice-claude

Send a voice note or a text message and it goes to Claude Code.
Replies come back as text plus a short spoken summary.

/new        start a fresh Claude session
/status     session, work dir, settings
/tts on|off toggle spoken replies
/lang <code>  speech language for recognition (zh, en, ja ...; auto = detect)
/cd <path>  change work dir (resets the session)
/cancel     kill the running Claude task
/help       this text
"""


class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tg = Telegram(cfg["bot_token"])
        self.groq = Groq(cfg)
        self.tts = TTS(cfg)
        self.runner = ClaudeRunner(cfg)
        self.state = load_state()
        self.q = queue.Queue()
        self.busy = None

    def chat(self, chat_id):
        c = self.state.setdefault(str(chat_id), {})
        c.setdefault("session_id", None)
        c.setdefault("workdir", self.cfg["work_dir"])
        c.setdefault("tts", self.cfg["tts_enabled"])
        c.setdefault("stt_language", self.cfg["stt_language"])
        c.setdefault("total_cost", 0.0)
        return c

    # --- commands

    def handle_command(self, chat_id, text):
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        c = self.chat(chat_id)
        if cmd in ("/start", "/help"):
            self.tg.send(chat_id, HELP)
        elif cmd == "/new":
            c["session_id"] = None
            save_state(self.state)
            self.tg.send(chat_id, "New session started.")
        elif cmd == "/status":
            self.tg.send(chat_id, "\n".join([
                f"work dir:  {c['workdir']}",
                f"session:   {c['session_id'] or '(none)'}",
                f"tts:       {'on' if c['tts'] and self.tts.available else 'off'}"
                f" ({self.cfg['tts_voice']})",
                f"stt lang:  {c['stt_language'] or 'auto'} ({self.cfg['stt_model']})",
                f"cost:      ${c['total_cost']:.4f}",
                f"busy:      {self.busy or '-'}  queued: {self.q.qsize()}",
            ]))
        elif cmd == "/tts":
            if arg.lower() in ("on", "off"):
                c["tts"] = arg.lower() == "on"
                save_state(self.state)
            self.tg.send(chat_id, f"tts {'on' if c['tts'] else 'off'}")
        elif cmd == "/lang":
            if arg:
                c["stt_language"] = "" if arg.lower() == "auto" else arg.lower()
                save_state(self.state)
            self.tg.send(chat_id, f"stt language: {c['stt_language'] or 'auto'}")
        elif cmd == "/cd":
            if not arg:
                self.tg.send(chat_id, f"work dir: {c['workdir']}")
                return
            path = os.path.abspath(os.path.expanduser(arg))
            c["workdir"] = path
            c["session_id"] = None
            save_state(self.state)
            self.tg.send(chat_id, f"work dir: {path} (session reset)")
        elif cmd == "/cancel":
            self.tg.send(chat_id, "cancelled" if self.runner.kill()
                         else "nothing running")
        else:
            self.tg.send(chat_id, f"unknown command {cmd}. /help")

    # --- incoming

    def dispatch(self, msg, allowed):
        chat_id = msg.get("chat", {}).get("id")
        user = msg.get("from") or {}
        uid = user.get("id")
        if chat_id is None or uid is None:
            return
        if uid not in allowed:
            log(f"rejected user id={uid} name={user.get('username')!r}")
            self.tg.send(chat_id, f"Not authorized. Your user id is {uid}; "
                                  f"add it to allowed_users in config.json.")
            return
        text = msg.get("text")
        media = msg.get("voice") or msg.get("audio") or msg.get("video_note")
        if text and text.startswith("/"):
            try:
                self.handle_command(chat_id, text)
            except Exception as e:
                log(f"command error: {e!r}")
                self.tg.send(chat_id, f"command error: {e!r}")
            return
        if not text and not media:
            self.tg.send(chat_id, "Send a voice note or text.")
            return
        self.q.put((chat_id, msg.get("message_id"), text, media))
        if self.busy:
            self.tg.send(chat_id, f"queued (running: {self.busy})")

    # --- worker

    def _keep_action(self, chat_id, what, stop):
        while not stop.is_set():
            self.tg.action(chat_id, what)
            stop.wait(4)

    def worker(self):
        while True:
            chat_id, msg_id, text, media = self.q.get()
            try:
                self.process(chat_id, msg_id, text, media)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                log(f"worker HTTP {e.code} from {e.url}: {body}")
                self.tg.send(chat_id, f"error: HTTP {e.code}: {body}")
            except Exception as e:
                log(f"worker error: {e!r}")
                self.tg.send(chat_id, f"error: {e!r}")
            finally:
                self.busy = None
                self.q.task_done()

    def process(self, chat_id, msg_id, text, media):
        c = self.chat(chat_id)
        if media:
            self.busy = "transcribing"
            self.tg.action(chat_id, "typing")
            audio, fname = self.tg.download(media["file_id"])
            text = self.groq.transcribe(audio, fname,
                                        language=c["stt_language"],
                                        translate=self.cfg["stt_translate"])
            if not text:
                self.tg.send(chat_id, "(no speech recognized)", reply_to=msg_id)
                return
            if self.cfg["echo_transcript"]:
                self.tg.send(chat_id, f"🎤 {text}", reply_to=msg_id)
        self.busy = text[:60].replace("\n", " ")

        stop = threading.Event()
        threading.Thread(target=self._keep_action,
                         args=(chat_id, "typing", stop), daemon=True).start()
        started = time.time()
        try:
            res = self.runner.run(
                text, c["session_id"], c["workdir"],
                on_wait=lambda: self.tg.send(
                    chat_id, "waiting for another Claude task to finish..."))
        finally:
            stop.set()
        elapsed = time.time() - started

        if res.get("session_id"):
            c["session_id"] = res["session_id"]
        if res.get("cost"):
            c["total_cost"] = c.get("total_cost", 0.0) + res["cost"]
        save_state(self.state)

        if "error" in res:
            self.tg.send(chat_id, f"⚠️ {res['error']}")
            return
        display, spoken = extract_speech(res["text"], self.cfg["tts_max_chars"])
        will_speak = c["tts"] and self.tts.available and spoken
        if spoken and (not will_speak or self.cfg["tts_format"] == "video_note"):
            # no caption on video notes: keep the summary readable in the text
            display = f"{display}\n\n{SPEAK_MARK} {spoken}".strip()
        self.tg.send(chat_id, display)
        log(f"chat={chat_id} turns={res.get('turns')} cost=${res.get('cost', 0):.4f} "
            f"elapsed={elapsed:.0f}s")

        if will_speak:
            self.busy = "speaking"
            stop = threading.Event()
            threading.Thread(target=self._keep_action,
                             args=(chat_id, "record_voice", stop),
                             daemon=True).start()
            try:
                path, kind = self.tts.synth(spoken)
            except Exception as e:
                log(f"tts failed: {e!r}")
                return
            finally:
                stop.set()
            try:
                if kind == "video_note":
                    self.tg.upload("sendVideoNote", "video_note", path,
                                   "video/mp4", chat_id=chat_id, length=384)
                elif kind == "voice":
                    self.tg.upload("sendVoice", "voice", path, "audio/ogg",
                                   chat_id=chat_id, caption=spoken[:1000])
                else:
                    self.tg.upload("sendAudio", "audio", path, "audio/mpeg",
                                   chat_id=chat_id, caption=spoken[:1000],
                                   title="Claude")
            except urllib.error.HTTPError as e:
                log(f"send voice failed ({e.code}): {e.read()[:200]}")
            finally:
                try:
                    os.unlink(path)
                    os.rmdir(os.path.dirname(path))
                except OSError:
                    pass

    # --- main loop

    def run(self):
        threading.Thread(target=self.worker, daemon=True).start()
        allowed = set(int(u) for u in self.cfg["allowed_users"])
        me = self.tg.call("getMe")["result"]
        log(f"started as @{me.get('username')}; allowed users: {sorted(allowed)}; "
            f"tts={'on' if self.tts.available else 'off'} "
            f"ffmpeg={'yes' if self.tts.ffmpeg else 'no'}")
        offset = None
        while True:
            try:
                r = self.tg.call("getUpdates", offset=offset, timeout=POLL_TIMEOUT,
                                 allowed_updates=json.dumps(["message"]))
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    log("409: another instance is polling this token")
                    time.sleep(10)
                else:
                    log(f"getUpdates HTTP {e.code}")
                    time.sleep(5)
                continue
            except Exception as e:
                log(f"getUpdates failed: {e}")
                time.sleep(5)
                continue
            if not r.get("ok"):
                log(f"getUpdates not ok: {r}")
                time.sleep(5)
                continue
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                self.dispatch(upd.get("message") or {}, allowed)


if __name__ == "__main__":
    Bot(load_config()).run()
