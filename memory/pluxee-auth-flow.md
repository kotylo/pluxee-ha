---
name: pluxee-auth-flow
description: How the Pluxee Austria consumer portal auth + balance API works (for the HA integration in pluxee-ha project)
metadata:
  type: project
---

Pluxee Austria (consumers.pluxee.at) is a Next.js SPA backed by a standard `node-oidc-provider` IdP at `https://connect.pluxee.app/op`.

Auth (OAuth2 authorization_code + PKCE, public client, token_endpoint_auth_method=none):
- client_id (AT): `568135b2-84c2-46a1-b471-9c34238ed924`
- redirect_uri: `https://consumers.pluxee.at/oidc/callback`
- scope: `openid profile email phone offline_access`
- authorize: `https://connect.pluxee.app/op/oidc/auth`, token: `.../op/oidc/token`
- Login is email + email-OTP. The **email-submission step is protected by invisible hCaptcha enforced server-side** → a headless/automated login is NOT possible (Playwright is blocked by hCaptcha; tokenless POST just re-renders the form). The user MUST log in in a real browser.
- Integration design consequence: config flow = **manual auth-code paste**. Generate authorize URL with our PKCE, user logs in via their own browser, the SPA callback page hangs on a spinner (state mismatch, so the code is NOT consumed), user copies the `…/oidc/callback?code=…` URL and pastes it; HA exchanges code for tokens. Auth code TTL is short (~60s) so exchange immediately.
- **Refresh tokens ROTATE on every use** — always persist the new refresh_token returned by each refresh, or the chain breaks. access_token lasts 1800s (30 min), is opaque (not a JWT). Refresh needs only grant_type+refresh_token+client_id.

Balance API: base `https://api.pluxee.app/gl/cwc/consumer-front-api`, headers `Authorization: Bearer <access>`, `Ocp-Apim-Subscription-Key: ****`, `Country-Code: AT`.
- One call does it all: `GET /v3/spl/cardsInfos` → consumerCardList[].cardInfoList[] each with uniqueCardId, productCode, maskedPan, panLastFourDigits, cardStatus, and accountBalanceList[].walletBalanceList[] where balance = `amount / 10**exponent` (e.g. amount 8311 exp 2 = €83.11).
- `GET /v3/spl/cards` = lighter card list. `GET /v2/product-referentials` maps productCode→name (SPAML=Meal Pass, SPAFX/SPAFD=Food Pass, SPAGF=Gift Pass).
- account unique id = `ciamId` from cardsInfos (use as config entry unique_id).

See [[pluxee-ha-project]].
