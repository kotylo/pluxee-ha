import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { setImmediate } from "node:timers/promises";
import test from "node:test";
import { createContext, SourceTextModule } from "node:vm";

const source = readFileSync(
  new URL("../custom_components/pluxee/frontend/pluxee-card.js", import.meta.url),
  "utf8"
);

// HA replaces the native registry and HTMLElement during app initialization.
// Its scoped registry defines stand-ins in the native registry, resolving any
// native whenDefined() promises that were created before the replacement.
function registry(native) {
  const definitions = new Map();
  const pending = new Map();
  return {
    get: (name) => definitions.get(name),
    define(name, constructor) {
      assert.equal(definitions.has(name), false, `duplicate definition: ${name}`);
      definitions.set(name, constructor);
      if (native && !native.get(name)) native.define(name, class {});
      pending.get(name)?.(constructor);
    },
    whenDefined(name) {
      if (definitions.has(name)) return Promise.resolve(definitions.get(name));
      return new Promise((resolve) => pending.set(name, resolve));
    },
  };
}

function environment() {
  class NativeHTMLElement {
    attachShadow() {
      this.shadowRoot = { innerHTML: "" };
    }
  }
  class PatchedHTMLElement extends NativeHTMLElement {}
  const native = registry();
  const context = createContext({
    customElements: native,
    HTMLElement: NativeHTMLElement,
    console: { info() {} },
  });
  context.window = context;
  return {
    context,
    native,
    PatchedHTMLElement,
    boot() {
      context.HTMLElement = PatchedHTMLElement;
      context.customElements = registry(native);
      context.customElements.define("home-assistant", class extends PatchedHTMLElement {});
    },
    async load() {
      const module = new SourceTextModule(source, { context });
      await module.link(() => assert.fail("The card must be self-contained"));
      return module;
    },
  };
}

function assertCardReady(env) {
  const Card = env.context.customElements.get("pluxee-card");
  assert.equal(typeof Card, "function");
  assert.equal(Object.getPrototypeOf(Card.prototype), env.PatchedHTMLElement.prototype);
  const card = new Card();
  card.setConfig({ entity: "sensor.meal_pass_1234_balance" });
  assert.equal(card.getCardSize(), 2);
  assert.equal(env.context.window.customCards.length, 1);
  assert.equal(env.context.window.customCards[0].type, "pluxee-card");
}

test("a fast extra module waits for HA's registry replacement", async () => {
  const env = environment();
  const module = await env.load();
  const evaluation = module.evaluate();
  await setImmediate();

  env.boot();
  await evaluation;
  assertCardReady(env);
});

test("a card loaded after app initialization registers normally", async () => {
  const env = environment();
  env.boot();
  const module = await env.load();
  await module.evaluate();
  assertCardReady(env);
});

test("multiple resource URLs keep one card and picker entry", async () => {
  const env = environment();
  env.boot();
  const first = await env.load();
  await first.evaluate();
  const Card = env.context.customElements.get("pluxee-card");
  const second = await env.load();
  await second.evaluate();
  assert.equal(env.context.customElements.get("pluxee-card"), Card);
  assertCardReady(env);
});
