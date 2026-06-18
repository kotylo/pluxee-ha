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

## Setup

1. *Settings → Devices & Services → Add Integration → Pluxee*.
2. The dialog shows a **login link**. Open it in your normal web browser.
3. Log in with your Pluxee email, solve the captcha, and enter the one-time code
   emailed to you.
4. Your browser lands on a page that just shows a **loading spinner** — that is
   expected. Copy the **full address-bar URL** (it starts with
   `https://consumers.pluxee.at/oidc/callback?code=...`).
5. Paste that URL back into the Home Assistant dialog **within ~60 seconds**
   (the code expires quickly). If it expires, just reopen the link and retry.
6. This token only is valid for 6 hours, so to automatically extend the session, **copy session key/value cookie** from the browser's developer tools (F12 → Application → Storage → Cookies for
   `consumers.pluxee.at`):
   - `op_session`
   - `op_session.legacy.sig`
   - `op_session.legacy`
   - `op_session.sig`
   
   Paste those into the dialog to have a stable session.

That's it — your card balance sensors appear under a device per card.

### When the session expires

If the refresh token ever expires or is revoked, Home Assistant raises a
*re-authentication* notification. Click it (or use *Reconfigure* on the
integration) and repeat the same login-link → paste-URL steps as well as the cookie copy step.

## Adding it to a dashboard

After setup, open your dashboard → *Edit → + Add Card → `Pluxee Card`*.

The integration ships a custom Lovelace card that shows a card's **balance** and
expands to its **latest transactions** when tapped.

### It installs itself — no manual resource needed

The integration serves the card (`custom_components/pluxee/frontend/pluxee-card.js`)
and registers it as a frontend module automatically. So once the integration is
installed and a config entry is set up, just **restart Home Assistant and
hard-refresh your browser (Ctrl+F5)** — then *Edit dashboard → + Add Card →
"Pluxee Card"* appears in the list.

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
| **AT** (Austria) | `568135b2-84c2-46a1-b471-9c34238ed924` |
| **BE** (Belgium) | `c6d7526f-b20b-40d4-bd4a-09e8701eb4a1` |
| **BG** (Bulgaria) | `23e18b22-e628-4277-beec-14cd5a8af660` |
| **DE** (Germany) | `6c12015c-07be-45e8-b930-7d4d457876d4` |
| **LU** (Luxembourg) | `8efd36ca-99d2-4943-90cc-74d2b25788c2` |
| **RO** (Romania) | `baa95e6c-bc57-4543-bd06-0997f46505d5` |
| **TN** (Tunisia) | `abf8a6ed-ef49-41a8-b5e4-ec5e48245313` |

## Disclaimer

Unofficial. Not affiliated with or endorsed by Pluxee / Sodexo. Use at your own risk; the private API may change at any time.
