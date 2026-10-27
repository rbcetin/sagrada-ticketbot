# sagrada-ticketbot

Watches the Sagrada Família ticket site for free slots on one day and puts the first one in your cart.

1. Every minute it checks all four ticket types for a slot with room for everyone in your config.
2. New slots are sent to you on Telegram.
3. The earliest new slot is added to a cart and filled in up to the payment page.
4. You connect to that browser and press **PAY** yourself. The bot never pays.

The site holds a cart for 15 minutes, so be ready to act when a message arrives.

## Setup

You need:

- Python 3.10 or newer
- Google Chrome, available as `google-chrome`
- A Telegram bot token (from [@BotFather](https://t.me/BotFather)) and your chat id

Install and configure:

```bash
pip install -r requirements.txt
cp book_config.example.json book_config.json
chmod 600 book_config.json      # it holds passport numbers
```

Then edit `book_config.json`.

## Config

```json
{
  "date": "2026-11-02",
  "telegram": {"token": "123456:BOT-TOKEN", "chat_id": "123456789"},
  "people": [
    {"name": "NAME", "surname": "SURNAME", "passport": "U00000000", "doc_type": "PSP", "issued_by": "OT"}
  ],
  "contact": {"name": "NAME", "surname": "SURNAME", "phone": "905000000000", "email": "you@example.com", "country": "TR"}
}
```

| Field | Meaning |
|---|---|
| `date` | The day to watch (`YYYY-MM-DD`). |
| `telegram` | Where notifications go. |
| `people` | One entry per ticket. **The number of people is the number of tickets.** Names must match the passports: tickets are named and checked at the door. |
| `doc_type` | Optional. Document type, default `PSP` (passport). |
| `issued_by` | Optional. Issuing country, default `OT` (Others). Turkey isn't in the site's list, so use `OT`. |
| `contact` | The buyer's details. The tickets are e-mailed here. |

`book_config.json` is git-ignored. Never commit it.

## Run

Add it to cron to check every minute:

```
* * * * * cd /path/to/sagrada-ticketbot && flock -n .slots.lock python3 slots.py >> slots.log 2>&1
```

Once the date has passed, it stops doing anything.

When a cart is ready, Telegram tells you. Then:

1. Open a tunnel from your computer:
   ```bash
   ssh -N -L 9351:127.0.0.1:9351 <user>@<bot-host>
   ```
2. In Chrome, open `chrome://inspect`, click **Configure…** and add `localhost:9351`.
3. Click **inspect** under the `summaryPurchase` page, check the details and press **PAY**.

You can also fill a cart by hand for a specific slot:

```bash
python3 book.py <productId> <YYYY-MM-DD> <HH:MM>
```

## Ticket types

| Product id | Ticket |
|---|---|
| `4375` | Basilica |
| `4374` | Basilica + guided tour |
| `4443` | Basilica + towers |
| `4779` | Basilica + guided tour + towers |

Guided and tower tickets have two times: the tour or tower slot, and a basilica entry 15–45 minutes earlier. The bot only reports a slot when both have room.

## Files

| File | What it is |
|---|---|
| `slots.py` | The watcher. Run by cron. |
| `book.py` | Fills the cart. Called by `slots.py`. |
| `book_config.json` | Your settings and personal data. Not in git. |
| `slots.log`, `slots_state.json` | Log and the slots already reported. |
| `book_last.png` | Screenshot of the last cart. Contains passport numbers. |

After your visit, delete `book_config.json`, `book_last.png` and `chrome-profile/`.

## How it works

- **Finding slots.** `slots.py` opens the ticket page in headless Chrome and calls the site's own availability API (`services.clorian.com/catalog/events/available`) from inside the page, so it gets a valid reCAPTCHA token. One query per ticket type, one day at a time; the API rejects date ranges.
- **Filling the cart.** `book.py` starts its own headless Chrome and clicks through the site like a person: date, time, number of tickets, names and passports, contact details, consent boxes. It keeps that browser open for 16 minutes.
- **One cart at a time.** After a cart is made, the watcher won't start another one for 16 minutes.
- **Errors.** After 10 failures in a row you get a Telegram message.

## Known issues

- **Form fields need real keystrokes.** If a value is set directly, the site ignores it and later fails with `ORD-494`. The bot types each field character by character.
- **The repeat-email field drops characters when typed fast.** The bot checks the field and retypes it slower if needed.
- **The first CONTINUE click is sometimes ignored.** The bot clicks again after 8 seconds.
- **Only the default `doc_type` and `issued_by` are tested.** Other values may show a different passport-number field.
- **The calendar's green days aren't reliable.** A day can look available and still have no slots. The bot only trusts the per-slot API.
- **Arrive on time.** Late arrivals aren't let in.
