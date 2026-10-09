# Project notes (for Claude sessions)

Any Claude session (new or old) reads this file first. It holds only the rules and the setup; everything else lives in five files under `docs/`.

Last updated 2026-10-09

## Where things are (read in this order)
1. **`docs/目前进度.md`** - current state, next steps, open issues, tests still to run, session handoffs. **Read every time.** **Update it after every prompt you finish** (completed or half-done), and push, so an interrupted session can be resumed cheaply.
2. **`docs/问题踩坑复利.md`** - solved problems per version (problem -> cause -> fix). Read once; later only new entries, or when Ep mentions a similar problem. Add every newly solved problem.
3. **`docs/版本更新内容（已归档和刚完成）.md`** - what each version added, how Ep tests and releases, how the image pipeline works, and the **latest member announcement** (only the newest one is kept). Read once; later only new parts. New work goes under the next version (v1.5).
4. **`docs/指令.md`** - the current full command list, ready to paste into Discord. Edit single lines when commands change; never regenerate it.
5. **`docs/计划文件.md`** - Ep's plans: 【/】 done, 【】 not yet. Add every new plan Ep mentions.

Keep these files current as you learn and build more: they are how sessions reuse past experience instead of rediscovering it. Don't let this file grow again; put state, history and fixes in the files above.

## Owner and language
- Owner: Ep (GitHub GBF9397). Repo: GBF9397/CC_DISCORD_BOT.
- Talk to Ep in Chinese: short, conclusion first, one concise step list rather than long explanations.
- Work on branches. Ask Ep before pushing to main or merging.

## What the bot is
- Python Discord bot for fun. Chat answers come from a local model **gemma4-12b-bionic-v2** (has vision) in LM Studio (`http://localhost:1234/v1`, OpenAI-compatible, localhost only). Web search uses free DuckDuckGo (no API key) with backup engines.
- Image generation uses ComfyUI (portable) on the same PC; Gemma and ComfyUI take turns on the GPU (RTX 5060 Ti 16GB, 32GB RAM) via `lms unload` / `lms load`. Plan features within what this PC can run.
- Models: anime `animagine-xl-4.0`, realistic `Juggernaut-XL_v9_RunDiffusionPhoto_v2`; optional canny ControlNet (`SD_CONTROLNET`).
- The bot only runs on Ep's PC. Cloud sessions cannot run it (no GPU, no LM Studio, DuckDuckGo blocked by the proxy): run the unit tests only (`pip install -r requirements.txt -r requirements-dev.txt`, then `python -m pytest`). Tests use a mock LM Studio server.

## Ep's PC setup (Windows)
- Bot folder: `C:\Bionic Workspace\001_Testing\CC_DISCORD_BOT` (git clone, `.venv` and `.env` next to `bot.py`).
- ComfyUI: `C:\Codex Workspace\003_CASUAL_CREATIVES\_LOCAL_AI_WORK\Software\ComfyUI_windows_portable` (start with `run_nvidia_gpu.bat`, or the bot starts it when `COMFYUI_DIR` is set). Restart ComfyUI once after `comfy_node.py` changes.
- LM Studio context length must be 16384+; give the model max_tokens >= 512 (it reasons first) and read only `message.content`.
- Hidden start, from the bot folder:
  `Start-Process .venv\Scripts\python.exe bot.py -WindowStyle Hidden -RedirectStandardError bot-error.log`
- Stop: `Stop-Process -Name python` (also closes the monitor window and a bot-started ComfyUI). Run only one copy. `pythonw.exe` does NOT work.
- `.env` is read only at startup; restart after edits. Auto-reply channels: `BOT_CHANNEL_ID` (numeric IDs, one line, comma-separated). Other channels only answer commands and @mentions.
- Ep's update routine: `Stop-Process -Name python` -> `git fetch` -> `git checkout <branch>` (or main) -> `git pull` -> hidden start -> `git log --oneline -1`.

## Hard rules (never break)
1. **Every command works in two forms in ANY channel**: the `/` slash command and a one-line `!` text command (full-width `！` and a pasted `/name` line too), fields separated by `|` (also `，` `｜` `／` where it makes sense). New commands must ship both. In `bot.py` add the name to `TEXT_COMMANDS` and handle it via the `text_command()` helper (returns `command, rest`). Add the command to `docs/指令.md`.
2. **Nothing from Discord is stored on the PC**: no messages, images or conversation logs. Memory is RAM only (last 30 messages per channel). Generated images go ComfyUI -> bot RAM -> Discord. Logs must never contain message content or search terms (keep `ddgs`, `ddgs.ddgs`, `primp`, `httpx` loggers silenced with `setLevel`).
3. **Add features only**: keep existing `.env` keys and connection settings working.
4. **.env**: never read or ask for keys/tokens. If a change needs `.env` edits, list the exact lines for Ep to add. No secrets in chat, code or commits.
5. **`UNFILTERED_NOTE` in core.py**: Ep alone edits and reviews it. Don't change, review or comment on it; only flag it if it causes a syntax/startup error.
6. Never produce or help relax sexual content involving minors or minor-looking characters (also applies to any reference images). No sexual or degrading pictures of real people.
7. Stickers stay off by default (`STICKERS=on` enables). Persona story easter eggs are PARKED; don't work on them.
8. Never skip or disable tests to get green. Never merge without Ep saying so.

## Working rules
- **After every change, give Ep (in Chinese) the commit id to expect, the PC commands to pull and restart, the commands to try and step-by-step checks with what a pass looks like; put the same checklist in `docs/目前进度.md`.**
- Split of work: Claude codes on a branch, runs unit tests and pushes the branch; Ep pulls it on the PC, tests in Discord (often from the phone) and merges to main from the terminal (`git checkout main`, `git pull`, `git merge <branch>`, `git push`).
- Before trusting a test result, compare Ep's `git log --oneline -1` with the expected commit id. Read `bot-error.log` before any restart (a restart overwrites it).
- Merge main into a branch before telling Ep it is ready.
- Don't subscribe to PR activity while waiting on Ep. Don't wake idle work. Big changes at medium effort, small at low effort.
- Check evidence before reporting the bot's status (online, stuck, running old code).
- **Trigger phrase**: when Ep says 「这个也是踩坑」, record that item (problem -> cause -> fix) in `docs/问题踩坑复利.md` and push.
- Announcement format and rules: see `docs/版本更新内容（已归档和刚完成）.md`.

## Key code map
- `bot.py` - Discord client (`ChatBot`): message routing, `TEXT_COMMANDS` + `text_command()`, slash commands, `/comment` (`COMMENT` regex runs before `text_command()`), persona (`switch_persona`, `persona_text`), polls, events (form, pickers, End buttons), `/status`, draw/refine/recall/edit and private `!dm…` (`private_picture`, `DM_COMMANDS`, `PRIVATE`), `main()` (logger setup).
- `core.py` - `Brain` (LM Studio calls, `image_part()` converts every picture to PNG), `ChannelMemory`, `SYSTEM_PROMPT`, `UNFILTERED_NOTE` (Ep only), personas, emoji/stickers, reply length, `shrink`, `load_config`.
- `imagegen.py` - `ImageMaker`: queue, styles, history/undo, refine edits, prompt rules (`SIZE_RULES`, `REFINER`, `EDITOR`, `SWAP_NOTE`), strengths, ControlNet, `fade_colors`, GPU swap, ComfyUI workflow/websocket, timings/estimate. `core` is imported as `brain_core` here (`core()` is a tag helper).
- `comfy_node.py` - ComfyUI node that reads a base64 image from the job (no disk writes).
- `search.py` - web/image search and backup engines. `monitor.py` - monitor window and `/status` stats.
- `tests/` - pytest suite with `mock_lmstudio.py`. `README.md` documents behaviour in detail; `.env.example` lists all keys.
