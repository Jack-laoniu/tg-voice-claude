# tg-voice-claude

Talk to [Claude Code](https://code.claude.com) from your phone by voice, through Telegram.

```
Telegram voice note ──▶ Groq Whisper (speech → text, 99 languages, free tier)
                    ──▶ claude -p   (Claude Code CLI, session resumed per chat)
                    ──▶ text reply + a short spoken summary (edge-tts, free)
```

Why this exists: Claude Code's built-in `/voice` needs a local microphone,
does not work over SSH, and does not support Chinese. This bridge works from
any phone, in any language Whisper knows, against a Claude Code running on a
remote box. It was written for Chinese first, but every language setting is a
config field.

- **Tiny footprint.** Runs happily on a 1 vCPU / 1 GB VPS: recognition and
  synthesis happen in the cloud, nothing is loaded locally.
- **No framework.** One Python file, standard library only. `edge-tts` and
  `ffmpeg` are optional extras for spoken replies.
- **Session memory.** Each Telegram chat maps to one Claude Code session that
  is resumed on every message. `/new` starts over.
- **Spoken summary, not a code dump.** Claude is asked to end every reply with
  one spoken-style line; only that line is turned into audio. The full reply
  still arrives as text.
- **Plays nice with other bots.** An optional file lock serialises Claude runs
  across several bots on the same small machine.

[中文说明](#中文说明) below.

## Requirements

- Python 3.9+
- [Claude Code CLI](https://code.claude.com/docs/en/quickstart) installed and
  logged in on the machine that runs the bot
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A [Groq API key](https://console.groq.com/keys) (free tier: 8 hours of audio
  per day, no card needed)
- Optional: `pip install edge-tts` for spoken replies, and `ffmpeg` on PATH to
  send them as Telegram voice bubbles (without ffmpeg they are sent as MP3
  audio files)

## Setup

```bash
git clone https://github.com/<you>/tg-voice-claude.git ~/tg-voice-claude
cd ~/tg-voice-claude
cp config.example.json config.json
chmod 600 config.json
pip install --user edge-tts          # optional, for voice replies
```

Edit `config.json`:

| Field | Meaning |
|---|---|
| `bot_token` | from @BotFather |
| `groq_api_key` | from console.groq.com/keys |
| `allowed_users` | **required.** Telegram numeric user ids allowed to talk to the bot. Send any message to the bot to learn yours; get it from [@userinfobot](https://t.me/userinfobot). |
| `work_dir` | directory Claude Code runs in (`/cd` changes it per chat) |
| `permission_mode` | passed to `claude --permission-mode`; `acceptEdits` by default |
| `model` | empty = account default; or `sonnet`, `opus`, ... |
| `claude_extra_args` | extra CLI args, e.g. `["--settings", "/path/sandbox.json"]` |
| `claude_lock_file` | if set, Claude runs take an `flock` on this path; use the same path in other bots to avoid running two Claude processes at once |
| `reply_language` | language Claude is asked to answer in |
| `stt_model` | `whisper-large-v3-turbo` (fast) or `whisper-large-v3` (most accurate) |
| `stt_language` | ISO code hint for recognition, `""` = auto-detect. `/lang` changes it per chat |
| `stt_translate` | `true` sends audio to Whisper's translate endpoint and hands Claude English text |
| `tts_enabled`, `tts_voice`, `tts_rate` | spoken replies. List voices with `edge-tts --list-voices` |
| `tts_max_chars` | fallback length when Claude did not produce a summary line |
| `tts_format` | `video_note` (default in the example): a round video message with a waveform, which Telegram **autoplays with sound**. `voice`: a normal voice bubble, tap to play. Both need ffmpeg |
| `echo_transcript` | send the recognised text back before running Claude |
| `brief_default` | start every chat in brief mode (`/brief` toggles it per chat) |
| `spoken_max_chars` | length Claude is asked to keep the spoken line under in normal mode (80) |
| `brief_max_chars` | brief mode (300): Claude is told the whole reply is read aloud and must be plain spoken prose under this length |
| `brief_style` | extra instruction for brief mode; default asks Claude to talk like a thinking partner: point, two or three directions, one question back |

Run it:

```bash
python3 bot.py
```

Then open the bot in Telegram and hold the microphone button.

### Run as a service

```bash
sudo cp tg-voice-claude.service /etc/systemd/system/tg-voice-claude@.service
sudo systemctl enable --now tg-voice-claude@$USER
journalctl -u tg-voice-claude@$USER -f
```

The unit assumes the checkout lives at `/home/<user>/tg-voice-claude`; edit
`WorkingDirectory` and `ExecStart` if yours is elsewhere.

## Commands

| Command | Effect |
|---|---|
| voice note / text | goes to Claude Code, context continues |
| `/new` | fresh Claude session |
| `/status` | session id, work dir, TTS and STT settings, cost so far |
| `/tts on\|off` | toggle spoken replies for this chat |
| `/brief on\|off` | reply with the one-line summary only, as text, no audio. See [Hands-free with AirPods](#hands-free-with-airpods) |
| `/lang zh\|en\|ja\|auto` | recognition language for this chat |
| `/cd <path>` | change work dir (resets the session) |
| `/cancel` | kill the running Claude task |

## Autoplay

Telegram never autoplays voice bubbles; the user has to tap. Round video
messages, however, autoplay with sound as soon as they scroll into view. With
`tts_format: "video_note"` the bot renders the synthesised speech into a
384x384 MP4 with a live waveform (ffmpeg, about 0.2 s on one core) and sends it
with `sendVideoNote`, so the answer starts speaking the moment you open the
chat. Set `tts_format: "voice"` if you prefer the classic voice bubble.

## Hands-free with AirPods

Telegram will not autoplay audio from a bot, but on iPhone with AirPods the
system can do the whole loop for you, no audio files involved:

1. **Hear replies.** iOS Settings → Notifications → Announce Notifications →
   on, and allow Telegram. In Telegram: Settings → Notifications and Sounds →
   Announce Messages with Siri. With the phone locked and AirPods in, Siri
   reads every incoming message from the bot aloud and offers to reply.
2. **Keep them short.** Send `/brief on` to the bot. From then on each reply
   is a single short text message (the spoken summary), which is what you want
   read into your ear. `/brief off` restores full replies plus audio.
3. **Send by voice.** Add a contact for the bot so Siri can address it:
   Contacts → new contact → name it something short like "Claude" → add url →
   set the label to `Telegram` → value `https://t.me/@oid<PEER_ID>` where
   `<PEER_ID>` is the number before the colon in your bot token. Then say
   "Hey Siri, message Claude on Telegram" and dictate. Siri's own dictation
   does the recognition, so this path does not even touch Groq.

## How the spoken summary works

The bot appends a system prompt asking Claude to finish every reply with one
line that starts with `🔊`, at most 80 characters, no markdown. That line is
stripped from the text message, synthesised with edge-tts, and sent as a voice
bubble with the same text as its caption. If Claude forgets the line, the bot
falls back to reading the first `tts_max_chars` of the reply with code blocks,
URLs and markdown removed.

## Security notes

- `allowed_users` is the only gate. Leave it empty and the bot refuses to start.
- Claude Code runs with whatever permissions the CLI has on that machine. Use
  `claude_extra_args` to pass a `--settings` file with a sandbox and deny
  rules; keep `config.json` (it holds two secrets) in a directory Claude is
  not allowed to read.
- Audio goes to Groq for recognition; the summary line goes to Microsoft's
  Edge TTS endpoint for synthesis. Nothing else leaves the machine.

## License

MIT

---

## 中文说明

在手机上用 Telegram 语音直接指挥远程机器上的 Claude Code。

链路：Telegram 语音 → Groq Whisper 识别 → `claude -p` → 文字回复 + 一句口播摘要（edge-tts 合成）。

为什么不用 Claude Code 自带的 `/voice`：它只能在本机有麦克风的终端里用，SSH 不行，也不支持中文。这个 bridge 在任何手机上都能用，识别语言随意，Claude 用中文回答。

### 安装

```bash
git clone https://github.com/<you>/tg-voice-claude.git ~/tg-voice-claude
cd ~/tg-voice-claude
cp config.example.json config.json && chmod 600 config.json
pip install --user edge-tts       # 可选，语音回复用
```

填 `config.json`：

- `bot_token`：Telegram 里找 @BotFather 发 `/newbot` 拿到
- `groq_api_key`：console.groq.com/keys 创建，免费额度每天 8 小时音频
- `allowed_users`：**必填**，你的 Telegram 数字 id，找 @userinfobot 要
- `work_dir`：Claude 的工作目录
- 其余字段见上方英文表格

启动：`python3 bot.py`，或按上文装成 systemd 服务。

### 中文相关默认值

- `stt_language` 默认 `zh`，说中文识别最准；`/lang auto` 切自动检测，`/lang en` 说英文
- `reply_language` 默认 `Chinese`，Claude 用中文回答
- `tts_voice` 默认 `zh-CN-XiaoxiaoNeural`，其它音色用 `edge-tts --list-voices | grep zh-CN` 看
- `tts_format` 默认 `video_note`：Telegram 不会自动播放语音气泡，但圆形视频消息进入视野就自动出声，所以 bot 把语音渲染成带波形的圆形视频发出。想要普通语音气泡就改成 `voice`

### AirPods 免手模式

Telegram 不会自动播放 bot 发的音频，但 iPhone 配 AirPods 可以让系统把整个循环包掉，完全不碰手机：

1. **听回复**：iOS 设置 → 通知 → 通过 Siri 播报通知 → 打开并允许 Telegram；Telegram 里 设置 → 通知和声音 → 用 Siri 播报消息。锁屏戴着 AirPods 时，bot 的每条消息 Siri 都会读出来，并问你要不要回复。
2. **让回复短一点**：给 bot 发 `/brief on`，之后每次只回一条短文字（就是口播摘要），Siri 念的就是这一句。`/brief off` 恢复完整回复加语音。
3. **用 Siri 发消息**：通讯录新建联系人，名字起短一点比如"Claude"，添加 URL，标签改成 `Telegram`，内容填 `https://t.me/@oid<PEER_ID>`，PEER_ID 是 bot token 冒号前面那串数字。然后说"嘿 Siri，用 Telegram 给 Claude 发消息"，直接口述。这条路走的是 Siri 自己的听写，连 Groq 都不经过。

### 口播摘要

Claude 每次回复末尾会附一行以 🔊 开头、不超过 80 字的口语摘要，bot 只把这一行合成语音，完整回复照常以文字发出。代码不会被念出来。

### 安全

`allowed_users` 是唯一的门。Claude 的权限就是那台机器上 CLI 的权限，建议用 `claude_extra_args` 传一个带沙箱和 deny 规则的 `--settings` 文件，并且把存有两个密钥的 `config.json` 放在 Claude 读不到的目录。
