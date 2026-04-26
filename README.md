# MIT Free Food Alarm Poller

Polls the private MIT `free-foods` Mailman archive and prints `ALARM` when a new
post mentions one of your watched buildings.

## Setup

```bash
python3 -m pip install requests
export FREE_FOODS_EMAIL="bot@example.com"
export FREE_FOODS_PASSWORD="mailman-password"
```

On the Raspberry Pi:

```bash
python3 -m pip install requests gpiozero
```

## Run

```bash
python3 free_food_alarm.py --interval 30
```

On each matching post, GPIO 16 is turned on for 10 seconds. Use that GPIO to
drive the MOSFET gate through the appropriate resistor/driver circuit. Do not
power a motor directly from a GPIO pin.

For a hardware-free test:

```bash
python3 free_food_alarm.py --dry-run-alarm --interval 30
```

By default, the first run marks existing messages as seen and only alarms on
future posts. To test against already-present archive messages:

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
