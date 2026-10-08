# Discord bot on the local Gemma 4 Bionic model

Answers in Discord using `gemma4-12b-bionic-v2` served by LM Studio. The model gets no tools; for current questions the bot searches the web (free DuckDuckGo, no API key) and passes the results to the model.

## How members use it
- `/ask question:<text>` - any member can ask (slash command).
- `@Bot <text>` - mention the bot in any channel.
- Any message in the channels listed in `BOT_CHANNEL_ID` (comma-separated).
- Attach images (PNG, JPEG, WebP) to a message the bot answers and it looks at them. Images are only held in RAM for that one request, never saved.
- `/search question:<text>` or `!search <text>` - look it up on the web first, then answer.
- `/draw request:<text>` or `!draw <text>` - draw a picture, when `IMAGE_GEN=on` (see below).
- `/refine changes:<text>` or `!refine <text>` - change your last picture in this channel (or the one `/recall` picked).
- `/recall [number]` or `!recall [number]` - list your last 5 pictures here, or go back to one so `/refine` builds on it.
- `/edit image:<upload> changes:<text>` or `!edit <text>` with a picture attached - redraw an uploaded picture with the changes.
- `/drawstyle style:<anime|realistic>` or `!style <anime|realistic>` - switch the drawing model for this channel.
- `/reset` or `!reset` - forget this channel's conversation.
- The bot uses the server's own custom emoji (animated ones too) in its replies now and then, and, if `STICKERS=on` is in `.env` (off by default), once in a while sends one of the server's stickers. On startup it shows each emoji (and sticker, when on) picture to Gemma once to learn what it means (kept in RAM only), and it remembers the last 2 short messages where members used each one (RAM only, gone on restart), so it can pick one that fits the mood.
- `/persona` - change the bot's personality in this channel: pick a `preset` (buddy, tsundere, wuxia, pirate, roast) or write your own with `custom`, or play a known character with `character` (e.g. `Ganyu Genshin Impact`): the bot searches the web for them and turns what it finds into a personality. With no options it shows the current one. Switching clears the channel's memory. Personalities live in RAM, so a restart goes back to buddy.

Questions that sound time-sensitive (today, latest, news, price, weather, score, a year like 2026, 今天, 最新, 新闻, 价格, 天气 ...) are searched automatically. Search results are used for that one answer only and are never saved.

The bot remembers the last 30 messages per channel (in RAM, trimmed to ~9000 tokens; set LM Studio's context length to 16384 or more), answers one request at a time in order, shows "typing...", splits replies over 2000 characters, and says so politely if LM Studio is offline.

## Drawing pictures (optional)
Set `IMAGE_GEN=on` in `.env` and restart, and members can use `/draw request:<text>` or `!draw <text>`. The graphics card takes turns: Gemma turns the request into a Stable Diffusion prompt, then is unloaded from LM Studio (`lms unload`); ComfyUI draws the picture, then frees its model; Gemma is loaded back (`lms load` with `LMSTUDIO_CONTEXT`) and the bot posts the picture. Picture requests queue up, one per member: a member can ask again only after their picture is posted. A request made while the bot is busy gets a "Queued, N ahead of you" notice. The bot draws every queued picture before it loads Gemma back (Gemma only comes back briefly to write prompts for requests that arrived meanwhile), and until the queue is empty it ignores everything except picture requests. If anything fails, Gemma is still reloaded and the bot says what went wrong. While ComfyUI draws, the bot edits its "Drawing..." notice to show the progress (a bar and a percentage, every 10%).

ComfyUI has no internet, so when a request names a specific character, person, place, product or artwork, Gemma first looks it up on the web, even when it thinks it knows the name, since it often guesses wrong: a text search plus up to five pictures (the same free DuckDuckGo search as `/search`, safe search on, once per picture). Gemma reads the text, keeps only the pictures that match it (some may show other characters from the same series), and writes the prompt from what they show. The anime model only knows characters from before 2025, so for newer ones the prompt leaves out the name and series and describes the looks alone; otherwise the model draws another character it knows from that series. Results and pictures stay in RAM for that one prompt and are never saved.

`/refine` (or `!refine`) changes the member's own last picture in that channel, so two members refining at once never build on each other's pictures. Members only write the change ("natural skin"), never the whole description again: Gemma looks at the picture and its tags and replies with just the edits (`ADD: medium hair REMOVE: short hair, bob cut AVOID: SIZE: medium`), and the bot applies them, so every tag nobody mentioned stays exactly as it was, round after round, and the prompt doesn't grow. The added tags get extra weight for that round; removed and avoided tags go into the negative prompt and stay there (the last 12) so they can't creep back. Gemma uses the drawing model's own words (hair: very short, short, medium = shoulders, long, very long = waist; "a bit longer" moves one step; color codes become color names). The new picture is redrawn from the last one, only as much as the change needs (SIZE small/medium/big = 45/60/75%), so the parts that were fine stay fine; SIZE new draws again from scratch with the same seed. If ComfyUI doesn't have the bot's node yet, a refine draws again with the same seed. Each member's last 5 pictures per channel are kept in RAM: `/recall` (or `!recall`) shows them, and `/recall number:2` goes back to picture 2 so the next `/refine` branches from it; the newer pictures stay. A restart forgets them all.

`/edit` (or `!edit <changes>` with a picture attached) redraws an uploaded picture: Gemma looks at it and writes the prompt with the change, and ComfyUI redraws it partly (`EDIT_STRENGTH` 0.6 in `imagegen.py`), so the shape stays and the asked-for details change. ComfyUI's own image loader needs the picture saved in its input folder, so the bot instead sends it inside the job to a small node, `comfy_node.py`, which the bot copies into `ComfyUI/custom_nodes` at startup when `COMFYUI_DIR` is set (otherwise copy it there by hand). ComfyUI reads new nodes only when it starts, so restart it once after updating.

Two styles: `SD_CHECKPOINT` is `anime` (the default) and `SD_CHECKPOINT_REALISTIC`, if set, adds `realistic`. Members switch a channel with `/drawstyle` or `!style realistic`; the choice is kept in RAM and a restart goes back to anime. A single request can also start with the style, e.g. `!draw realistic a sports car`. Each queued picture keeps the style it was asked with, and pictures are drawn first come, first served. `/refine` uses the model the picture was drawn with, unless the change starts with a style name or the channel's style was switched since. Gemma is told which model will draw: for `realistic` it writes a photo description without anime tags (a character from an anime or game becomes a real person in cosplay), and anime, illustration and cel shading go into the negative prompt. Realistic pictures use DPM++ 2M Karras, 30 steps, CFG 4.5, the settings Juggernaut XL is made for.

Setup: install ComfyUI and keep it running (it uses almost no graphics memory while idle), or put its `ComfyUI_windows_portable` folder in `COMFYUI_DIR` and the bot starts it in the background, with no window, the first time someone asks for a picture (it then stays running); put one model in its `models/checkpoints` folder, and put that file name in `SD_CHECKPOINT`. Use `IMAGE_SIZE=1024` for SDXL models, `512` for SD 1.5. The `lms` command must work in the bot's terminal. Pictures are never written to disk: ComfyUI sends each one straight to the bot over its websocket (the `SaveImageWebsocket` node, which ships with ComfyUI in `custom_nodes/websocket_image_save.py`), and the bot keeps it in RAM only until it is posted. Hard limits: no sexual pictures involving anyone who is or looks under 18, and no sexual or degrading pictures of real people.

## Unfiltered mode (optional)
Set `UNFILTERED_MODE=on` in `.env` and restart to loosen the bot's style: swearing, crude and dark humor, harsher roasts, mature topics, blunt opinions, and far fewer refusals or safety disclaimers. It is off by default. Hard limits stay: no sexual content involving minors, no real-world instructions for weapons or serious harm, no doxxing or harassing real people. Gemma has its own built-in caution, so a prompt can only loosen it so far; to go further, load a less filtered model in LM Studio and put its id in `LMSTUDIO_MODEL` (no code change). Keep anything sexual to Discord age-restricted channels.

## Emoji meanings by hand (optional)
If the bot misreads an emoji or sticker, create `emoji_meanings.txt` next to `bot.py` (it is gitignored) with one line per emoji, using its Discord name:
```
catstare: speechless at nonsense
awkward_girl: awkward
middlefinger: rude, playful f-you
```
Lines win over the bot's own guess and take effect on the next reply, no restart needed.

## Setup (Windows)
1. LM Studio: load `gemma4-12b-bionic-v2`, then `lms server start` (serves `http://localhost:1234/v1`).
2. Discord Developer Portal: New Application > Bot > turn on **Message Content Intent** > Reset Token and copy it.
3. OAuth2 > URL Generator: scopes `bot` and `applications.commands`; permissions Send Messages, Read Message History. Open the URL to invite the bot.
4. In this folder:
   ```
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   copy .env.example .env
   ```
   Paste the token into `.env` (never into a chat, never commit it; `.env` is gitignored).
5. `python bot.py`

Slash commands can take a few minutes to appear the first time.

## Tests
`pip install -r requirements-dev.txt` then `pytest`. The tests run the bot against a mock LM Studio server, so neither Discord nor LM Studio is needed.
