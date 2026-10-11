# PokéBot SG 🔔

A Telegram bot that **@tags you and your friends** for Pokémon 30th-anniversary drops on Lazada Singapore — **only from the official Pokémon Center Singapore store** ([Pokémon Store Online Singapore](https://www.lazada.sg/shop/pokemon-store-online-singapore/)).

It does two things:

1. **Timed reminders** — pings your group before each drop (e.g. 8 PM the night before, 10 min before, and "HAPPENING NOW"), with a button that opens the official store.
2. **Official store watcher** — checks the store every ~10 minutes and tags you when a **new 30th-anniversary item is listed** or something **comes back in stock**. Listings from any other seller are ignored.

### Pre-loaded reminders (edit in `config.yaml`)

| When (SGT) | What | Pings |
|---|---|---|
| Fri 30 Oct | 30th Celebration — Espeon ex & Umbreon ex Battle Decks* | 8 PM night before · midnight · 9 AM |
| Fri 6 Nov | 30th Celebration final wave — Ultra Premium Collection: Day & Night, Ditto Premium, Mew & Mewtwo Figure* | 8 PM night before · midnight · 9 AM |
| Wed 11 Nov, 12:00 AM | Lazada 11.11 midnight start | 8 PM · 11 PM · 11:50 PM · midnight |
| Wed 11 Nov, 12:00 PM | 11.11 noon round | 11:30 AM · noon |
| Wed 11 Nov, 8:00 PM | 11.11 evening flash rounds | 7:30 PM · 8 PM |

\* International release dates. The official store hasn't confirmed these as Lazada drops — the watcher is what confirms a real listing. When the store announces an exact drop (e.g. on Instagram @pokemonofficial.sg), add it as an event.

---

## Setup (about 10 minutes)

**1. Create the bot.** In Telegram, open **@BotFather** → send `/newbot` → pick a name → copy the **token** it gives you.

**2. Add it to your group.** Group → Add members → search your bot's username. (Only if you turn on `pin_live`: make it an admin with "Pin messages".)

**3. Install.** You need Python 3.10 or newer. In this folder run:

```
pip install -r requirements.txt
```

**4. Add your token.** Copy `.env.example` to a new file called `.env` and paste the token after `TELEGRAM_BOT_TOKEN=`.

**5. Get your group's chat ID.** Start the bot:

```
python pokebot.py run
```

then in your group send `/id@YourBotName`. It replies with the chat ID (a negative number like `-1001234567890`) and your user ID. Put the chat ID in `.env` as `TELEGRAM_CHAT_ID`, and list who to tag in `TELEGRAM_TAGS`:

```
TELEGRAM_TAGS=@yourname,@friend,123456789:Mum
```

Use `@username`, or `ID:Name` for someone without a username (ask them to send `/id@YourBotName` to get their ID). Stop the bot (Ctrl+C) and start it again.

**6. Test it.**

```
python pokebot.py test       # a tagged sample reminder appears in your group
python pokebot.py check      # reads the official store and lists what it sees (★ = 30th items)
python pokebot.py schedule   # every ping and the exact time it fires
```

---

## Keep it running — pick ONE

### Option A · Your computer (most reliable timing)

```
python pokebot.py run
```

The computer must stay **awake and online** during drop windows. Windows: Settings → System → Power → Sleep = Never (while plugged in). Mac: run `caffeinate -i python3 pokebot.py run`. If it was asleep, it sends the most recent missed ping when it wakes (up to 2 hours late), never a pile of old ones.

### Option B · Free on GitHub (no computer needed)

1. Create a **public** GitHub repository and upload everything in this folder, including `.github/workflows/pokebot.yml`. **Do not upload `.env`.**
2. Repo → Settings → Secrets and variables → Actions → add three secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_TAGS`.
3. Actions tab → enable workflows → open "PokeBot SG" → **Run workflow** to test.

It then runs every 5 minutes. Things to know:

- **GitHub can start runs several minutes late**, especially on the hour (midnight drops are the busiest). Rely on the 1-hour and 10-minute pings rather than the exact-time one. Late pings are reworded ("Out since 12:00 AM, 6 min ago") so nothing is misleading.
- **Why public:** private repos get 2,000 free minutes a month; running every 5 minutes uses about 8,600. Your token, chat ID and tags stay in encrypted secrets, never in the code.
- Lazada blocks cloud servers more than home connections, so the store watcher may be blocked more often here. Reminders are unaffected.
- GitHub pauses schedules after 60 days without commits — edit any file (e.g. add an event) to keep it alive.
- Telegram commands are answered within about 5 minutes instead of instantly.

**Don't run A and B at the same time** — you'd get every ping twice.

---

## Telegram commands

| Command | What it does |
|---|---|
| `/id` | Shows the chat ID and your user ID (works in any chat — for setup) |
| `/next` | Upcoming drops and the next ping for each |
| `/test` | Sends a sample tagged reminder |
| `/status` | Store watcher status: last check, items tracked, problems |
| `/check` | Checks the official store right now |

In groups, add the bot name if needed: `/next@YourBotName`. Only your configured group can trigger tags.

---

## Adding or changing reminders

Each event in `config.yaml`:

```yaml
- name: "Official store drop — Pikachu 30th plush"
  when: "2026-11-20 12:00"            # 24-hour Singapore time, keep the quotes
  remind: ["1d", "1h", "10m", "0m"]   # before the time; "+9h" means 9 hours after
  remind_at: ["2026-11-19 21:00"]     # optional exact extra pings
  link: "https://www.lazada.sg/products/xxxx-i1234567890.html"   # button target (default: official store)
  note: "Limit 1 per customer."
  enabled: true
```

Run `python pokebot.py schedule` afterwards to double-check the times. Changing an event's time re-arms its pings.

To watch a specific official-store product for stock, paste its link under `lazada → products`.

---

## How "official store only" is enforced

- It reads the store's own **All products** pages and **searches inside the store** — official by definition.
- Site-wide search results are kept **only if the seller is the official store**. The bot learns the store's exact Lazada seller name and ID from the store page; anything else is dropped before it's even saved.
- Product links you add are skipped if the page shows a different seller.
- You're only tagged for titles containing one of `match_any` (default: *30th, anniversary, celebration, pokexciting*). Set `match_any: []` to be tagged for every new official listing.
- On its very first check it quietly records what's already listed and posts one silent summary, so you don't get flooded.

## Honest limits

- **Lazada has anti-bot protection.** Some checks may be blocked. The bot slows down automatically (up to once an hour), tells the group after 3 failures in a row, and says when it's working again. Timed reminders never depend on Lazada.
- I couldn't test against live Lazada while building this, so the store reading is written defensively. Run `python pokebot.py check` on your connection — if it lists ★ items, you're set. If it keeps saying "store listing came back empty", Lazada changed its page format.
- Bots can't tag "@all" — only the people you list, and they must be in the group.

## Don't want to run anything?

For reminders only, Telegram can do it natively: type your message with the @tags, **long-press Send → Schedule message**, and pick the time. You lose the store watcher and the countdown wording, but there's nothing to install.

---

**Files:** `pokebot.py` (the bot) · `config.yaml` (events and watcher settings) · `.env.example` (copy to `.env`) · `requirements.txt` · `.github/workflows/pokebot.yml` (Option B) · `state.json` (created automatically — the bot's memory of what it already sent)
