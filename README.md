# MIT Free Food Alarm Poller

Polls the private MIT `free-foods` Mailman archive and prints `ALARM` when a new
post mentions one of your watched buildings.

## Setup

```bash
python3 -m pip install requests
export FREE_FOODS_EMAIL="bot@example.com"
export FREE_FOODS_PASSWORD="mailman-password"
```

On the Raspberry Pi, use a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install requests gpiozero
```

## Run

```bash
. .venv/bin/activate
python3 free_food_alarm.py --interval 30
```

On each matching post, GPIO 16 pulses for 20 seconds, alternating 0.5 seconds
on and 0.5 seconds off. Use that GPIO to drive the MOSFET gate through the
appropriate resistor/driver circuit. Do not power a motor directly from a GPIO
pin.

Duplicate replies to the same food/location event are suppressed for 60 minutes
by default, and "gone/no more/taken" updates do not trigger the GPIO.
If the Mailman session expires, the poller automatically logs in again and
retries the poll.

```bash
python3 free_food_alarm.py --dedupe-minutes 60 --interval 30
```

For a hardware-free test:

```bash
python3 free_food_alarm.py --dry-run-alarm --interval 30
```

By default, the first run marks existing messages as seen and only alarms on
future posts. To test against already-present archive messages:

When `--archive-url` is not set, the poller recalculates the current monthly
archive on each poll, so it moves from April to May automatically while running.

```bash
python3 free_food_alarm.py --process-existing --once
```

For a fixed month during development:

```bash
python3 free_food_alarm.py \
  --archive-url "https://mailman.mit.edu/mailman/private/free-foods/2026-April/date.html" \
  --once
```

Default watched buildings:

```text
8,12,16,18,24,26,32,34,36,46,48,54,55,56,57,62,64,66,68,76,
E14,E15,E17,E18,E19,E23,E25,E28,NE45
```

Override them when needed:

```bash
python3 free_food_alarm.py --buildings 32,36,W20,E14
```

The seen-message state is stored at `~/.free_food_alarm_seen.json` unless
overridden with `--state`.
