// Offline checks for prototype-r2.html. The page is one file with inline script;
// these run its script in a Node VM against a stub DOM and fake timers.
// Run: node --test docs/ui/prototype-r2.test.js
// Point PROTOTYPE at another copy to check it: PROTOTYPE=old.html node --test ...
"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const file = process.env.PROTOTYPE || path.join(__dirname, "prototype-r2.html");
const script = fs.readFileSync(file, "utf8").match(/<script>([\s\S]*)<\/script>/)[1];
const html = fs.readFileSync(file, "utf8");

function fakeTimers() {
  let now = 0, seq = 0;
  const queue = [];
  return {
    setTimeout(fn, ms = 0) { queue.push({ id: ++seq, at: now + ms, fn, every: null }); return seq; },
    setInterval(fn, ms) { queue.push({ id: ++seq, at: now + ms, fn, every: ms }); return seq; },
    clear(id) { const i = queue.findIndex((t) => t.id === id); if (i >= 0) queue.splice(i, 1); },
    advance(ms) {
      const end = now + ms;
      for (;;) {
        queue.sort((a, b) => a.at - b.at || a.id - b.id);
        const t = queue[0];
        if (!t || t.at > end) break;
        now = t.at;
        if (t.every) t.at += t.every; else queue.shift();
        t.fn();
      }
      now = end;
    },
  };
}

function stubDom() {
  const els = new Map();
  const listeners = {};
  const el = (sel) => {
    if (!els.has(sel)) {
      els.set(sel, { id: sel, innerHTML: "", textContent: "", value: "", checked: false, disabled: false,
        hidden: false, style: {}, dataset: {}, offsetHeight: 0, focus() {}, click() {}, setAttribute() {} });
    }
    return els.get(sel);
  };
  return {
    els,
    querySelector: el,
    activeElement: null,
    createElement: () => ({ innerHTML: "", get textContent() { return this.innerHTML.replace(/<[^>]*>/g, ""); } }),
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    dispatch: (type, target) => { for (const fn of listeners[type] || []) fn({ target, preventDefault() {} }); },
  };
}

function open(roomId = "office") {
  const doc = stubDom();
  const t = fakeTimers();
  const ctx = { document: doc, console, setTimeout: t.setTimeout, clearTimeout: t.clear,
    setInterval: t.setInterval, clearInterval: t.clear, parseFloat, Math, Number, String, JSON };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(script, ctx, { filename: file });
  const run = (code) => vm.runInContext(code, ctx);
  run(`openRoom(${JSON.stringify(roomId)}); render();`);
  return {
    t, doc, run,
    room: () => run("room()"),
    drag: (v) => doc.dispatch("input", { id: "slider", value: String(v) }),
    unlock: (on) => doc.dispatch("change", { id: "unlock", checked: on }),
  };
}

test("a drag that pauses above -10 dB keeps the unlock and sends the rest of the drag", () => {
  const h = open();
  h.unlock(true);
  h.drag(-5);
  h.t.advance(100);
  h.drag(-4);
  h.t.advance(300);                         // 300 ms pause: the first write starts
  assert.equal(h.room().ui.flight, -4);
  assert.equal(h.run("S.unlock"), true, "a pause must not relock");
  h.t.advance(500);
  h.drag(-3);                               // rest of the drag, during the first write
  assert.equal(h.room().ui.err, "", "rest of the drag must not be refused");
  assert.equal(h.room().ui.want, -3);
  h.t.advance(1300);                        // first write sets -4; the queued -3 goes next
  assert.equal(h.room().vol.v, -4);
  assert.equal(h.room().ui.flight, -3);
  h.t.advance(100);
  h.drag(-2);
  h.t.advance(2000);
  assert.equal(h.run("S.unlock"), true, "still in the gesture while -2 is in flight");
  h.t.advance(1500);
  assert.equal(h.room().dev, -2);
  assert.equal(h.room().vol.v, -2);
  assert.equal(h.room().ui.msg.kind, "set");
  assert.equal(h.run("S.unlock"), false, "the unlock relocks once the volume settles");
  assert.match(h.run("S.unlockNote"), /Locked again/);
});

test("once the volume has settled, a later drag needs the unlock again (interpretation to confirm)", () => {
  const h = open();
  h.unlock(true);
  h.drag(-5);
  h.t.advance(2010);                        // flush at 0.3 s, read at 1.2 s, write at 2.0 s
  assert.equal(h.room().vol.v, -5);
  assert.equal(h.run("S.unlock"), false);
  h.t.advance(2000);
  h.drag(-4);
  assert.match(h.room().ui.err, /Allow above/);
});

test("a write checked after the 60 s cap is refused, and so is a later louder change", () => {
  const h = open();
  h.unlock(true);
  h.t.advance(59000);
  h.drag(-5);
  h.t.advance(1000);                        // t = 60 s
  assert.equal(h.run("S.unlock"), false);
  assert.match(h.run("S.unlockNote"), /ran out after 60 s/);
  h.t.advance(2000);                        // the write flushed at 59.3 s is checked at 60.2 s
  assert.equal(h.room().vol.v, -32);
  assert.equal(h.room().dev, -32);
  assert.equal(h.room().ui.msg.kind, "refused");
  h.drag(-4.5);
  assert.match(h.room().ui.err, /Allow above/);
});

test("Cancel during a write says the write may still land", () => {
  const h = open();
  h.unlock(true);
  h.drag(-5);
  h.t.advance(300);                         // flight begins at -5
  h.drag(-4);                               // queued behind it
  h.run("cancelVol(room())");
  assert.match(h.doc.els.get("#live").textContent, /may still land/);
});

test("with the step unread, the level is not changed and says what to do", () => {
  const h = open("living");
  h.run(`room().vol = { v: -30, src: "read", age: "x" }; room().step = { v: null, src: "unknown" };`);
  h.run("refreshVolume()");
  h.drag(-20);
  assert.match(h.room().ui.err, /Read the device first/);
  assert.equal(h.room().ui.want, null);
  assert.equal(h.doc.els.get("#slider").disabled, true);
});

test("a changed device refuses the change and says what to do next", () => {
  const h = open();
  h.unlock(false);
  h.drag(-40);
  h.t.advance(300);
  h.run("room().dev = -20");
  h.t.advance(1000);
  const msg = h.room().ui.msg;
  assert.equal(msg.kind, "refused");
  assert.match(msg.text, /Read Office again, then set the level again/);
});

test("power unknown: Sleep and Wake are both enabled and the preview says so", () => {
  const h = open("living");
  const block = h.run("powerBlock(room(), true)");
  assert.doesNotMatch(block, /data-to="sleep" disabled/);
  assert.doesNotMatch(block, /data-to="wake" disabled/);
  h.run("stagePower(true)");
  assert.match(h.run("S.pending.note"), /Current power state unknown/);
});

test("power known: only the action that changes state is enabled", () => {
  const h = open("office");
  const block = h.run("powerBlock(room(), true)");
  assert.match(block, /data-to="wake" disabled/);
  assert.doesNotMatch(block, /data-to="sleep" disabled/);
});

test("a preset that raises the preamp past -10 dB needs the unlock", () => {
  const h = open("office");
  h.run(`room().preamp = { v: -6.7, src: "written", age: "x" };
    room().vol = { v: -8, src: "read", age: "x" }; room().dev = -8;`);
  h.run(`stagePreset("e3-oratory-harman-oe"); renderPending();`);
  assert.match(h.doc.els.get("#pending").innerHTML, /data-act="send" disabled/);
  h.unlock(true);
  assert.doesNotMatch(h.doc.els.get("#pending").innerHTML, /data-act="send" disabled/);
  h.run("sendPending()");
  h.t.advance(800);
  assert.equal(h.run("room().preset.v"), "e3-oratory-harman-oe");
  assert.equal(h.run("S.unlock"), false, "a preset that used the unlock consumes it");
});

test("a preset that raises the preamp is sent without the unlock when the level is quiet", () => {
  const h = open("office");
  h.run(`room().preamp = { v: -6.7, src: "written", age: "x" };`);
  h.run(`stagePreset("e3-oratory-harman-oe"); renderPending();`);
  assert.doesNotMatch(h.doc.els.get("#pending").innerHTML, /data-act="send" disabled/);
});

test("a level that was never read is shown as unknown, not as -99", () => {
  const h = open("living");
  h.run(`room().vol = { v: null, src: "unknown" };`);
  assert.doesNotMatch(h.run("volumeCard(room(), true)"), /-99|value="/);
  assert.doesNotMatch(h.run("volumeCard(room(), false)"), /-99|value="/);
  h.run(`room().vol = { v: -30, src: "read", age: "x" }; room().step = { v: null, src: "unknown" };`);
  const stepper = h.run("volumeCard(room(), true)");
  assert.match(stepper, /quieter, step unknown/);
  assert.match(stepper, /louder, step unknown/);
});

test("the volume status is not a polite live region", () => {
  assert.doesNotMatch(html, /id="vol-status"[^>]*aria-live/);
});
