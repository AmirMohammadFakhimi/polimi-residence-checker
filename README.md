# polimi-residence-checker
An unofficial Python bot that monitors Polimi residence availability, supports scheduled and on-demand checks, and sends customizable Telegram alerts.

## Temporary residence-notice page

The current accommodation flow contains an additional **University residences
— Details of the university residences** declaration between the privacy step
and the availability table. Configure its handling in `.env`:

```dotenv
INCLUDE_RESIDENCE_NOTICE_PAGE=true
```

With `true`, the checker selects **YES** on that page and clicks
**Save & Continue**. Change it to `false` if Polimi removes the page. If the
page appears while the setting is `false`, the checker stops safely and sends
a specific failure notification instead of interacting with it.
