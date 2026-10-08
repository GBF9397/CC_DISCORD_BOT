# Project notes (for Claude sessions)

Any Claude session (new or old) reads this file first and can start work from it.

Last updated 2026-10-08

Keep this file current: update it in the same PR as any change to rules, setup or state.

## Owner and language
- Owner: Ep (GitHub GBF9397). Repo: GBF9397/CC_DISCORD_BOT.
- Talk to Ep in Chinese: short, conclusion first, one concise step list rather than long explanations.
- Work on branches and PRs. Ask Ep before pushing to main or merging.

## What the bot is
- Python Discord bot for fun. Chat answers come from a local model **gemma4-12b-bionic-v2** (has vision) in LM Studio (`http://localhost:1234/v1`, OpenAI-compatible, localhost only). Web search uses free DuckDuckGo (no API key) with backup engines.
- Image generation uses ComfyUI (portable) on the same PC; Gemma and ComfyUI take turns on the GPU (RTX 5060 Ti 16GB, 32GB RAM) via `lms unload` / `lms load`. Plan features within what this PC can run.
- Models: anime `animagine-xl-4.0`, realistic `Juggernaut-XL_v9_RunDiffusionPhoto_v2`.
- The bot only runs on Ep's PC. Cloud sessions cannot run it (no GPU, no LM Studio, DuckDuckGo blocked by the proxy): run the unit tests only (`pip install -r requirements-dev.txt`, then `python -m pytest`). Tests use a mock LM Studio server.

## Ep's PC setup (Windows)
- Bot folder: `C:\Bionic Workspace\001_Testing\CC_DISCORD_BOT` (git clone, `.venv` and `.env` next to `bot.py`).
- ComfyUI: `C:\Codex Workspace\003_CASUAL_CREATIVES\_LOCAL_AI_WORK\Software\ComfyUI_windows_portable` (start with `run_nvidia_gpu.bat`, or the bot starts it when `COMFYUI_DIR` is set). Restart ComfyUI once after `comfy_node.py` changes.
- LM Studio context length must be 16384+ (30-message memory); give the model max_tokens >= 512 (it reasons first) and read only `message.content`.
- Hidden start, from the bot folder:
  `Start-Process .venv\Scripts\python.exe bot.py -WindowStyle Hidden -RedirectStandardError bot-error.log`
- Stop: `Stop-Process -Name python` (also closes the monitor window). Run only one copy. `pythonw.exe` does NOT work.
- If venv activate is blocked: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`.
- `.env` is read only at startup; restart after edits. Auto-reply channels: see `BOT_CHANNEL_ID` in `.env` (numeric IDs, one line, comma-separated; a second line overrides the first). Other channels only answer commands and @mentions.
- Ep's update routine: `Stop-Process -Name python` -> `git fetch` -> `git checkout <branch>` (or main) -> `git pull` -> hidden start. Before saying something is "live", confirm it is on main and Ep restarted.

## Hard rules (never break)
1. **Every command works in two forms in ANY channel**: the `/` slash command and a one-line `!` text command (full-width `！` and a pasted `/name` line too), fields separated by `|` (also `，` `｜` `／` where it makes sense). New commands must ship both. In `bot.py` add the name to `TEXT_COMMANDS` and handle it via the `text_command()` helper (returns `command, rest`).
2. **Nothing from Discord is stored on the PC**: no messages, images or conversation logs. Memory is RAM only (last 30 messages per channel). Generated images go ComfyUI -> bot RAM -> Discord. Logs must never contain message content or search terms (keep `ddgs`, `ddgs.ddgs`, `primp`, `httpx` loggers silenced; `disabled=True` is not enough for child loggers, use `setLevel`).
3. **Add features only**: keep existing `.env` keys and connection settings working.
4. **.env**: never read or ask for keys/tokens. If a change needs `.env` edits, list the exact lines for Ep to add. No secrets in chat, code or commits.
5. **`UNFILTERED_NOTE` in core.py**: Ep alone edits and reviews it. Don't change, review or comment on it; only flag it if it causes a syntax/startup error.
6. Never produce or help relax sexual content involving minors or minor-looking characters (also applies to any reference images). No sexual or degrading pictures of real people.
7. Stickers stay off by default (`STICKERS=on` enables). Persona story easter eggs are PARKED; don't work on them.
8. Never skip or disable tests to get green. Never merge without Ep saying so.

## Announcements
Every member-facing release gets a short Chinese announcement as a raw markdown code block: heading `# 🤖 Gemma 4 机器人 v<version> 更新`, one `##` section per feature, point form, a short 示例 (Ep's tested example when available). Internal/infra changes get none. Small persona tweaks: don't go into detail, just say personas were improved and how to use `/persona character 角色 作品` (old personas must be re-created).

## Current state
- v1.4 is merged to main: PRs #12-#22, including #13 (perf monitor, `/status`), #15 (chat fixes, `/comment`, persona search improvements, backup search engines), #20 (`!` form for all commands), #21 (`!status`), #22 (creator-only 「结束」 end button on polls and events; event end-button fix done by a cloud session).
- PR #14 (image fixes: per-member `/refine` delta edits, `/recall`, `/edit`, branch `claude/image-gen-v1-4-417eyf`) is merged too, but Ep is still testing the image features. Fix anything he reports in a new branch/PR.
  Test plan: two people `!refine` at once; 5-6 rounds of `!refine`; `!recall` / `!recall 2` then refine; `!edit 头发改成红色` with an image; `!draw realistic 一个女生`.
- v1.4 announcement and full command list are drafted (kept on the project side, not in this repo).
- Next release after v1.4 is **v1.5**.

## Working rules (save usage)
- Threads/sessions don't subscribe to PR activity while waiting on Ep; subscribe only while actively driving an Ep-approved task.
- Big changes at medium effort, small changes at low effort.
- Don't wake idle work. Wait for Ep.
- Ep prefers short Chinese step lists.
- Check evidence before reporting the bot's status (online, stuck, running old code).

## Parked / future (only when Ep asks)
- Persona story easter eggs: parked.
- Stickers: off by default.
- Reference-image (IP-Adapter) folder: possible future PR. Local only, gitignored, path set in `.env`, nothing minor-looking (adult references kept separate and never minor-looking).
- More drawing styles via `.env` (`SD_STYLES=name:model, ...`): deferred.

## Key code map
- `bot.py` - Discord client (`ChatBot`): message routing, slash commands, `TEXT_COMMANDS` + `text_command()` for `!` forms, `/comment` webhook repost (`COMMENT`), polls (`poll_answers`, `EndPollButton`, `vote_summary`), events (`event_fields`, `event_start`, `clock_time`, `event_hours`, `EventForm`, `EventPicker`, `EndEventButton`), `/status`, draw/refine/recall/edit commands, `main()` (logger setup).
- `core.py` - `Brain` (LM Studio calls), `ChannelMemory` (30 messages in RAM), `SYSTEM_PROMPT`, `UNFILTERED_NOTE` (Ep only), `PERSONAS` / character persona prompts (`CHARACTER_STYLE`, `CHARACTER_PROMPT`, `LORE_*`), emoji/sticker handling (`extras_note`, `apply_extras`), random reply length (`REPLY_LENGTHS`, `too_long`, `shorten`, `REWRITE_NOTE`), `split_message`, `load_config`.
- `imagegen.py` - `ImageMaker`: queue (one per member), styles, per-member history/recall, refine delta edits (`_apply_edits`), GPU swap (`_lms`), ComfyUI workflow/websocket, ComfyUI auto-start and node install, ETA, `shrink` for uploads.
- `comfy_node.py` - ComfyUI custom node that reads a base64 image from the job (no disk writes); copied into `ComfyUI/custom_nodes` at startup.
- `search.py` - `web_search`, `image_search`, `needs_search`, `is_fact_question`, backup engines.
- `monitor.py` - always-on-top monitor window and stats used by `/status` (`MONITOR_WINDOW=off` disables).
- `tests/` - pytest suite with `mock_lmstudio.py`; `README.md` documents member-facing behavior; `.env.example` lists all keys.

## Open task: v1.4 release announcement (handed over 2026-10-08)
- Owner of this task is now whichever session Ep is working with. The old project coordinator has retired.
- Goal: give Ep, in chat, (a) the full v1.4 member announcement and (b) the full latest command list, each as one raw markdown code block, in Chinese.
- Draft to start from: `docs/v1.4_release_draft.md` (written before the latest image/red-hair fixes and `!dm`; verify every command against the current code and README before finalizing).
- Format: heading `# 🤖 Gemma 4 机器人 v1.4 更新`, one `##` section per feature, short point form, a `示例` per section. Use only examples Ep actually tested (so far: /poll 谁最帅 and !event 电影夜); for others write 用法 lines and ask Ep for test results to turn into examples. No tech jargon. Persona changes: one short section only (人设优化了, `/persona character 角色 作品`, old personas must be re-created).
- Must cover: every command now has a one-line `!` / `！` form usable in any channel (fields separated by `|`); polls (`members` option @s people, ends when all have voted; default 1 day, 7 days with @members; creator-only 「结束」 button that reports current votes); events (`!event 名称 | 日期 | 时间 | 地点 [| 小时 | 说明]`, just `!event` opens a form, creator-only end button); `/status` (private live bars) and `!status` (public); `/comment` / `!comment` (reposts your words under your own name/avatar); chat fixes (no more cut-off replies, `:emoji` works); image `/refine`, `/recall`, `/edit` (and anything newer that is merged by then, e.g. estimated time, recolor fix). Include only features that are on main when you write it.
- Command list: every command with its `/` form, `!` form and a one-line example.
- Internal/infra changes (perf monitor window, logging, AGENTS.md) get no announcement.
- PR #14 is merged; its follow-up work is on branch `claude/task-nb5ft9` (owned by Ep's current session).
