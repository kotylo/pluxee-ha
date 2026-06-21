/*
 * Pluxee Card — a Lovelace custom card for the Pluxee integration.
 *
 * Shows a card's balance; tap to expand the latest transactions.
 *
 * The DOM is built once and only updated in place: `set hass` fires very often,
 * so rebuilding innerHTML each time caused header flicker and made the toggle
 * miss clicks (the node was replaced mid-click). Expansion is a CSS class.
 *
 * Example config:
 *   type: custom:pluxee-card
 *   entity: sensor.meal_pass_1234_balance
 *   # transactions_entity: sensor.meal_pass_1234_last_transaction   (auto-derived)
 *   # title: Meal Pass
 *   # max_transactions: 10
 */

const fmtAmount = (n, currency) => {
  if (n === null || n === undefined || isNaN(Number(n))) return "—";
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: currency || "EUR",
    }).format(Number(n));
  } catch (e) {
    return `${Number(n).toFixed(2)} ${currency || ""}`.trim();
  }
};

const fmtDate = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
};

const TEMPLATE = `
  <style>
    ha-card { overflow: hidden; }
    .head {
      display: flex; align-items: center; justify-content: space-between;
      padding: 16px; cursor: pointer; gap: 12px;
      -webkit-tap-highlight-color: transparent; user-select: none;
    }
    .head:hover { background: var(--secondary-background-color); }
    .titlewrap { display: flex; flex-direction: column; min-width: 0; }
    .title {
      font-size: 1.05rem; font-weight: 600; color: var(--primary-text-color);
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .sub { font-size: 0.8rem; color: var(--secondary-text-color); }
    .balance {
      font-size: 1.5rem; font-weight: 700; color: var(--primary-text-color);
      white-space: nowrap; display: flex; align-items: center; gap: 6px;
    }
    .chev { transition: transform 0.2s ease; color: var(--secondary-text-color); flex: none; }
    ha-card.expanded .chev { transform: rotate(180deg); }
    .tx { display: none; border-top: 1px solid var(--divider-color); }
    ha-card.expanded .tx { display: block; }
    .row {
      display: flex; align-items: center; justify-content: space-between;
      padding: 10px 16px; border-bottom: 1px solid var(--divider-color); gap: 12px;
    }
    .row:last-child { border-bottom: none; }
    .row.empty { color: var(--secondary-text-color); justify-content: flex-start; }
    .left { min-width: 0; }
    .merchant {
      color: var(--primary-text-color); font-size: 0.95rem;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .date { color: var(--secondary-text-color); font-size: 0.78rem; }
    .amt { font-weight: 600; white-space: nowrap; }
    .amt.neg { color: var(--error-color, #e53935); }
    .amt.pos { color: var(--success-color, #43a047); }
  </style>
  <ha-card>
    <div class="head">
      <div class="titlewrap">
        <span class="title"></span>
        <span class="sub"></span>
      </div>
      <div class="balance">
        <span class="bal"></span>
        <svg class="chev" width="20" height="20" viewBox="0 0 24 24">
          <path fill="currentColor" d="M7.41 8.59 12 13.17l4.59-4.58L18 10l-6 6-6-6z"/>
        </svg>
      </div>
    </div>
    <div class="tx"></div>
  </ha-card>
`;

class PluxeeCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._expanded = false;
    this._txSig = null;
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("You need to define an 'entity' (a Pluxee balance sensor).");
    }
    this._config = {
      max_transactions: 10,
      ...config,
      transactions_entity:
        config.transactions_entity ||
        config.entity.replace(/_balance$/, "_last_transaction"),
    };
    // Force a fresh structure on (re)configuration.
    this.shadowRoot.innerHTML = "";
    this._root = null;
    this._txSig = null;
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  getCardSize() {
    return this._expanded ? 5 : 2;
  }

  static getStubConfig(hass) {
    const balance = Object.keys(hass.states).find(
      (e) => e.startsWith("sensor.") && e.endsWith("_balance")
    );
    return { entity: balance || "sensor.pluxee_balance" };
  }

  _ensureStructure() {
    if (this._root) return;
    const tpl = document.createElement("template");
    tpl.innerHTML = TEMPLATE;
    this.shadowRoot.appendChild(tpl.content.cloneNode(true));
    this._root = this.shadowRoot.querySelector("ha-card");
    this._el = {
      head: this.shadowRoot.querySelector(".head"),
      title: this.shadowRoot.querySelector(".title"),
      sub: this.shadowRoot.querySelector(".sub"),
      bal: this.shadowRoot.querySelector(".bal"),
      tx: this.shadowRoot.querySelector(".tx"),
    };
    this._el.head.addEventListener("click", () => this._toggle());
    this._applyExpanded();
  }

  _toggle() {
    this._expanded = !this._expanded;
    this._applyExpanded();
  }

  _applyExpanded() {
    if (!this._root) return;
    this._root.classList.toggle("expanded", this._expanded);
    if (this._expanded) this._renderTx(true);
  }

  _setText(el, text) {
    if (el.textContent !== text) el.textContent = text;
  }

  _update() {
    if (!this._hass || !this._config) return;
    this._ensureStructure();
    const bal = this._hass.states[this._config.entity];
    if (!bal) {
      this._setText(this._el.title, `Entity not found: ${this._config.entity}`);
      this._setText(this._el.sub, "");
      this._setText(this._el.bal, "");
      return;
    }
    const currency = bal.attributes.unit_of_measurement || "EUR";
    const title =
      this._config.title ||
      (bal.attributes.friendly_name || this._config.entity).replace(/\s*Balance$/i, "");
    const last4 = bal.attributes.last4 ? `••${bal.attributes.last4}` : "";

    this._setText(this._el.title, title);
    this._setText(this._el.sub, last4);
    this._el.sub.style.display = last4 ? "" : "none";
    this._setText(this._el.bal, fmtAmount(bal.state, currency));

    if (this._expanded) this._renderTx(false);
  }

  _renderTx(force) {
    if (!this._el) return;
    const tx = this._hass.states[this._config.transactions_entity];
    const balEntity = this._hass.states[this._config.entity];
    const currency =
      (balEntity && balEntity.attributes.unit_of_measurement) || "EUR";
    const transactions = (tx && tx.attributes.transactions) || [];
    const shown = transactions.slice(0, this._config.max_transactions);

    const sig = JSON.stringify(shown);
    if (!force && sig === this._txSig) return; // nothing changed -> don't touch DOM
    this._txSig = sig;

    if (!shown.length) {
      this._el.tx.innerHTML = `<div class="row empty">No transactions available</div>`;
      return;
    }
    this._el.tx.innerHTML = shown
      .map((t) => {
        const cls = Number(t.amount) < 0 ? "neg" : "pos";
        return `
          <div class="row">
            <div class="left">
              <div class="merchant">${t.merchant || t.description || "—"}</div>
              <div class="date">${fmtDate(t.date)}</div>
            </div>
            <div class="amt ${cls}">${fmtAmount(t.amount, t.currency || currency)}</div>
          </div>`;
      })
      .join("");
  }
}

if (!customElements.get("pluxee-card")) {
  customElements.define("pluxee-card", PluxeeCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "pluxee-card")) {
  window.customCards.push({
    type: "pluxee-card",
    name: "Pluxee Card",
    description: "Shows a Pluxee card balance; tap to view latest transactions.",
    preview: false,
    documentation: "https://github.com/kotylo/pluxee-ha",
  });
}

console.info("%c PLUXEE-CARD %c v0.2.3 ", "background:#1a1a1a;color:#fff", "");
