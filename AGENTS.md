# AGENTS.md — Pluxee Home Assistant integration

Context for working on this repo without re-researching. Read this first.

## What this is
A Home Assistant **custom integration** (`custom_components/pluxee/`) that logs
into a **Pluxee Austria** consumer account and exposes card balances and recent
transactions as sensors. Unofficial; built by reverse-engineering the
`consumers.pluxee.at` SPA + `api.pluxee.app`.

Test account: `somesodexo@gmail.com`, country `AT`, 2 cards (Meal Pass SPAML,
Food Pass SPAFX).

## ⚠️ Privacy — sanitize before finishing (NEVER commit real account data)
When investigating the live API you WILL handle real values (card numbers, PANs,
card/consumer/wallet IDs, balances, merchant names, transaction dates). **Before
you finish / hand off / commit, replace every real value that landed in tracked
files** (tests, fixtures, code comments/docstrings, README, this file) **with
fictitious placeholders.** Conventions used here:
- Card/consumer/benefit IDs → `TESTCARD…`, `TESTCONSUMER…`, `TESTBENEFIT…`; wallet
  id → `WALLET123`.
- PANs → `XXXX 1234` / `XXXX 5678` (last4 `1234` / `5678`).
- Balances → `83.11` (amount value `8311`) and `6.30` (value `630`).
- Merchants/places → `Test Lounge`, `Test Market` (Test-prefixed, never real names).
Delete scratch files that captured live JSON/tokens (put them in the OS temp /
scratchpad, never the repo). Before done, grep the repo for any real value you
saw and confirm zero hits, e.g.
`grep -rIE '<real-last4>|<real-merchant>' custom_components tests README.md`.
`creds.txt` and `explore/tokens.json` are gitignored — keep it that way.

## Repo layout
- `custom_components/pluxee/` — the integration (the deliverable).
  - `const.py` — all endpoints, client id, subscription key, config keys, defaults.
  - `api.py` — standalone async client: PKCE/authorize helpers, token exchange/refresh,
    `PluxeeClient` (manages rotating tokens), data models (`Card`, `Wallet`, `Transaction`,
    `PluxeeData`), `async_get_data()`, `async_get_transactions()`.
  - `coordinator.py` — `PluxeeCoordinator` (DataUpdateCoordinator); persists rotated tokens.
  - `config_flow.py` — manual OAuth-code paste flow + reauth + reconfigure + options.
  - `sensor.py` — `PluxeeCardEntity` base, balance sensor, last-transaction sensor.
  - `manifest.json`, `strings.json`, `translations/{en,de,uk}.json`.
- `tests/` — pytest using pytest-homeassistant-custom-component (`test_pluxee.py`, `conftest.py`).
- `explore/` — reverse-engineering scripts (NOT shipped). Holds `tokens.json` with a LIVE
  rotating refresh token for the test account — do not share/commit; rerun scripts refresh it.
- `custom_components/pluxee/frontend/pluxee-card.js` — custom Lovelace card (vanilla
  HTMLElement + Shadow DOM, no build step). Shows balance; tap-to-expand latest
  transactions. Registers in `window.customCards` so it shows in the card picker. Config:
  `entity` (balance sensor), optional `transactions_entity` (auto-derived
  `_balance`→`_last_transaction`), `title`, `max_transactions`. Syntax-check with
  `node --check`. (Canonical copy lives inside the integration; no separate `www/`.)
- `custom_components/pluxee/frontend.py` — auto-serves the card via
  `hass.http.async_register_static_paths([StaticPathConfig("/pluxee/pluxee-card.js", path,
  cache_headers=False)])` and loads it with `frontend.add_extra_js_url(hass, url+"?v=…")`,
  once per HA instance (flag in `hass.data[DOMAIN]`). Called from `async_setup_entry`.
  Guarded: skips if `http`/`frontend` not in `hass.config.components` (keeps the bare test
  harness green) and never fails setup. manifest uses `after_dependencies`
  (NOT hard `dependencies`, since `frontend` needs the `hass_frontend` pkg absent in tests).
  So installing the integration (incl. via HACS) auto-installs the card — no manual resource.
- `README.md`, `hacs.json`, `run_wsl_tests.sh`.

## Auth flow (the critical part)
OIDC provider = `node-oidc-provider` at `https://connect.pluxee.app/op`
(discovery: `/op/.well-known/openid-configuration`). OAuth2 **authorization_code + PKCE**,
public client (`token_endpoint_auth_method=none`).
- client_id (AT): `568135b2-84c2-46a1-b471-9c34238ed924`
- redirect_uri: `https://consumers.pluxee.at/oidc/callback`
- scope: `openid profile email phone offline_access`
- authorize: `https://connect.pluxee.app/op/oidc/auth` ; token: `.../op/oidc/token`

**hCaptcha blocks headless login.** The email-submission step uses invisible
hCaptcha enforced server-side. Automated browsers (Playwright) are refused; a
tokenless form POST just re-renders. => HA cannot send the OTP itself. The
config flow therefore is **manual**: generate authorize URL (our PKCE) → user logs
in in a real browser (email+captcha+OTP) → SPA callback page hangs on a spinner
(state mismatch ⇒ the code is NOT consumed) → user copies the
`…/oidc/callback?code=…` URL → HA exchanges it. **Auth code TTL ≈ 60s**, exchange
immediately.

**Refresh tokens ROTATE on every use.** Always persist the new `refresh_token`
returned by each refresh (coordinator does this via `token_updated_cb` →
`async_update_entry`), or the chain breaks → reauth. access_token = opaque, 1800s.
Refresh needs only `grant_type=refresh_token` + `refresh_token` + `client_id`.

## Data / balance API (migrated ~2026-06-29 to the "eva/bff" backend)
Base `https://api.pluxee.app/gl/eva/bff`. Required headers:
`Authorization: Bearer <access>`, `authorization-version: 2`,
`Ocp-Apim-Subscription-Key: f2b9fb99716f43a38b01867a0aeaf687`, plus
`Origin` / `Referer: https://consumers.pluxee.at`. Country is a **lowercase path
segment** (`at`), not a header.
**DECOMMISSIONED (now 401/403):** the old base `…/gl/cwc/consumer-front-api`, key
`76ecbd16…`, `Country-Code: AT` header, and the `/v3/spl/cardsInfos` /
`/v2/spl/cards/…/transactions` / `/v2/product-referentials` endpoints. Only the
DATA API moved — the OIDC/auth layer (connect.pluxee.app/op) is unchanged.
- `GET /v2/at/cards` → `{walletId, cards:[{cardId, uniqueConsumerId, maskedPan
  ("XXXX 1234"), isPhysical, scheme, state ("ACTIVE"), name, icon, color, expiry
  ("2028-07-01T…Z"), benefits:[{benefitId, productType (SPAML/SPAFX — same codes),
  externalProductType, name, programType, amount:{value,exponent,currency}}]}]}`.
  **balance = value / 10^exponent** (e.g. 8311 exp 2 = €83.11). `walletId` = account
  id → config entry unique_id (replaced the old `ciamId`). No `product-referentials`
  endpoint anymore; friendly names come from `PRODUCT_NAMES` (SPAML=Meal Pass,
  SPAFX=Food Pass), falling back to the API's localized `name`.
- `GET /v2/at/cards/{cardId}/transactions?limit=N` → `{card, transactions:[{id,
  description, merchantName, date ("2026-06-11T13:15:47.000"), status ("APPROVED"),
  type ("DEBIT"/"CREDIT"), code ("PAYMENT"/"LOAD"), amount:{value,exponent,currency}
  (**signed** — negative for debits), merchantId/Address/City, splitData[]}]}`.
- Reverse-eng tip: the SPA is Vite + RTK Query. Fetch the live bundle
  (`https://consumers.pluxee.at/assets/index-*.js`) and grep for `backendEndpointUrl`,
  `backendAPIMKey`, `evaBffApi`, `getWallet1`, and endpoint `url:` template strings.

## Entities
Per card → a device with: `<product>_<last4>_balance` (monetary EUR, wallet breakdown attrs)
and `<product>_<last4>_last_transaction` (state = latest amount; attrs `last_merchant`,
`last_date`, `last_description`, `transactions` list). `transactions` is in
`_unrecorded_attributes` (avoid recorder bloat). Options: update interval hours (default 6),
transaction count (default 15, 0=off).

## Testing — IMPORTANT: use WSL, not Windows
The HA test harness is Linux-oriented; on Windows the asyncio self-pipe needs an
AF_INET socket that phcc blocks, and HA forces a Proactor loop. Run tests in **WSL Ubuntu**:
```
wsl -d Ubuntu -- bash -lc "cd /mnt/d/Projects/pluxee-ha && python3 -m pytest tests -q"
```
WSL python deps are installed to user site (`~/.local`, no venv — `python3-venv` missing;
bootstrapped pip via get-pip). `run_wsl_tests.sh` reproduces setup. `conftest.py` warms up
the pycares daemon thread and unloads entries before phcc's cleanup check (else lingering
timer/thread errors). `api.py` can be validated against the LIVE API on Windows via
`python explore\test_client.py` (uses `explore/tokens.json`; refreshes + rotates it).

## Auth resilience (restart recovery)
Pluxee uses **refresh-token rotation with reuse-detection** and the refresh token is
session-bound (no `offline_access`; requesting it = `invalid_scope`). A keep-alive
(`TOKEN_KEEPALIVE_INTERVAL`=25 min) rotates the token to beat the inactivity timeout.
Across an HA/Docker restart the chain can be revoked, so the stored access token 401s
even though it looks unexpired. Two safeguards (added v0.2.2):
- `api.py _async_get` does **reactive 401 recovery**: on 401/403 it forces a refresh
  (which falls back to silent re-auth via the stored `op_session` cookie) and retries
  once. So a restart self-heals instead of prompting reauth. Don't remove this.
- `__init__.py _async_update_listener` reloads **only on options change** (compares
  `coordinator.options`), NOT on the frequent token-data writes. Reloading on every
  token save recreated the client mid-refresh and could trigger reuse-revocation.
- Silent re-auth (`_async_silent_reauth`) uses a normal authorize (NOT prompt=none) with
  the `op_session` cookie; node-oidc-provider auto-confirms consent and returns a code.
  The optional session cookie is collected in the config/reauth flow and persisted; it
  rolls occasionally and the rolled value is saved back.
- **Overnight "session lost" (fixed v0.2.3):** the session-bound refresh token dies
  periodically (`invalid_grant`) — normal; silent re-auth recovers it. But it was
  intermittently failing with **HTTP 408** after a ~10s hang and that 408 was treated as
  fatal. Root cause: we stored/replayed Azure **load-balancer affinity cookies**
  (`ASLBSA`, `ASLBSACORS`) which pin to a backend instance recycled overnight → request
  to the dead instance times out (408). Two fixes in `api.py`: (1) `_is_session_cookie`
  denylists infra/transient cookies (`aslbsa*`, `*interaction`, `*resume`) so only the
  SSO/device cookies (`op_session`/`_session`/`op_device`) are kept — applied in BOTH
  `parse_cookie_header` and `update_jar_from_response`, plus `clear_domain` before every
  redirect hop so aiohttp can't auto-replay affinity from Set-Cookie; (2) silent-reauth
  treats `408/425/429/5xx` as **transient** (`PluxeeApiError` → keep-alive retries next
  cycle) instead of `PluxeeAuthError` (which would force a reauth prompt). Only a real
  200 interaction page = genuinely expired session. Added a 20s per-hop timeout.
  Note: the OP session cookie is named `op_session` in prod but node-oidc-provider's
  default is `_session` — keep the denylist naming-agnostic.
- **Multi-hop silent re-auth regression (v0.2.3 follow-up):** the first-pass ASLBSA fix
  over-corrected. It used ONE denylist for both "what to persist" and "what to carry
  in-flight", so it dropped the transient `op_interaction`/`_interaction` cookie from
  the redirect chain too. When the OP routes the authorize through an
  `/op/interaction/.../consent` step (not always — only when consent isn't short-
  circuited), it sets that cookie at one hop and the NEXT hop requires it; dropping it
  made the next hop return **HTTP 400**, which was treated as "session lost" → forced
  reauth. (Symptom in logs: silent re-auth `hop 0 -> 303`, `hop 1 -> 400`, while an
  earlier single-hop attempt succeeded.) Fix in `api.py`: split into TWO denylists —
  `_keep_in_flight` (drop ONLY `aslbsa`, so interaction cookies ride the chain like the
  `verify_cookie.py` probe's `requests.Session` does) used by `update_jar_from_response`,
  and `_is_durable_session_cookie` (drop `aslbsa`+`*interaction`+`*resume`) used by
  `parse_cookie_header` and when persisting `_session_cookie`. Also: only a **2xx**
  (rendered interaction page) is now a fatal "session expired" signal; any other non-
  redirect status (incl. a stray 400) is transient → keep-alive retries next cycle
  instead of prompting reauth. Don't re-merge the two denylists.

## Gotchas / lessons
- Don't create `PluxeeClient` with `token_expires_at=None` right after exchange — it forces
  an immediate refresh that rotates/consumes the just-issued refresh token. Pass a real
  expiry; read final tokens via `client.token_state()`.
- `DeviceInfo` import is `homeassistant.helpers.device_registry` (not `device_info`).
- Config-flow validation uses `async_get_data(tx_limit=0)` to stay fast.
- Min HA ~2024.12 (uses `runtime_data`, `_abort_if_unique_id_mismatch`,
  `_get_reconfigure_entry`). Verified on HA 2025.1.4.
- Re-running the login: `python explore\gen_url.py` prints a fresh authorize URL (saves
  `explore/pkce.json`); user pastes callback URL; `python explore\exchange.py "<url>"`.

## Environment
Windows host (PowerShell + Python 3.12 via Store stub — works from PowerShell, blocked in
bash). Node available. WSL Ubuntu for HA tests. No git repo yet.
