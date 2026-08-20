# Canyon XS endurance-bike watcher

Watches Canyon's UK Endurace range for **size XS** variants that are buyable
(`InStock` or `PreOrder`) **between £1,500 and £4,000**, and pushes a phone
alert **only when that set actually changes** — never a daily "still
available" ping.

Checks every **3 minutes** (see [Polling](#polling-why-not-just-cron)).

The band drops the entry-level and top-spec bikes. As of 20 Aug 2026 five
models are in scope, 15 XS colour variants in total:

| Model | XS price | In scope |
|---|---|---|
| Endurace AllRoad | £1,099 | no — below band |
| Endurace CF 6 | £1,649 | no — excluded by name |
| Endurace CF 7 | £2,199 | **yes** |
| Endurace CF 7 AXS | £2,849 | **yes** |
| Endurace CF 8 Di2 | £3,149 | **yes** |
| Endurace CF SLX 7 Di2 | £3,799 | **yes** |
| Endurace CF SLX 7 AXS | £3,799 | **yes** |
| Endurace CF SLX 8 Di2 | £4,299 | no — above band |
| Endurace CF SLX 9 Di2 | £6,649 | no — above band |
| Endurace CFR Di2 / CFR AXS | £8,500 | no — above band |

Filtering happens on the **live** price, so a discount pulls a bike into
scope on its own: if the SLX 8 is ever cut to £3,899, it starts being watched.

A variant whose price can't be parsed is **kept**, not dropped — if Canyon
changes their markup, an unwanted alert beats a bike silently falling off
the watch list.

(The Endurace CF SLX 8 Di2 in Champagne was pre-orderable on 6 Aug and had
gone by the next morning — which is the whole reason this exists. The XS
SLX 7 AXS then came and went on 20 Aug without an alert, which is why the
polling model changed; see below.)

## How it works

Canyon's "In-Stock Bikes" tool looks like it filters client-side, but it
doesn't — it's Salesforce Commerce Cloud, and the filter state lives in the
URL as server-side refinements:

```
?prefn1=pc_rahmengroesse&prefv1=XS      # frame size
&prefn2=pc_ride_style&prefv2=Endurance  # ride style
```

So **no headless browser is needed**. That removes the whole class of false
positives the brief worried about: no cookie banners, no ad frames, no
waiting for JS to settle.

Stock status comes from each product page's JSON-LD `ProductGroup`, which
lists every colour × size variant with a real `InStock` / `OutOfStock` /
`PreOrder` value. Variants are keyed by **SKU**, so a model that's in stock
in Dark Matter but sold out in Pro Black is tracked as two separate things —
you get told exactly which colour appeared.

## Polling: why not just cron

The workflow cron says every 15 minutes. GitHub does not honour that. Measured
over 10 days on this repo: **300 scheduled runs where 963 were due** — a 42
minute median gap between checks, 77 at the 90th percentile, and a worst case
of 163 minutes. GitHub deprioritises `schedule` triggers under load and there
is no way to buy your way out of it on a public repo.

That lost a bike. The XS SLX 7 AXS came into stock and sold out inside one of
those gaps, so it was never recorded — and because "no longer available" only
fires for a SKU the watcher had previously *seen*, there was no alert on the
way in **or** the way out. A miss was completely silent, which is the worst
possible failure for this thing.

The fix is to stop depending on cron resolution. The cron only *starts* a run;
each run then polls in a loop for ~50 minutes, checking every 3 minutes. Inside
a live runner `sleep()` is exact. The workflow's concurrency group keeps one run
live and one queued, so as one loop finishes the next begins — effectively
continuous coverage, and about 0.06 requests/second against Canyon.

State is written when the item set actually changes, plus once at the end of
the run, so a 50-minute loop produces one commit rather than twenty.

Note the in-stock tool alone would *miss pre-orders*: it only lists
already-shippable bikes. Reading the product pages catches those too.

## Setup

### 1. Get push notifications on your phone

1. Install **ntfy** ([iOS](https://apps.apple.com/app/ntfy/id1625396347) /
   [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
2. Tap **+** and subscribe to a topic. Choose something long and unguessable —
   on the public server the topic name *is* the only secret, e.g.
   `canyon-xs-REPLACE-WITH-YOUR-OWN-RANDOM-STRING`.
3. In iOS: Settings → Notifications → ntfy → allow **Time Sensitive** alerts,
   so high-priority "NOW IN STOCK" pings cut through Focus modes.

### 2. Push this repo to GitHub

Make it **public** — Actions minutes are then unlimited and free. Nothing
sensitive lives in the code; the topic name goes in Secrets.

```bash
gh repo create canyon-xs-watch --public --source=. --push
```

### 3. Add the topic as a secret

```bash
gh secret set NTFY_TOPIC --body "canyon-xs-REPLACE-WITH-YOUR-OWN-RANDOM-STRING"
```

Optional: `NTFY_TOKEN` if you use a private ntfy server, and a repo variable
`NTFY_SERVER` to point at it.

### 4. Test it

Actions tab → **Canyon XS watch** → **Run workflow**. The first run records a
baseline and sends one summary notification. After that it's silent until
something genuinely changes.

## Alerts you'll get

| Event | Priority |
|---|---|
| New XS bike **in stock** | 5 (max — breaks through silent mode) |
| New XS **pre-order** open | 4 |
| `PreOrder` → `InStock` | 5 |
| Variant no longer available | 2 (quiet) |
| Scraper broken 3 checks running | 4 |

Every alert includes the direct link in the message body **and** as a
tap-through, deep-linked to that exact colour and size:

```
NOW IN STOCK: Endurace CFR Di2 XS
Endurace CFR Di2 - Dark Matter (XS)
£8,500 - InStock
https://www.canyon.com/...4434.html?dwvar_4434_pv_rahmenfarbe=R148_P03&dwvar_4434_pv_rahmengroesse=XS
```

## Avoiding false alarms

- Compares a **set of SKUs**, not counts or page hashes — so a swap (one in,
  one out) is caught, and a reordered page is not mistaken for a change.
- Every request retries 3× with exponential backoff.
- A failed run **never** reports bikes as gone: it keeps the previous state
  untouched and exits.
- "No longer available" only fires for a product whose page was successfully
  re-read this run — one flaky request can't fake a sell-out.
- If the listing returns implausibly few products, the run aborts rather than
  believing the range vanished.
- Because a silently-broken scraper is worse than none, 3 consecutive
  failures trigger a warning notification.

## Changing what's watched

Defaults are overridable by environment variable:

| Variable | Default | Notes |
|---|---|---|
| `CANYON_SIZES` | `XS` | Comma-separated, e.g. `XS,2XS,XS/S` |
| `CANYON_AVAILABILITY` | `InStock,PreOrder` | Drop `PreOrder` for in-stock only |
| `CANYON_MIN_PRICE` | `1500` | Inclusive, in the listing currency |
| `CANYON_MAX_PRICE` | `4000` | Inclusive |
| `CANYON_EXCLUDE_MODELS` | `Endurace CF 6` | Comma-separated, matched on the exact model name (case-insensitive) |
| `CANYON_POLL_INTERVAL` | `180` | Seconds between checks within a run |
| `CANYON_RUN_DURATION` | `3000` | Seconds a run keeps polling; keep below the job's `timeout-minutes` |
| `CANYON_CATEGORY_URL` | Endurace XS listing | Any Canyon category URL |

`CANYON_EXCLUDE_MODELS` matches the whole name, so it won't accidentally catch
`Endurace CF 6 Something-Else` if Canyon renames a bike — you'd get an alert
you don't want rather than losing one you do.

Canyon's other refinement values, found while reverse-engineering the page:

- `pc_rahmengroesse`: `3XS 2XS XS S S/M M M/L L XL 2XL XS/S`
- `pc_ride_style`: `Endurance Race Aero Gravel Trail Cross-country Triathlon …`
- `pc_familie`: `Endurace Ultimate Aeroad Grail Grizl Inflite …`

To widen to every endurance road bike rather than just the Endurace family:

```
CANYON_CATEGORY_URL="https://www.canyon.com/en-gb/road-bikes/?prefn1=pc_rahmengroesse&prefv1=XS&prefn2=pc_ride_style&prefv2=Endurance&sz=100"
```

## Running locally

```bash
NTFY_TOPIC=your-topic python3 canyon_watch.py
```

Without `NTFY_TOPIC` it prints alerts to the console instead of sending them.

That runs the full ~50 minute polling loop. For a single check:

```bash
CANYON_RUN_DURATION=1 python3 canyon_watch.py
```

To see everything in the band regardless of stock — useful for checking the
filters are catching what you expect:

```bash
CANYON_AVAILABILITY=OutOfStock CANYON_RUN_DURATION=1 python3 canyon_watch.py
```
