# Discord bot on the local Gemma 4 Bionic model

Answers in Discord using `gemma4-12b-bionic-v2` served by LM Studio. The model gets no tools; for current questions the bot searches the web (free DuckDuckGo, no API key) and passes the results to the model.

## How members use it
- `/ask question:<text>` - any member can ask (slash command).
- `@Bot <text>` - mention the bot in any channel.
- Any message in the channels listed in `BOT_CHANNEL_ID` (comma-separated).
- Attach images (PNG, JPEG, WebP) to a message the bot answers and it looks at them. Images are only held in RAM for that one request, never saved.
- `/search question:<text>` or `!search <text>` - look it up on the web first, then answer.
- `/reset` or `!reset` - forget this channel's conversation.
- The bot uses the server's own custom emoji (animated ones too) in its replies now and then, and once in a while sends one of the server's stickers. On startup it shows each emoji and sticker picture to Gemma once to learn what it means (kept in RAM only), and it remembers the last 2 short messages where members used each one (RAM only, gone on restart), so it can pick one that fits the mood.
- `/persona` - change the bot's personality in this channel: pick a `preset` (buddy, tsundere, wuxia, pirate, roast) or write your own with `custom`, or play a known character with `character` (e.g. `Ganyu Genshin Impact`): the bot searches the web for them and turns what it finds into a personality. With no options it shows the current one. Switching clears the channel's memory. Personalities live in RAM, so a restart goes back to buddy.

Questions that sound time-sensitive (today, latest, news, price, weather, score, a year like 2026, 今天, 最新, 新闻, 价格, 天气 ...) are searched automatically. Search results are used for that one answer only and are never saved.

The bot remembers the last 30 messages per channel (in RAM, trimmed to ~9000 tokens; set LM Studio's context length to 16384 or more), answers one request at a time in order, shows "typing...", splits replies over 2000 characters, and says so politely if LM Studio is offline.

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
