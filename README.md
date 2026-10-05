# Pluxee for Home Assistant

A custom integration that logs into your **Pluxee** (formerly Sodexo) account and
exposes your card balances as Home Assistant sensors.

Built and tested for **Pluxee Austria** (`consumers.pluxee.at`, country `AT`).

## What you get

Per card, the integration creates a **device** with two sensors:

- **Balance** (`sensor.<product>_<last4>_balance`) — current balance in EUR.
  Attributes: product, masked PAN, last 4 digits, status, expiry, per-wallet
  breakdown.
- **Last transaction** (`sensor.<product>_<last4>_last_transaction`) — amount of
  the most recent transaction. Its attributes hold the **recent transaction
  history** (`last_merchant`, `last_date`, and a `transactions` list of the last
  N entries with date / amount / merchant / description).

The session is kept alive automatically using a rotating OAuth refresh token, so
you only log in once (until the refresh token eventually expires, at which point
Home Assistant asks you to re-authenticate).

The UI is translated to **English, German (de) and Ukrainian (uk)**.

## Why login is "copy a URL" instead of typing the OTP

Pluxee's login (email + one-time code) is protected by an invisible **hCaptcha**
on the email step. A captcha can only be solved in a *real* browser, so Home
Assistant cannot send the OTP itself. Instead, you log in once in your normal
browser (where the captcha and OTP work as usual) and paste the resulting
callback URL back into Home Assistant. From then on the integration refreshes the
session in the background without any captcha.

## Installation

### HACS (custom repository)
1. HACS → ⋮ → *Custom repositories* → add this repo, category **Integration**.
2. Install **Pluxee**, then restart Home Assistant.

### Manual
Copy `custom_components/pluxee` into your Home Assistant `config/custom_components/`
folder and restart.

### Copy to your server with PowerShell

The `copy-to-server.ps1` script uses PowerShell 7 and OpenSSH (`ssh` / `scp`)
with key-based authentication.

Copy `.env.example` to `.env` and set `HA_HOST`, `HA_USER`, and
`HA_CUSTOM_COMPONENTS_PATH` (the server's absolute `custom_components` path).
The local `.env` is ignored by Git. Then run:

```powershell
pwsh -File .\copy-to-server.ps1
```
Backups are kept in `config/.pluxee-deployments`, outside `custom_components`
so Home Assistant cannot discover a backup as a duplicate integration. The
script also relocates backups created by older script versions.
Restart Home Assistant after a successful copy.

## Setup

1. *Settings → Devices & Services → Add Integration → Pluxee*.
2. Open the dialog's **/op/ page** link in your normal browser and log in normally.
  Complete any required consent screens; the newsletter checkbox is optional.
3. After logging in, reopen that link to view the `/op/` login page, or
  [open the /op/ page](https://connect.pluxee.app/op/) to inspect cookies.
4. Press **F12** → **Application → Cookies → https://connect.pluxee.app**.
  Copy all `op_session*` rows, including `op_session.sig` and any `.legacy` /
  `.legacy.sig` rows.
5. Paste the rows into **Session cookie** and leave **Callback URL** empty.

The optional callback method appears below the session-cookie field. Use
**the Pluxee login link** in that alternative section, capture the full
`https://consumers.pluxee.at/oidc/callback?code=...` URL before the portal consumes
the one-time code, and submit it immediately (within about 60 seconds).

That's it — your card balance sensors appear under a device per card.

### When the session expires

If the refresh token ever expires or is revoked, Home Assistant raises a
*re-authentication* notification. Click it (or use *Reconfigure* on the
integration), then follow the setup steps above: log in, reopen the **/op/ page**,
and copy the `op_session*` cookies from **F12 → Application → Cookies**.
An interactive consent page can require reauthentication even when the stored
session cookie has not expired; the integration cannot accept terms for you.

As checked on 2026-10-04, the live Austrian portal uses the **EVA Austria** OAuth
client and `openid profile email` scopes. Version **0.3.2** aligns the integration
with that configuration. After updating and restarting Home Assistant, complete
one fresh login if prompted: refresh tokens issued to the previous client cannot
be refreshed using the new client ID. The balance API endpoints are unchanged.

## Adding it to a dashboard

After setup, open your dashboard → *Edit → + Add Card → `Pluxee Card`*.

The integration ships a custom Lovelace card that shows a card's **balance** and
expands to its **latest transactions** when tapped.

 ```yaml
  type: conditional
  conditions:
    - condition: or
      conditions:
        - condition: time
          after: "11:30"
          before: "14:00:00"
          weekdays:
            - mon
            - tue
            - wed
            - thu
        - condition: location
          locations:
            - Work
    - condition: state
      entity: input_boolean.work_vacation # custom boolean to disable the card when on vacation
      state: "off"
  card:
    type: horizontal-stack
    cards:
      - type: custom:pluxee-card
        entity: sensor.meal_pass_1234_balance
      - type: custom:pluxee-card
        entity: sensor.food_pass_5678_balance
  ```

### It installs itself — no manual resource needed

The integration serves the card (`custom_components/pluxee/frontend/pluxee-card.js`)
and registers it as both a Lovelace resource and a frontend module automatically.
Its URL changes whenever the bundled JavaScript changes, avoiding stale frontend
caches. Once the integration is installed and a config entry is set up, the card
appears under *Edit dashboard → + Add Card → "Pluxee Card"* without a manual
resource or cache reset.

Config (the picker adds `type: custom:pluxee-card`; set the entity in YAML):

```yaml
type: custom:pluxee-card
entity: sensor.meal_pass_1234_balance
# transactions_entity is auto-derived (…_balance -> …_last_transaction); override if needed
# title: Meal Pass
# max_transactions: 10
```

Add one card per Pluxee card. Tap the row to toggle the transaction list
(amounts are colour-coded: spending in red, top-ups in green).

### Does HACS install it?

Yes — when you install **this integration** through HACS, the card comes with it
and auto-loads (above). You do **not** need a separate HACS "Dashboard/Lovelace"
plugin or a manual resource entry. (A single HACS repo is one category, so the
card is bundled with the integration rather than published as a standalone HACS
plugin.)

### Manual fallback

If the card doesn't appear (e.g. you disabled auto-loaded modules, or run a
stripped-down frontend), register it by hand:

1. Copy `custom_components/pluxee/frontend/pluxee-card.js` to `config/www/`
   (served at `/local/pluxee-card.js`).
2. *Settings → Dashboards → ⋮ → Resources → + Add resource* →
   URL `/local/pluxee-card.js`, Type **JavaScript Module**.
3. Hard-refresh (Ctrl+F5).

## Other ways to add

- **Quick way:** choose *By entity* (or the *Entities* card) and pick the
  `... balance` / `... last transaction` entities. Or use the auto-generated
  device page: *Settings → Devices & Services → Pluxee → (your card)* shows the
  card with its sensors, which you can add to a dashboard directly.
- **Balances at a glance** — an Entities card:

  ```yaml
  type: entities
  title: Pluxee
  entities:
    - entity: sensor.meal_pass_1234_balance
    - entity: sensor.food_pass_5678_balance
  ```

- **Transaction history (detailed view)** — the history lives in the
  `transactions` attribute of the *Last transaction* sensor. Render it with a
  Markdown card:

  ```yaml
  type: markdown
  title: Meal Pass — recent transactions
  content: >
    {% for t in state_attr('sensor.meal_pass_1234_last_transaction',
    'transactions') %}
    - **{{ t.amount }} {{ t.currency }}** · {{ t.merchant }} · {{
    as_timestamp(t.date) | timestamp_custom('%d.%m.%Y') }}
    {% endfor %}
  ```

  You can also just click the *Last transaction* sensor to open its more-info
  dialog and see the full list under *Attributes*.

## Options

*Configure* on the integration lets you set:

- **Update interval** (hours, default 6). One poll per day is plenty to keep the
  session alive.
- **Number of recent transactions** to keep per card (default 15, `0` disables
  fetching transactions).

## How it works (technical)

- OAuth2 Authorization Code + PKCE against `https://connect.pluxee.app/op`
  (public client, country AT).
- Balances come from a single call to
  `GET https://api.pluxee.app/gl/cwc/consumer-front-api/v3/spl/cardsInfos`
  with the bearer token, the API subscription key, and `Country-Code: AT`.
- Refresh tokens **rotate** on every refresh; the newest one is always persisted
  back to the config entry.

## Development / tests

The integration logic is covered by tests using
`pytest-homeassistant-custom-component`:

```bash
pip install pytest-homeassistant-custom-component
pytest tests
```

The `explore/` folder contains the scripts used to reverse-engineer the API
(not part of the integration).

### Sodexo Connect Client IDs (`sodexoConnectClientId`)
These are used for authentication via the Sodexo Connect platform. If you want to try different country, try replacing the ID in `const.py` (maybe someone wants to open PR and implement an auto-detection - welcome!):

| Country Code | GUID |
| :--- | :--- |
| **AT** (Austria) | `b8425d52-0bfb-4130-9cf2-bd27f0188901` |
| **BE** (Belgium) | **changed since last update** |
| **BG** (Bulgaria) | **changed since last update** |
| **DE** (Germany) | **changed since last update** |
| **LU** (Luxembourg) | **changed since last update** |
| **RO** (Romania) | **changed since last update** |
| **TN** (Tunisia) | **changed since last update** |

## Disclaimer

Unofficial. Not affiliated with or endorsed by Pluxee / Sodexo. Use at your own risk; the private API may change at any time.
