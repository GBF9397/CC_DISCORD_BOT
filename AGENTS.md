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
- v1.4 is merged to main: PRs #12-#22, including #13 (perf monitor, `/status`), #15 (chat fixes, `/comment`, persona search improvements, backup search engines), #20 (`!` form for all commands), #21 (`!status`), #22 (creator-only 「结束」 end button on polls and events), the event form with date/time menus, and PR #14 (per-member `/refine`, `/recall`, `/edit`). Main also has (merged at `3c9c254`) the two-picture character swap for `/edit`, the wait-time estimate, Pillow shrinking of uploads and the 5-minute Gemma countdown.
- **In flight: branch `claude/task-nb5ft9`** (image follow-ups after #14, not on main yet). A new session starts there: `git checkout claude/task-nb5ft9`, `pip install -r requirements.txt -r requirements-dev.txt`, `python -m pytest`. It adds, on top of main:
  - refine/edit redraw sizes: any change the member names redraws over half (small 0.55); hair colour counts as big; `/edit` takes its size from Gemma instead of a fixed 0.6;
  - a change against a named character's usual look (e.g. Ganyu with red hair) drops the name/series tags and describes the looks, since the drawing model always draws a named character their usual way;
  - refine keeps every untouched tag's exact text and weight across rounds (only this round's additions get 1.3);
  - recolor: when a big area gets a new colour, the source picture is faded (35% colour left, outlines darkened) and redrawn at most 60%;
  - logs `Edit: SIZE x, redrawing N%`, `Refine: ...`, `Recolor: ...` (no message content).
- **Testing on Ep's PC (2026-10-08 evening):** two-picture swap, countdown, estimate and big uploads work. Red hair on official Ganyu art: at 75% + recolor the hair turned red but horns became cat ears and the pose moved, so recolor was capped at 60% (`22efe04`); Ep is re-testing that. Still to test: 5-6 rounds of `!refine` keeping earlier changes; `!recall` / `!recall 2` then refine; **two people `!refine` at once** (Ep, at home); `!draw realistic 一个女生`. When all pass, Ep merges `claude/task-nb5ft9` into main from the terminal. Update this file's state and README in that same branch before the merge.
- **Private drawing commands (built on this branch, not yet tested on the PC):** `!dmdraw`/`/dmdraw`, `!dmedit`/`/dmedit` (incl. second character picture), `!dmrefine`/`/dmrefine`, `!dmrecall`/`/dmrecall`. The bot first DMs the member "收到，画好会发到这里"; if the DM fails it says so in the channel and draws nothing. `!dm…` messages are deleted (Manage Messages, as `!comment`); `/dm…` replies are ephemeral. The picture goes only by DM; the channel gets an unnamed "画画中（私人请求）" notice with the countdown/progress (Ep wants it so members know why the bot is quiet). Private pictures live under history key `PRIVATE` (0), one per member across channels, so the public `!recall` never shows them; they keep the asking channel's drawing style (`style_channel`). Code: `bot.private_picture()` for `!dm…`, `draw_command(..., private=True)` for `/dm…`, `queue_picture(private=True)`.
- Red hair on official Ganyu art failed both ways with plain img2img (75%: red but horns became cat ears; 60%: white hair with red streaks, another person). **ControlNet added 10-08 (Ep agreed):** optional `.env` key `SD_CONTROLNET` = a canny SDXL ControlNet file in `ComfyUI/models/controlnet` (suggested: xinsir/controlnet-canny-sdxl-1.0, ~2.5 GB, renamed `controlnet-canny-sdxl.safetensors`). For a RECOLOR job the workflow adds `ControlNetLoader` (20) -> `Canny` on the upload (21, built into ComfyUI) -> `ControlNetApplyAdvanced` (22, weight `CONTROLNET_WEIGHT` 0.8 until `CONTROLNET_UNTIL` 80% of the steps) and redraws `RECOLOR_CONTROLNET_STRENGTH` 0.9. `_has_controlnet()` checks ComfyUI's `/object_info/ControlNetLoader`; if the file isn't listed, recolors fall back to the 60% fade path and log a warning. **Passed on the PC 10-08** (Ep: acceptable).
- 10-08 fixes after Ep's `!dm` test: `!recall`/`!dmrecall` with no number go back one picture and post only that one (short `RECALLED`/`AT_OLDEST` texts, `RECALL_LIST` removed); a `!dm…` request the bot can't delete (no Manage Messages in that channel) gets a `CANT_HIDE` DM; two-picture swaps keep picture 2's name tags and are never faded (`SWAP_NOTE`; the name-dropping and RECOLOR rules had turned Ganyu into a generic teal-haired girl).
- **Ep decided (10-08): everything built today is v1.4**, including this branch (image fixes, `!dm*`, ControlNet recolor). Write one v1.4 announcement after this branch is merged. The next release after that is v1.5.

## Working rules (save usage)
- **After every change, give Ep (in Chinese) the commit id to expect, the PC commands to pull and restart, the command list to try and step-by-step checks with what a pass looks like; and keep the same checklist in "Test checklist" below so other sessions know it.**
- Threads/sessions don't subscribe to PR activity while waiting on Ep; subscribe only while actively driving an Ep-approved task.
- Big changes at medium effort, small changes at low effort.
- Don't wake idle work. Wait for Ep.
- Ep prefers short Chinese step lists.
- Check evidence before reporting the bot's status (online, stuck, running old code).
- Split of work: Claude codes on a branch, runs unit tests and pushes the branch; Ep pulls it on the PC, tests in Discord (often from the phone) and merges to main from the terminal (`git checkout main`, `git merge <branch>`, `git push`).
- Before trusting a test result, have Ep run `git log --oneline -1` and compare the commit id: once Ep tested on `main` for half an hour while the fixes were on the branch.
- `Start-Process ... -RedirectStandardError bot-error.log` overwrites the log: read it before restarting. Read with `Select-String -Path bot-error.log -Pattern "Edit:|Refine:|Recolor" | Select-Object -Last 5`.
- Branches drift behind main fast (other sessions push too); merge main in before telling Ep a branch is ready.
- Discord limits: select menus hold 25 options; answer a button click within 3 s (defer first); a typed message can't open a form (needs a button); a public @mention is always visible; ephemeral messages exist only for slash commands.
- `Get-Process python` shows **4 processes for one running bot**: the bot and the monitor window (`monitor.py` runs as its own process), each as a small venv launcher plus the real python. ComfyUI adds more once it starts. Two bots would show 8.
- LM Studio can't read WebP (it answers 400 "'url' field must be a base64 encoded image"). Build every image part with `core.image_part()`, which converts to PNG; never put raw upload bytes in a data URL.
- In `imagegen.py` the module `core` is imported as `brain_core`, because `imagegen.core()` is a tag helper.
- `!comment` is matched by the `COMMENT` regex before `text_command()` (which can't handle a leading @mention or a newline); keep both cases working if you touch it.

## Parked / future (only when Ep asks)
- Persona story easter eggs: parked.
- Stickers: off by default.
- Reference-image (IP-Adapter) folder: possible future PR. Local only, gitignored, path set in `.env`, nothing minor-looking (adult references kept separate and never minor-looking).
- More drawing styles via `.env` (`SD_STYLES=name:model, ...`): deferred.

## Test checklist (branch `claude/task-nb5ft9`; update it with every change)
Pull and restart on the PC, then check the commit id:
```
Stop-Process -Name python
git fetch
git checkout claude/task-nb5ft9
git pull
Start-Process .venv\Scripts\python.exe bot.py -WindowStyle Hidden -RedirectStandardError bot-error.log
git log --oneline -1
```
After a test, before any restart: `Select-String -Path bot-error.log -Pattern "Edit:|Refine:|Recolor|SD_CONTROLNET" | Select-Object -Last 5`

| # | Try | Pass when | Status |
|---|---|---|---|
| 1 | `/edit 把头发改成红色` with official Ganyu art | hair red, horns kept, pose close; log `Edit: SIZE big` + `Recolor: ... redrawing 60%` | FAILED 10-08 without ControlNet (60%: white hair with red streaks; 75%: red hair but cat ears); solved by #14 |
| 2 | `!refine` 5-6 rounds (red hair, then 戴上帽子, 背景改成晚上, ...) | each round changes only what was asked; earlier changes stay | not tested |
| 3 | after 2+ refines: `!recall`, then `!recall 1`, then `!refine 改成蓝色衣服` | `!recall` shows only the previous picture ("回到第 N 张（共 M 张）"); `!recall 1` the first; the refine builds on it; at the first picture `!recall` says 已经是最早的一张 | changed 10-08 (`!recall` = one step back), not tested |
| 4 | two members `!refine` at the same time | each gets their own picture changed | not tested (Ep, at home) |
| 5 | `!draw realistic 一个女生` | realistic photo style | passed 10-08 (natural photo, ~1 min) |
| 6 | `!dmdraw 一只猫` | your message disappears; channel shows only "画画中（私人请求）" + countdown/progress, no name; DM "收到…" then the picture by DM | passed 10-08 once Ep gave the bot Manage Messages (before that the message stayed; now the bot DMs CANT_HIDE in that case) |
| 7 | `/dmdraw request:一只猫` | only you see "开始画了，画好私信给你"; rest as #6 | passed 10-08 |
| 8 | `!dmedit 第2张的角色穿第1张的衣服` with two pictures | message deleted; result by DM, looks like picture 2's character | 10-08: DM ok but drifted to a generic teal-haired girl (name dropped + faded); fixed: swaps keep the name and are never faded |
| 9 | `!dmrefine 戴上帽子`, `!dmrecall`, `!dmrecall 1` | refines your last private picture; `!dmrecall` sends only the previous one by DM; public `!recall` doesn't show private pictures | refine/DM passed 10-08; `!dmrecall` now one step back |
| 10 | turn off DMs from server members, then `!dmdraw 一只猫` | channel reply "我私信不了你…", message stays, nothing drawn | passed 10-08 after turning DMs off in both servers Ep shares with the bot (with one still on, the DM still arrives; that's Discord, not a bug) |
| 13 | grant the bot **Manage Messages** in #gemma-4-img-sfw, then `!dmdraw 一只猫` | your message is deleted | passed 10-08 (Ep granted it) |
| 14 | ControlNet installed + `SD_CONTROLNET=controlnet-canny-sdxl.safetensors` in `.env`, then `/edit 把头发改成红色` with official Ganyu art | red hair; same pose, horns and face; log `Recolor: following the outlines with ControlNet, redrawing 90%` | passed 10-08 21:05: fully red hair, horns, pose and framing kept; eyes turned orange-red, a bit more gold on the outfit. Ep: 能接受，不错 |
| 15 | after a draw, refine again right away (ControlNet red-hair picture -> `/refine ...` twice) | each refine draws; if one says "brain (LM Studio) is offline", read `Select-String -Path bot-error.log -Pattern "lms|reload|LM Studio|ERROR|WARNING" \| Select-Object -Last 10` before restarting | 10-08 21:18 a second refine got "offline" ~1 min after starting (not the 5-min timeout); cause unknown, `core.image_prompt` now logs unreachable / refused (status) / empty reply. Same evening the log showed `LM Studio refused a chat request: 400 'url' field must be a base64 encoded image` (LM Studio can't read WebP): now every picture Gemma sees goes through `core.image_part()` -> `shrink()` (PNG, max 1024 px), for chat, emoji/sticker descriptions and picture prompts |
| 11 | second picture after a restart | queue notice shows `⏱️ 预计 X 分 Y 秒` | passed 10-08 |
| 12 | two-picture swap `!edit 第2张的角色穿第1张的衣服` | outfit/pose from 1, looks from 2 | passed 10-08 |

## How picture edits work (imagegen.py)
- Per picture: Gemma writes the prompt (sees uploaded/old pictures, shrunk to 1024 px) -> `lms unload` -> ComfyUI draws (img2img from a source picture when editing or refining) -> `lms load`. Pictures, prompts and timings stay in RAM.
- `REFINE_STRENGTH` = share of the source redrawn: small 0.55, medium 0.6, big 0.75, new 1.0. `SIZE_RULES` (shared by `/refine` and `/edit`): small = expression, lighting, colour of a small thing; medium = hairstyle, eye colour, clothes, background; big = hair colour, pose, framing, adding/removing someone; against a named character's usual look -> drop name/series tags, at least big; a big area getting a new colour -> `RECOLOR`.
- `/refine`: Gemma replies `ADD/REMOVE/AVOID/SIZE`; untouched tags keep text and weight, ADD tags get `(tag:1.3)`, removed ones go to the negative prompt (max 12).
- `/edit` one picture: size from Gemma (`EDIT_STRENGTH` 0.6 if none). Two pictures: picture 2's character into picture 1 (`SWAP_NOTE`; only Gemma sees picture 2), `SWAP_STRENGTH` 0.75.
- Recolor: `fade_colors()` keeps `KEEP_COLOR` 0.35 of the colour and darkens outlines so hair and a same-brightness background stay apart; capped at `RECOLOR_STRENGTH` 0.6 (swaps keep 0.75). Stored pictures are never faded.
- Gemma's prompt: one try of `core.IMAGE_PROMPT_TIMEOUT` 300 s, no retries (`TooSlow` -> "took too long"), countdown in the notice every 15 s. Estimate: `ImageMaker.timings` (last 5 per step) -> `⏱️ 预计 X 分 Y 秒` in the queue notice.

## History (everything built so far)
- **10-05 v1.0 start**: chat bot on local Gemma via LM Studio; `/ask`, @mention and auto-reply in `BOT_CHANNEL_ID` channels (several allowed); startup/channel logging without message content.
- **10-06** (#1-#11):
  - Reads attached images (RAM only). Web search (DuckDuckGo) for time-sensitive questions, `/search` `!search`; the model is told it has live results.
  - `/persona` presets (buddy, tsundere, wuxia, pirate, roast), custom personas, and `character` personas built from a web lookup.
  - Server custom emoji and stickers in replies; Gemma learns each emoji/sticker's meaning from its picture and from members' recent uses (RAM); `emoji_meanings.txt` for hand-written meanings; stickers off by default (`STICKERS=on`).
  - Memory raised from 10 to 30 messages per channel. Optional `UNFILTERED_MODE` (Ep owns `UNFILTERED_NOTE`).
  - Drawing with ComfyUI taking turns with Gemma on the GPU: `/draw` `!draw`, `/refine` `!refine`, anime/realistic styles per channel (`/drawstyle`, `!style`), pictures over ComfyUI's websocket (never on disk), ComfyUI auto-start, one queued picture per member, style fixed when queued, progress in percent, member pinged when done; named subjects looked up on the web (text + matching pictures) before writing the prompt; new characters described instead of named.
  - Random reply length cap (10/30/50/100) except for search answers; emoji written without colons or with full-width colons fixed.
- **10-07** (#12-#15):
  - `/poll` and `/event` with Discord's native polls and scheduled events. PC monitor window and `/status` (GPU, VRAM, CPU, RAM bars).
  - Image v1.4: per-member refine history, `/recall`, `/edit` with an uploaded picture, prompts that follow the asked change, colour codes as names, removed things in the negative prompt, Juggernaut sampler for realistic.
  - Chat fixes: length cap sent with the newest message, over-long replies rewritten within the cap, `:name` emoji, `/comment` (members talking among themselves; reposted under their name via webhook, no reply, not remembered); character personas look up facts, teammates and relationships, use official Chinese names, react to what a picture shows, explain techniques and respect worthy rivals as in canon; backup search engines (Bing, Brave, Google, Yahoo, Wikipedia) with Chinese results; search logs silenced.
- **10-08** (#16-#22 and after):
  - Polls: answers split on `| , / 、` and full-width marks; `members` named voters end the poll when all voted; default 1 day, 7 days with members.
  - Events: 12-hour and Chinese times, one-line `!event 名称 | 日期 | 时间 | 地点 [| 小时 | 说明]` in any order, deferred creation with Discord's error shown, bare `!event`/`/event` opens a form with date (25 days), hour, minute and length menus.
  - Every command works in any channel as `/`, `!`, full-width `！` or a pasted `/name` line; `!ask`, `!poll`, `!comment`, `!status` added.
  - Creator-only End buttons on polls (posts current counts) and events (answers the click first; a second click on a finished event just drops the button).
  - `/status` refreshes every ~3 s for a minute; monitor shows coloured bars.
  - Images: bare `!edit` explains itself; two-picture character swap; wait-time estimate; uploads shrunk to 1024 px with Pillow (big uploads used to time out after 3x2 min retries); Gemma gets one 5-minute try with a countdown. On `claude/task-nb5ft9`: redraw sizes, name dropping, tag weights kept, recolor (see Current state).
  - `AGENTS.md` and `CLAUDE.md` added for Claude sessions.

## Key code map
- `bot.py` - Discord client (`ChatBot`): message routing, private `!dm…`/`/dm…` pictures (`private_picture`, `DM_COMMANDS`, `PRIVATE`), slash commands, `TEXT_COMMANDS` + `text_command()` for `!` forms, `/comment` webhook repost (`COMMENT`), polls (`poll_answers`, `EndPollButton`, `vote_summary`), events (`event_fields`, `event_start`, `clock_time`, `event_hours`, `EventForm`, `EventPicker`, `EndEventButton`), `/status`, draw/refine/recall/edit commands, `main()` (logger setup).
- `core.py` - `Brain` (LM Studio calls), `ChannelMemory` (30 messages in RAM), `SYSTEM_PROMPT`, `UNFILTERED_NOTE` (Ep only), `PERSONAS` / character persona prompts (`CHARACTER_STYLE`, `CHARACTER_PROMPT`, `LORE_*`), emoji/sticker handling (`extras_note`, `apply_extras`), random reply length (`REPLY_LENGTHS`, `too_long`, `shorten`, `REWRITE_NOTE`), `split_message`, `load_config`.
- `imagegen.py` - `ImageMaker`: ControlNet recolors (`controlnet`, `_has_controlnet`, nodes 20-22 in `_workflow`), queue (one per member), styles, per-member history/recall, refine delta edits (`_apply_edits`), prompt rules (`SIZE_RULES`, `REFINER`, `EDITOR`, `SWAP_NOTE`), `REFINE_STRENGTH`/`SWAP_STRENGTH`/`RECOLOR_STRENGTH`, `fade_colors` and `shrink` (Pillow), Gemma prompt with countdown (`_ask`), GPU swap (`_lms`), ComfyUI workflow/websocket, ComfyUI auto-start and node install, `timings`/`estimate`.
- `comfy_node.py` - ComfyUI custom node that reads a base64 image from the job (no disk writes); copied into `ComfyUI/custom_nodes` at startup.
- `search.py` - `web_search`, `image_search`, `needs_search`, `is_fact_question`, backup engines.
- `monitor.py` - always-on-top monitor window and stats used by `/status` (`MONITOR_WINDOW=off` disables).
- `tests/` - pytest suite with `mock_lmstudio.py`; `README.md` documents member-facing behavior; `.env.example` lists all keys.

## Sessions and handoffs (log every takeover here)
Each session that takes over, hands off or retires adds a dated line, newest last, so the next session knows who owns what.
- 2026-10-08: the original local project coordinator retired (context too long). Its last push: the v1.4 announcement task below and `docs/v1.4_release_draft.md`.
- 2026-10-08: the cloud session `session_01TuHhG4sALA7AwNnx2tNU4G` is Ep's main session. It owns branch `claude/task-nb5ft9` (image follow-ups, `!dm*`) and the v1.4 announcement task. New sessions that hand work to it: add a line here with what you hand over and where it lives (branch, file, PR).

## Open task: v1.4 release announcement (handed over 2026-10-08)
- Owner of this task is now whichever session Ep is working with. The old project coordinator has retired.
- Goal: give Ep, in chat, (a) the full v1.4 member announcement and (b) the full latest command list, each as one raw markdown code block, in Chinese.
- Draft to start from: `docs/v1.4_release_draft.md` (written before the latest image/red-hair fixes and `!dm`; verify every command against the current code and README before finalizing).
- Format: heading `# 🤖 Gemma 4 机器人 v1.4 更新`, one `##` section per feature, short point form, a `示例` per section. Use only examples Ep actually tested (so far: /poll 谁最帅 and !event 电影夜); for others write 用法 lines and ask Ep for test results to turn into examples. No tech jargon. Persona changes: one short section only (人设优化了, `/persona character 角色 作品`, old personas must be re-created).
- Also covers (Ep, 10-08): everything on `claude/task-nb5ft9` once merged: `!dm…`/`/dm…` private drawing, two-picture character swap, wait-time estimate and countdown, `!recall` stepping back one picture, better colour changes.
- Must cover: every command now has a one-line `!` / `！` form usable in any channel (fields separated by `|`); polls (`members` option @s people, ends when all have voted; default 1 day, 7 days with @members; creator-only 「结束」 button that reports current votes); events (`!event 名称 | 日期 | 时间 | 地点 [| 小时 | 说明]`, just `!event` opens a form, creator-only end button); `/status` (private live bars) and `!status` (public); `/comment` / `!comment` (reposts your words under your own name/avatar); chat fixes (no more cut-off replies, `:emoji` works); image `/refine`, `/recall`, `/edit` (and anything newer that is merged by then, e.g. estimated time, recolor fix). Include only features that are on main when you write it.
- Command list: every command with its `/` form, `!` form and a one-line example.
- Internal/infra changes (perf monitor window, logging, AGENTS.md) get no announcement.
- Leave admin/setup details out of announcements (Discord permissions such as Manage Messages, DM privacy switches, `.env`): Ep is the only admin and handles them. For `!dm…` just say the picture comes by DM and the request is hidden.
- PR #14 is merged; its follow-up work is on branch `claude/task-nb5ft9` (owned by Ep's current session).
