---
name: pluxee-ha-project
description: The Pluxee (Sodexo) Home Assistant custom integration project at D:\Projects\pluxee-ha
metadata:
  type: project
---

Project: a Home Assistant custom integration (HACS-style custom_component `pluxee`) that logs into the user's Pluxee Austria account and exposes card balances as sensors. Started 2026-06-15.

User account for testing: `somesodexo@gmail.com`, country AT, two cards (Meal Pass, Food Pass).

Key design (see [[pluxee-auth-flow]] for the why):
- Config flow uses manual OAuth-code paste because login needs a real browser (hCaptcha).
- Reauth + reconfigure reuse the same paste step (for when refresh tokens expire).
- DataUpdateCoordinator polls `/v3/spl/cardsInfos`; refreshes access token via rotating refresh_token and persists the new refresh_token to the config entry every time.
- Per card: a balance sensor (EUR, wallet breakdown in attributes) AND a "last transaction" sensor whose `transactions` attribute holds recent history (from `GET /v2/spl/cards/{cardId}/transactions`; attrs: transactionAmount, transactionCurrency, merchantName, transactionDateTime, description, transactionCode, mobilePayment). `transactions` is in `_unrecorded_attributes` to avoid recorder bloat.
- Translations: en, de, uk. Options flow: update interval (hours) + transaction count.
- Ships a custom Lovelace card `frontend/pluxee-card.js` (balance + tap-to-expand transactions) that the integration **auto-registers** (frontend.py: static path + `add_extra_js_url`), so installing the integration—including via HACS—makes the card appear in the picker with no manual resource step.
- Tests run via WSL (Linux) using pytest-homeassistant-custom-component; the Windows venv can't run the HA harness (socket/event-loop). conftest warms up pycares thread + handles cleanup.

`explore/` holds the reverse-engineering scripts (Playwright login driver, api probes). The real component lives under `custom_components/pluxee/`.
