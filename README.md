# 🤖 Gemma 4 Discord Bot

A fun Discord bot for chatting with server members, running fully on one PC.

- **Brain:** local `gemma4-12b-bionic-v2` (with vision) in LM Studio
- **Drawing:** ComfyUI on the same PC, taking turns with Gemma on the graphics card
- **Search:** free DuckDuckGo (no API key), with backup engines
- **Privacy:** nothing from Discord is saved to disk

<br>

## 💬 Commands

Every command works in **any channel**, in two forms:

- a slash command: `/draw a cat`
- one line of text: `!draw a cat` (also `！draw` or a pasted `/draw` line)

Fields are split with `|`, e.g. `!poll Dinner tonight? | fried rice, noodles`.

<br>

### Chat

| Command | What it does |
|---|---|
| `/ask` · `!ask` | Ask the bot anything |
| `@bot` | Same as `/ask`, in any channel |
| `/search` · `!search` | Search the web first, then answer |
| `/comment` · `!comment` | Talk to members only: bot doesn't reply or remember |
| `/reset` · `!reset` | Clear this channel's memory |
| `/persona` · `!persona` | Change personality (preset, custom or a known character) |

<br>

### Drawing

| Command | What it does |
|---|---|
| `/draw` · `!draw` | Draw a picture (`!draw realistic ...` for photo style) |
| `/refine` · `!refine` | Change your last picture |
| `/recall` · `!recall` | Undo your newest picture |
| `/edit` · `!edit` | Change an uploaded picture, or swap in a character |
| `/drawstyle` · `!style` | Switch this channel to anime or realistic |
| `/dmdraw` `/dmedit` `/dmrefine` `/dmrecall` | Private versions: request hidden, picture by DM |

<br>

### Tools

| Command | What it does |
|---|---|
| `/poll` · `!poll` | Discord poll; ends when all named members voted |
| `/event` · `!event` | Server event; `!event` alone opens a form |
| `/status` · `!status` | Live bars for GPU, CPU and RAM |

> Full Chinese command list for members: [`docs/指令.md`](docs/指令.md)

<br>

## 🧠 How chatting works

### Where it answers

- **Bot channels** (`BOT_CHANNEL_ID`): every message
- **Other channels:** only commands and @mentions

<br>

### Memory

- Last **30 messages** per channel, in RAM only
- Gone on restart or `/reset`

<br>

### Web search

- Runs by itself for time-sensitive questions (today, latest, price, weather ...), in English or Chinese
- Results are used for one answer, never saved

<br>

### Personas

| Type | Example |
|---|---|
| Preset | `!persona pirate` (buddy, tsundere, wuxia, pirate, roast) |
| Custom | `!persona custom a grumpy cat who loves fish` |
| Character | `!persona character Sherlock Holmes` |

- Characters are looked up on the web: personality, teammates, rivals, abilities
- They look facts up again when asked, and refresh who they are every 8 messages
- Switching clears the channel's memory; a restart goes back to buddy

<br>

### Extras

- Uses the server's own **emoji** now and then, learning what each one means
- **Stickers** are off by default (`STICKERS=on` turns them on)
- Reply length is capped at a random 10 / 30 / 50 / 100 (not for search answers)

<br>

## 🎨 How drawing works

### The flow

1. Gemma writes the picture prompt (looks up named characters on the web first)
2. Gemma is unloaded from the graphics card
3. ComfyUI draws the picture
4. Gemma is loaded back and the picture is posted

> While drawing, the bot answers only picture requests and `!status`.

<br>

### Queue

- One picture per member at a time, first come, first served
- The notice shows the queue, an **estimated wait**, a countdown and progress
- Each member has their own last 5 pictures per channel (private ones kept apart)

<br>

### Editing pictures

| Action | How much is redrawn |
|---|---|
| Small change (expression, lighting) | 55% |
| Medium change (hairstyle, clothes, background) | 60% |
| Big change (hair colour, pose, framing) | 75% |
| New colour on a big area, with ControlNet | 90%, following the outlines |
| Character swap (up to 3 character pictures) | 75%, or 90% with ControlNet |

- Things you didn't mention keep their exact tags and weights
- `!recall` drops the newest picture; the next refine tries a new seed

> Full pipeline details: [`docs/版本更新内容（已归档和刚完成）.md`](docs/版本更新内容（已归档和刚完成）.md)

<br>

### Styles

| Style | Model | Notes |
|---|---|---|
| anime (default) | `SD_CHECKPOINT` | e.g. animagine-xl-4.0 |
| realistic | `SD_CHECKPOINT_REALISTIC` | e.g. Juggernaut XL; anime characters become cosplay |

<br>

### Hard limits

- No sexual pictures of anyone who is or looks under 18
- No sexual or degrading pictures of real people

<br>

## 🔒 Privacy

- No messages, images or conversation logs are saved
- Pictures go ComfyUI → bot RAM → Discord (never to disk)
- Logs never contain message content or search terms

<br>

## ⚙️ Setup (Windows)

### 1. LM Studio

- Load `gemma4-12b-bionic-v2`, then run `lms server start`
- Context length: `16384` or more

<br>

### 2. Discord bot

- Developer Portal → New Application → Bot → turn on **Message Content Intent** → copy the token
- OAuth2 URL scopes: `bot`, `applications.commands`
- Permissions: Send Messages, Read Message History, Send Polls, Create Events
- Also for `!comment` and `!dm…`: Manage Messages, Manage Webhooks

<br>

### 3. Install

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

- Put the token in `.env` only (never in chat or commits)
- Start: `python bot.py` (slash commands may take a few minutes to appear)

<br>

### 4. Drawing (optional)

- Install ComfyUI and put a model in `models/checkpoints`
- Or set `COMFYUI_DIR` and the bot starts ComfyUI itself
- The `lms` command must work in the bot's terminal
- Restart ComfyUI once after `comfy_node.py` changes

<br>

### `.env` keys

| Key | Purpose |
|---|---|
| `DISCORD_TOKEN` | Bot token (required) |
| `LMSTUDIO_BASE_URL` · `LMSTUDIO_MODEL` | LM Studio server and model |
| `BOT_CHANNEL_ID` | Channels answered in full (one line, comma-separated) |
| `ALLOWED_USER_IDS` | Limit who can use the bot (empty = everyone) |
| `UNFILTERED_MODE` | `on` = looser replies (private servers only) |
| `STICKERS` | `on` = sometimes send server stickers |
| `MONITOR_WINDOW` | `off` = no PC monitor window |
| `IMAGE_GEN` | `on` = drawing commands |
| `COMFYUI_URL` · `COMFYUI_DIR` | ComfyUI address and folder |
| `SD_CHECKPOINT` · `SD_CHECKPOINT_REALISTIC` | Anime and realistic models |
| `SD_CONTROLNET` | Optional canny ControlNet (SDXL) for colour changes |
| `IMAGE_SIZE` | `1024` for SDXL, `512` for SD 1.5 |
| `LMSTUDIO_CONTEXT` | Context Gemma is reloaded with (`16384` or more) |

<br>

## 🧩 Optional extras

### Monitor window

- Small always-on-top window with the `/status` numbers
- Closes with the bot; `MONITOR_WINDOW=off` turns it off

<br>

### Unfiltered mode

- `UNFILTERED_MODE=on`: swearing, dark humour, mature topics, fewer refusals
- Hard limits stay: nothing sexual with minors, no harm instructions, no doxxing

<br>

### Emoji meanings by hand

Create `emoji_meanings.txt` next to `bot.py` (gitignored), one line per emoji:

```
catstare: speechless at nonsense
awkward_girl: awkward
```

<br>

## 🧪 Tests

```
pip install -r requirements-dev.txt
python -m pytest
```

- Runs against a mock LM Studio server: no Discord, LM Studio or GPU needed
