# Polimi Residence Checker

An unofficial Python service that checks Polimi student-residence availability
and sends Telegram alerts.

It supports scheduled and on-demand checks, configurable academic years,
per-user residence monitoring and notification schedules, a button-based
Telegram interface, and continuous operation with PM2.

> [!IMPORTANT]
> This project is not affiliated with or endorsed by Politecnico di Milano.
> Users are responsible for complying with the university portal's terms,
> policies, and access rules. The portal can change without notice and may
> require corresponding updates to this project.

## What it does

The checker:

- Selects English on the SOL landing page.
- Signs in through Polimi SSO using a person code, password, and TOTP.
- Opens the configured academic year's full-rate accommodation section.
- Handles the required declaration pages.
- Reads every room count from the English availability table.
- Sends distinct Telegram messages based on the result.
- Registers people who start the bot and stores separate preferences for each.
- Allows each person to monitor or summarize residences through Telegram buttons.
- Lets each person turn scheduled alerts on/off and select a multiple of the
  global check interval without adding website checks.
- Sends scheduled results to configured administrators immediately and can
  delay delivery to other subscribers.
- Supports interval-based and fixed Tehran-time schedules.
- Explicitly logs out after each check.
- Reports configuration, browser, portal-flow, table, and logout failures.

The checker is read-only at the availability table. It never selects or books
a room.

## Portal flow

Each check follows this sequence:

1. Open the [Polimi SOL portal](https://polimi-sol.dirittoallostudio.it/apps/V3.1/sol/public/index.php).
2. Select English from the top-right language menu.
3. Open LOGIN and continue to Polimi SSO.
4. Submit the configured person code and password.
5. Generate and submit the current TOTP code.
6. Verify that the authenticated SOL portal is still in English.
7. Open the academic year configured by `ACADEMIC_YEAR`.
8. Open **Student accommodation at FULL RATE → Accommodation Booking**.
9. Select **YES** on the accommodation declaration and click
   **Save & Continue**, when shown.
10. Optionally handle the additional **University residences — Details of the
    university residences** declaration.
11. Wait for the availability table to finish loading.
12. Read every residence and room-type count.
13. Send or log the result.
14. Log out through the SOL account menu.

SOL may remember previously completed declarations and open the availability
table directly. The checker accepts that path as well.

## Features

- Configurable academic year
- English room and residence names
- Headless Chromium automation with Playwright
- TOTP generation from an `otpauth://` URI
- Scheduled checks with stable start-to-start timing
- Fixed 09:00 and 21:00 Tehran schedule
- On-demand checks from Telegram
- Multiple administrator chat IDs
- Per-user residence monitoring
- Per-user scheduled notification on/off setting
- Per-user interval multiples with no extra SOL checks
- Optional delayed subscriber delivery
- Detailed monitored-room alerts
- Accumulated totals for unmonitored residences
- Clickable SOL link in urgent alerts
- Telegram delivery retries
- Sanitized failure notifications
- Explicit and verified logout
- PM2 support
- No Docker requirement

## Requirements

- Python 3.10 or newer
- Chromium installed through Playwright
- An accurately synchronized system clock
- A Polimi account with TOTP authentication
- A Telegram bot and at least one administrator private-chat ID for Telegram features
- Node.js and PM2 for optional continuous server operation

## Project files

| File               | Purpose                                                              |
|--------------------|----------------------------------------------------------------------|
| `checker.py`       | Browser automation, scheduling, Telegram controls, and notifications |
| `requirements.txt` | Pinned Python dependencies                                           |
| `.env.example`     | Safe configuration template                                          |
| `.env`             | Local credentials and settings; never committed                      |
| `.gitignore`       | Excludes secrets, state, caches, logs, OS files, and IDE files       |
| `.bot_state.json`  | Automatically created local bot state                                |

`.bot_state.json` contains the learned residence catalog, registered private
chats, their monitoring and delivery preferences, Telegram interface versions,
and the processed-update offset. It does not contain Polimi credentials or the
Telegram bot token.

## Installation

### 1. Clone the public repository

```bash
git clone https://github.com/AmirMohammadFakhimi/polimi-residence-checker.git
cd polimi-residence-checker
```

### 2. Create the configuration file

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` and provide the required values. Never commit this file.

### 3. Install on Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-venv

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

sudo .venv/bin/playwright install-deps chromium
.venv/bin/playwright install chromium
```

Check clock synchronization because TOTP codes depend on accurate time:

```bash
timedatectl status
```

### 4. Install on macOS

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

## Configuration

The project reads `.env` from its own directory. Operating-system environment
variables take precedence over values in the file.

```dotenv
# Polimi credentials
POLIMI_USERNAME=
POLIMI_PASSWORD=
POLIMI_TOTP_URI=

# Target
ACADEMIC_YEAR=2026/2027

# Schedule
CHECK_START_HOUR=9
CHECK_INTERVAL_HOURS=12
SUBSCRIBER_NOTIFICATIONS_ENABLED=true
DELAYED_NOTIFICATIONS_ENABLED=true
NOTIFICATION_DELAY_MINUTES=5
INCLUDE_RESIDENCE_NOTICE_PAGE=true

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_IDS=
```

### Configuration reference

| Variable                           | Required | Description                                                                                                |
|------------------------------------|----------|------------------------------------------------------------------------------------------------------------|
| `POLIMI_USERNAME`                  | Yes      | Polimi person code                                                                                         |
| `POLIMI_PASSWORD`                  | Yes      | Polimi account password                                                                                    |
| `POLIMI_TOTP_URI`                  | Yes      | Complete `otpauth://totp/...` URI                                                                          |
| `ACADEMIC_YEAR`                    | No       | Target year in consecutive `YYYY/YYYY` format; default `2026/2027`                                         |
| `CHECK_START_HOUR`                 | No       | Tehran-time anchor for interval mode; blank means check immediately at startup                             |
| `CHECK_INTERVAL_HOURS`             | No       | Hours between scheduled starts; default `12`                                                               |
| `SUBSCRIBER_NOTIFICATIONS_ENABLED` | No       | `false` suppresses all outgoing messages to chats outside `TELEGRAM_CHAT_IDS`; default `true`              |
| `DELAYED_NOTIFICATIONS_ENABLED`    | No       | `true` delays non-administrator notifications; `false` sends due notifications immediately; default `true` |
| `NOTIFICATION_DELAY_MINUTES`       | No       | Delay for non-administrator notifications; finite number of minutes, zero or more; default `5`             |
| `INCLUDE_RESIDENCE_NOTICE_PAGE`    | No       | Allow the additional University residences declaration; default `true`                                     |
| `TELEGRAM_BOT_TOKEN`               | No       | Telegram bot token                                                                                         |
| `TELEGRAM_CHAT_IDS`                | No       | Comma-separated administrator numeric private-chat IDs                                                     |

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_IDS` must either both be set or both be
left blank.

### Academic year

The academic year must contain consecutive four-digit years:

```dotenv
ACADEMIC_YEAR=2027/2028
```

The first year determines the SOL panel selector. For example:

```text
2026/2027 → #aa2026
2027/2028 → #aa2027
```

The configured value is also used in logs and Telegram messages. Invalid or
nonconsecutive values fail during configuration before Chromium starts.

### Additional University residences declaration

The portal may show an additional page titled:

```text
University residences - Details of the university residences
```

To allow the checker to select **YES** and continue:

```dotenv
INCLUDE_RESIDENCE_NOTICE_PAGE=true
```

The page is identified using its heading, YES radio control, and visible
**Save & Continue** button together. This prevents it from being confused with
the preceding declaration, which reuses a similar radio control.

To prevent the checker from accepting that declaration:

```dotenv
INCLUDE_RESIDENCE_NOTICE_PAGE=false
```

If the page appears while disabled, the check stops safely and reports a
specific error. Direct access to the table remains valid with either setting,
so removal of the temporary page does not break the flow.

## Usage

### Test Telegram only

```bash
.venv/bin/python checker.py --test-telegram
```

This sends one test message and exits without opening the Polimi website.

### Run one website check

```bash
.venv/bin/python checker.py --once
```

This performs one complete check, reports any result or failure, logs out, and
exits.

### Run continuously in interval mode

```bash
.venv/bin/python checker.py --mode interval
```

### Run continuously at fixed Tehran times

```bash
.venv/bin/python checker.py --mode tehran
```

## Scheduling

### Interval mode

When `CHECK_START_HOUR` is blank, the first check runs immediately. Later
checks use `CHECK_INTERVAL_HOURS`.

When `CHECK_START_HOUR` is set, it must be a whole hour from `0` through `23`
in Tehran time. The next check is selected from:

```text
CHECK_START_HOUR + n × CHECK_INTERVAL_HOURS
```

For example:

```dotenv
CHECK_START_HOUR=9
CHECK_INTERVAL_HOURS=12
```

produces:

```text
09:00 → 21:00 → 09:00 → 21:00
```

Starting the process at 15:00 waits until 21:00.

When a start hour is configured, the interval must divide 24 hours evenly.
Examples include `12`, `8`, `6`, `4`, `3`, `2`, and `1.5`.

Intervals are measured **start-to-start**. If an hourly check starts at 09:00
and takes five minutes, the next start remains 10:00. If a check takes longer
than an entire interval, missed starts are skipped rather than executed in a
burst.

Each Telegram user can select a positive whole-number multiplier of this global
interval. For example, with `CHECK_INTERVAL_HOURS=2`, a user selecting `3×`
is due every third completed scheduled check, or every six hours. This changes
only delivery frequency: it never starts an additional SOL check. A changed
multiplier begins a fresh personal cycle.

### Tehran mode

`--mode tehran` runs at:

- 09:00 Tehran time
- 21:00 Tehran time

This mode ignores `CHECK_START_HOUR`.

## Telegram

### Setup

1. Create a bot using Telegram's BotFather.
2. Have every intended recipient open a private chat with the bot and press
   Telegram's built-in **Start** button once.
3. Obtain the bot token and numeric private-chat IDs for the administrators.
4. Add the token and comma-separated administrator IDs to `.env`.

For example:

```dotenv
TELEGRAM_CHAT_IDS=123456789,987654321
```

Group chats are not supported. Private-chat actions are checked against the
sender, and each person is stored after interacting with the bot. Telegram does
not expose a list of people who have not interacted with the running service.

If Telegram is not configured, results remain available in the terminal or
PM2 logs.

### Bot controls

The bot uses buttons instead of typed commands:

- `🔎 Check now`: runs a fresh check and always sends the result; visible only
  to configured administrators.
- `🏘 Manage residences`: shows the complete residence list.
- `⚙️ Notification settings`: turns scheduled alerts on/off and changes the
  user's interval multiplier.
- `ℹ️ Help`: explains the available controls.

The residence manager supports:

- `✅ Monitored`: include detailed room types and trigger an urgent alert.
- `▫️ Total only`: include only one accumulated total for the residence.
- `✅ Monitor all`: mark every known residence as monitored.
- `📊 Totals only`: mark every known residence as unmonitored.
- `🔄 Refresh list from SOL`: reload the complete English residence list;
  available only to configured administrators.

If the bot has not learned the residence catalog yet, opening the residence
manager as an administrator performs one read-only SOL check first. Other users
are asked to wait for the next scheduled check. Newly discovered residences
default to monitored for every user.

Duplicate taps for the same pending check or refresh are combined.

### Scheduled delivery

One SOL check produces one shared availability result. The bot then renders
that result separately for each due recipient using that person's monitored
residences.

- Configured administrators receive due scheduled availability alerts first.
- With `SUBSCRIBER_NOTIFICATIONS_ENABLED=false`, nobody outside
  `TELEGRAM_CHAT_IDS` receives alerts, replies, controls, message edits, or
  callback responses, regardless of personal settings.
- Other enabled users receive due alerts after `NOTIFICATION_DELAY_MINUTES`
  when `DELAYED_NOTIFICATIONS_ENABLED=true`.
- With `DELAYED_NOTIFICATIONS_ENABLED=false`, all due alerts are sent
  immediately after the same check.
- A user's on/off setting and interval affect scheduled availability alerts.
  Controls remain usable while alerts are off.
- Failures are sent to configured administrators; automatic zero-availability
  checks remain silent, as before.

The delay runs independently from the website checker and Telegram controls.
It does not delay the next SOL check or create another one.

### Notification types

#### Monitored availability

```text
🎉 MONITORED RESIDENCE AVAILABLE
```

The message contains:

- Detailed room types for available monitored residences
- Accumulated totals for available unmonitored residences
- An urgent action message
- A clickable SOL link

#### Unmonitored-only availability

```text
📊 ROOMS IN UNMONITORED RESIDENCES
```

This is an informational alert with accumulated totals and no urgent SOL link.

#### No availability

```text
😴 NO ROOMS AVAILABLE
```

This is sent after an on-demand check when every count is zero. Automatic
zero-result checks remain silent.

#### Failure

```text
⚠️ CHECK FAILED
```

The message contains a sanitized reason and recommends reviewing the process
logs. Credentials, TOTP secrets, raw browser errors, and page HTML are not
included.

Telegram delivery is attempted up to three times with a two-second delay
between attempts.

## Run continuously with PM2

PM2 is optional and is used only to keep the Python process running.

Install it:

```bash
npm install -g pm2
```

### PM2 interval mode

```bash
pm2 start ./checker.py \
  --name polimi-residence-checker \
  --interpreter "$(pwd)/.venv/bin/python" \
  -- --mode interval
```

### PM2 fixed Tehran schedule

```bash
pm2 start ./checker.py \
  --name polimi-residence-checker \
  --interpreter "$(pwd)/.venv/bin/python" \
  -- --mode tehran
```

Do not run both modes or multiple instances for the same Telegram bot.
Telegram permits only one active `getUpdates` consumer per bot.

### PM2 commands

```bash
pm2 status
pm2 logs polimi-residence-checker
pm2 restart polimi-residence-checker
pm2 stop polimi-residence-checker
pm2 delete polimi-residence-checker
```

To restore the service after a system reboot:

```bash
pm2 startup
```

Run the command printed by PM2, then save the process list:

```bash
pm2 save
```

## Runtime state

The bot creates `.bot_state.json` automatically with permission `600`. It
stores:

- Known residence names
- Registered user chat IDs, names, and usernames
- Per-user on/off state, interval multiplier, and delivery-cycle position
- Per-user monitored/unmonitored preferences
- Per-user Telegram interface version
- Last processed Telegram update

Keep this file when updating an installation. Deleting it is safe, but resets
the registered-user list, residence catalog, and monitoring preferences.
Configured administrators are recreated at startup; other users must press
**Start** again. All residences default to monitored after the next successful
check.

## Logging and failures

The checker writes timestamped status messages to standard output. With PM2:

```bash
pm2 logs polimi-residence-checker
```

Configuration, login, browser, timeout, portal-flow, table-reading, and logout
failures attempt a Telegram notification.

If availability was read successfully but logout fails, the room result is
reported first and the logout failure is reported afterward.

## Updating

```bash
cd /path/to/polimi-residence-checker
git pull
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

For a PM2 installation:

```bash
pm2 restart polimi-residence-checker
pm2 logs polimi-residence-checker
```

`.env` and `.bot_state.json` are ignored by Git and remain in place during a
normal pull.

## Security

- Never commit `.env`.
- Commit `.env.example` only with empty credentials.
- Keep `.env` permission `600`.
- Treat the Polimi password, TOTP URI, and Telegram bot token as secrets.
- Avoid placing secrets in shell commands, logs, issue reports, screenshots,
  or Git remote URLs.
- Rotate any secret that is accidentally exposed.
- Remember that adding a file to `.gitignore` does not remove it from existing
  Git history.

Confirm that `.env` is ignored:

```bash
git check-ignore -v .env
```

## Troubleshooting

### Chromium fails to start

Reinstall the dependencies and browser:

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

On Ubuntu:

```bash
sudo .venv/bin/playwright install-deps chromium
```

### Login or TOTP fails

- Verify all three Polimi credential settings.
- Ensure `POLIMI_TOTP_URI` is the complete `otpauth://` URI.
- Check system time synchronization.

On Ubuntu:

```bash
timedatectl status
```

### The configured academic year is rejected

Use consecutive four-digit years separated by `/`:

```dotenv
ACADEMIC_YEAR=2027/2028
```

### The University residences notice is disabled

If the portal shows the additional notice, set:

```dotenv
INCLUDE_RESIDENCE_NOTICE_PAGE=true
```

Restart the checker afterward.

### Telegram does not work

Set both Telegram variables and run:

```bash
.venv/bin/python checker.py --test-telegram
```

### The portal flow changed

Run one check in the foreground:

```bash
.venv/bin/python checker.py --once
```

Review the sanitized error and process logs. Changes to portal text, structure,
or workflow may require updated Playwright selectors.

## Uninstall

Delete the PM2 process if present:

```bash
pm2 delete polimi-residence-checker
pm2 save
```

Remove the project directory:

```bash
cd ..
rm -rf polimi-residence-checker
```

PM2 can retain log files after process deletion:

```bash
rm -f ~/.pm2/logs/polimi-residence-checker-out.log
rm -f ~/.pm2/logs/polimi-residence-checker-error.log
```

Playwright normally stores downloaded browsers under
`~/.cache/ms-playwright`. Remove that directory only if no other Playwright
project uses it.

## Contributing

Portal-flow fixes, selector updates, documentation improvements, and bug
reports are welcome. Never include credentials, TOTP URIs, Telegram tokens,
session cookies, or personally identifying portal HTML in issues or pull
requests.
