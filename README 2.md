# Polimi residence checker

This project opens the Polimi SOL landing page, selects English from the top-right language menu before LOGIN, logs in, opens the 2026/2027 full-rate accommodation booking table, reads all room counts, and explicitly logs out through the portal account menu. It verifies that the authenticated portal remained in English, so room types in availability notifications come from the English table headers. It never clicks a room. If any count is above zero, it sends a Telegram message when Telegram is configured and always writes the result to the PM2 log. Check failures also trigger a sanitized Telegram error alert.

The script can either start interval checks immediately or wait for an optional Tehran-time start hour, then continue every configured interval. It can alternatively run at 09:00 and 21:00 Tehran time. While that one PM2 process is running, the configured Telegram chat can also use bot buttons to request an immediate check and manage which residences receive detailed room-type reporting.

## Files

- `checker.py`: login, TOTP, availability checks, Telegram buttons/alerts, and both scheduling modes.
- `.env`: your real credentials and settings. It is plaintext, must never be committed or archived, and should remain mode `600`.
- `.bot_state.json`: created automatically on the server after use; stores only the residence catalog, watch preferences, Telegram interface version, and last processed update offset. It is mode `600` and contains no login credentials.
- `requirements.txt`: pinned Python packages.

This folder is currently inside iCloud Drive. File mode `600` blocks other local Unix users, but it does not prevent cloud synchronization or access through the iCloud account.

## Ubuntu setup

Use Python 3.10 or newer. From the uploaded project directory:

```bash
sudo apt update
sudo apt install -y python3 python3-venv

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

sudo .venv/bin/playwright install-deps chromium
.venv/bin/playwright install chromium

chmod 600 .env
```

Make sure the server clock is synchronized because TOTP codes depend on accurate time:

```bash
timedatectl status
```

After filling the Telegram settings, test Telegram without opening the website:

```bash
.venv/bin/python checker.py --test-telegram
```

Then test exactly one website check:

```bash
.venv/bin/python checker.py --once
```

For interval mode, configure the timing in `.env`:

```dotenv
CHECK_INTERVAL_HOURS=12
CHECK_START_HOUR=9
```

`CHECK_START_HOUR` is optional and uses Tehran time. It must be a whole hour from `0` through `23`. When it is set, `CHECK_INTERVAL_HOURS` must divide 24 hours evenly, such as `12`, `8`, `6`, or `1.5`; this keeps the grid stable across midnight and PM2 restarts. The first automatic check waits for the nearest future point in the `CHECK_START_HOUR + n × CHECK_INTERVAL_HOURS` sequence. For example, with a start hour of `9` and a 12-hour interval, the sequence is 09:00, 21:00, 09:00, 21:00; starting the service at 15:00 waits until 21:00. Starting during one of those target minutes runs the first check immediately. If the setting is blank or absent, interval mode checks immediately at startup, with no divide-24 restriction.

Interval timing is start-to-start. A one-hour interval started at 09:00 remains scheduled for 10:00 even if the 09:00 website check takes five minutes; the next wait is about 55 minutes. If one check lasts longer than an entire interval, missed slots are skipped instead of running several checks back-to-back.

The start hour affects only `--mode interval`. It does not change `--once`, `--test-telegram`, or the fixed 09:00/21:00 schedule of `--mode tehran`. A PM2 restart recalculates the nearest future point in the configured start-hour-and-interval sequence.

## Start with PM2

Node and PM2 must already be installed. Check them with `node --version` and `pm2 --version`. Choose one of the following commands while your shell is inside the project directory.

In interval mode, start according to `CHECK_START_HOUR` when it is set, then continue every `CHECK_INTERVAL_HOURS` (12 by default):

```bash
pm2 start ./checker.py --name polimi-residence-checker --interpreter "$(pwd)/.venv/bin/python" -- --mode interval
```

Or check only at 09:00 and 21:00 Tehran time:

```bash
pm2 start ./checker.py --name polimi-residence-checker --interpreter "$(pwd)/.venv/bin/python" -- --mode tehran
```

Do not start both modes under separate PM2 processes.

If PM2 starts or restarts during the 09:00 or 21:00 minute, Tehran mode runs that slot immediately. Repeated restarts within that same minute can therefore repeat a check; the operation remains read-only at the room table.

Useful commands:

```bash
pm2 status
pm2 logs polimi-residence-checker
pm2 restart polimi-residence-checker
pm2 stop polimi-residence-checker
```

For restart after a server reboot, run `pm2 startup`, execute the command it prints, and then run:

```bash
pm2 save
```

## Telegram

Create a Telegram bot, open a private chat with it, and place its bot token and your numeric private-chat ID in `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Both values must be set together. If they remain blank, availability and error reports are visible only through `pm2 logs polimi-residence-checker`.

`--test-telegram` sends only a test message and exits; it does not contact Polimi. Every Telegram delivery is attempted at most three times, with a two-second delay between attempts. During normal checks, configuration, login, timeout, and table-reading failures also attempt a stage-specific error notification without including credentials or raw browser errors.

When the normal PM2 service is running, use the persistent buttons at the bottom of the private bot chat:

- `🔎 Check now` starts an immediate check and always sends a result, including when every room count is zero.
- `🏘 Manage residences` shows the complete residence list and an inline button for every hall.
- `ℹ️ Help` explains the controls again.

In the residence panel, tap any hall to switch it between `✅ Monitored` and `▫️ Total only`. The panel refreshes in place after each selection. `✅ Monitor all`, `📊 Totals only`, and `🔄 Refresh list from SOL` are available at the bottom of that panel. If the bot has never learned the residence list, opening the panel runs one read-only check first and caches the English residence names.

Repeated taps of the same check or refresh request are combined while that request is still pending, preventing accidental back-to-back duplicates. A check and a residence-list refresh remain separate requests, so neither requested result is silently discarded.

After this upgrade, the service sends the keyboard once automatically. With a brand-new Telegram bot chat, press Telegram's built-in **Start** button once; you do not need to type any bot command.

Automatic alerts still trigger when any residence has availability. The first notification line clearly distinguishes `🎉 MONITORED RESIDENCE AVAILABLE`, `📊 ROOMS IN UNMONITORED RESIDENCES`, and the on-demand zero result `😴 NO ROOMS AVAILABLE`. Monitored availability includes a clickable SOL link; other result types do not. When a monitored residence has availability, the message shows detailed room types for monitored residences followed by one accumulated total for every available unmonitored residence. When availability exists only in unmonitored residences, the message shows the same accumulated totals without the urgent monitored-residence header or SOL link. Newly appearing residences default to monitored so a portal change cannot silently hide an opportunity.

`TELEGRAM_CHAT_ID` must identify your private chat with the bot; group chats are not supported by this button interface. Callback presses are checked against both the private chat and the sender. Run only one PM2 instance of this checker because Telegram permits only one active `getUpdates` consumer for a bot.

Keep `.bot_state.json` when updating an existing server installation. Deleting it is safe, but resets the cached list and makes every residence watched again after the next successful check.

## Zip for transfer

From the directory that contains `residence_checker`, create a zip without credentials, local packages, or caches:

```bash
zip -r residence_checker.zip residence_checker \
  -x 'residence_checker/.env' \
     'residence_checker/.bot_state.json' \
     'residence_checker/.bot_state.corrupt-*.json' \
     'residence_checker/.venv/*' \
     'residence_checker/__pycache__/*' \
     'residence_checker/*.log'
```

Transfer and extract that zip, then send `.env` separately over SSH/SCP and run `chmod 600 .env` on the server. Do not place a credential-bearing `.env` in a retained archive.

## Uninstall

```bash
pm2 delete polimi-residence-checker
pm2 save
cd ..
rm -rf residence_checker
```

PM2 can leave the app's old log files. Remove only this app's logs if you no longer need them:

```bash
rm -f ~/.pm2/logs/polimi-residence-checker-out.log
rm -f ~/.pm2/logs/polimi-residence-checker-error.log
```

Playwright installs Chromium under `~/.cache/ms-playwright`. You may remove that directory only if no other Playwright project on the server uses it.
