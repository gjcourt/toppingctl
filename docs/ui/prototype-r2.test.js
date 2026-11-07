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
  assert.equal(h.run("S.unlock"), true, "still unlocked while -2 is in flight");
  h.t.advance(1500);
  assert.equal(h.room().dev, -2);
  assert.equal(h.room().vol.v, -2);
  assert.equal(h.room().ui.msg.kind, "set");
  assert.equal(h.run("S.unlock"), true, "settling doesn't relock: the unlock runs to the cap");
  h.drag(-1.5);                             // resumed after the pause, within the cap
  assert.equal(h.room().ui.err, "");
  h.t.advance(3000);                        // flush at 6.1 s, write at 7.8 s
  assert.equal(h.room().dev, -1.5);
  assert.equal(h.run("S.unlock"), true);
  h.t.advance(60000 - 8800);                // to t = 60 s
  assert.equal(h.run("S.unlock"), false);
  assert.match(h.run("S.unlockNote"), /ran out after 60 s/);
});

test("leaving the room ends the unlock and cancels a queued change", () => {
  const h = open();
  h.unlock(true);
  h.drag(-5);
  h.t.advance(300);                         // -5 is in flight
  h.drag(-4);                               // -4 queued behind it
  h.run(`onAct("back", { dataset: {} })`);
  assert.equal(h.run("S.unlock"), false);
  assert.equal(h.run("S.view"), "list");
  assert.equal(h.run("S.rooms.find((x) => x.id === 'office').ui.want"), null);
  assert.match(h.doc.els.get("#live").textContent, /may still land/);
  h.t.advance(3000);
  assert.equal(h.run("S.rooms.find((x) => x.id === 'office').dev"), -32);
});

test("leaving the room with a change only queued sends nothing", () => {
  const h = open();
  h.unlock(true);
  h.drag(-5);                               // queued, not yet flushed
  h.run(`onAct("signout", { dataset: {} })`);
  assert.equal(h.run("S.view"), "signin");
  h.t.advance(3000);
  assert.equal(h.run("S.rooms.find((x) => x.id === 'office').dev"), -32);
  assert.equal(h.run("S.unlock"), false);
});

test("a write is refused when the unlock it was queued under is gone, even if another unlock is live", () => {
  const h = open();
  h.unlock(true);
  h.drag(-5);
  h.t.advance(300);                         // -5 is in flight, queued under this unlock
  h.unlock(false);
  h.run(`openRoom("living"); render();`);
  h.unlock(true);                           // a second unlock, ticked in another room
  assert.equal(h.run("S.unlock"), true);
  h.t.advance(1000);                        // the agent check of the office write, at 1.2 s
  assert.equal(h.run("S.rooms.find((x) => x.id === 'office').dev"), -32);
  assert.equal(h.run("S.rooms.find((x) => x.id === 'office').ui.msg.kind"), "refused");
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
  const h = open("office");
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
  const h = open("office");
  h.run(`room().power = { v: null, src: "unknown" };`);
  const block = h.run("powerBlock(room())");
  assert.doesNotMatch(block, /data-to="sleep" disabled/);
  assert.doesNotMatch(block, /data-to="wake" disabled/);
  h.run("stagePower(true)");
  assert.match(h.run("S.pending.note"), /Current power state unknown/);
});

test("power known: only the action that changes state is enabled", () => {
  const h = open("office");
  const block = h.run("powerBlock(room())");
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
  const h = open("office");
  h.run(`room().vol = { v: null, src: "unknown" };`);
  assert.doesNotMatch(h.run("volumeCard(room())"), /-99|value="/);
  h.run(`room().state = "down";`);
  assert.doesNotMatch(h.run("volumeCard(room())"), /-99|value="/);
  h.run(`room().state = "ok"; room().vol = { v: -30, src: "read", age: "x" }; room().step = { v: null, src: "unknown" };`);
  const stepper = h.run("volumeCard(room())");
  assert.match(stepper, /quieter, step unknown/);
  assert.match(stepper, /louder, step unknown/);
});

test("the volume status is not a polite live region", () => {
  assert.doesNotMatch(html, /id="vol-status"[^>]*aria-live/);
});

// --- TOPPINGCTL-12: gain, room states, "DAC volume", wake ---------------------

const app = (h) => h.doc.els.get("#app").innerHTML;
const bar = (h) => h.doc.els.get("#pending").innerHTML;
const act = (h, a, data = {}) => h.run(`onAct(${JSON.stringify(a)}, { dataset: ${JSON.stringify(data)} })`);

test("the room list shows every room state from the plan, in the plan's words", () => {
  const h = open();
  act(h, "back");
  const list = app(h);
  assert.match(list, /not read: no confirmed read path for this model/);
  assert.match(list, /DAC busy · in use by the toppingctl CLI on den/);
  assert.match(list, /Unreachable · studio · last seen 14 min ago/);
  assert.match(list, /No DAC found on snap-test/);
  assert.match(list, /Unrecognised DAC: D50s \(sample product string\)\. Read-only/);
  assert.match(list, /Different DAC than expected: Topping D90 III Discrete/);
  assert.match(list, /DAC volume/);
});

test("an identified room that isn't read shows Unknown with the reason, and no controls", () => {
  const h = open("living");
  const page = app(h);
  assert.match(page, /Unknown · not read: no confirmed read path/);
  assert.doesNotMatch(page, /data-act="(step|gain|power|read)"/);
  assert.doesNotMatch(page, /name="preset"/);
});

test("the room page labels the figure DAC volume and says it isn't the level at the ear", () => {
  const h = open();
  assert.match(app(h), /<h2>DAC volume<\/h2>/);
  assert.match(app(h), /isn't the level at your ears/);
});

test("DAC busy: a queued write is refused, nothing is sent, and the controls come back when free", () => {
  const h = open();
  h.drag(-36);
  h.t.advance(300);                         // -36 is in flight
  act(h, "cli");                            // the CLI takes the lock before the agent's check
  assert.match(app(h), /DAC busy/);
  assert.doesNotMatch(app(h), /data-act="step"/);
  h.t.advance(1000);
  assert.equal(h.room().dev, -32);
  assert.equal(h.room().ui.msg.kind, "refused");
  assert.match(h.room().ui.msg.text, /in use by the toppingctl CLI/);
  h.t.advance(8000);
  assert.equal(h.room().state, "ok");
  assert.match(app(h), /data-act="step"/);
});

test("high gain needs the unlock, low gain doesn't, and a gain send relocks", () => {
  const h = open();
  act(h, "gain", { to: "High" });
  assert.match(bar(h), /data-act="send" disabled/);
  assert.match(bar(h), /high gain counts as loud/);
  assert.match(bar(h), /id="unlock-bar"/);
  h.doc.dispatch("change", { id: "unlock-bar", checked: true });
  assert.doesNotMatch(bar(h), /data-act="send" disabled/);
  h.run("sendPending()");
  h.t.advance(800);
  assert.equal(h.room().gain.v, "High");
  assert.equal(h.room().gain.src, "read");
  assert.equal(h.run("S.unlock"), false);
  act(h, "undogain", { to: "Low" });
  assert.doesNotMatch(bar(h), /data-act="send" disabled/);
  assert.doesNotMatch(bar(h), /unlock-bar/);
});

test("a gain change is refused as changed elsewhere when the knob moved, and nothing is sent", () => {
  const h = open();
  act(h, "gain", { to: "High" });
  h.unlock(true);
  act(h, "frontpanel");                     // -32 -> -38 behind the page's back
  h.run("sendPending()");
  h.t.advance(800);
  assert.equal(h.room().devGain, "Low");
  assert.equal(h.room().vol.v, -38);
  assert.match(h.run("S.toast.html"), /Changed elsewhere/);
  assert.equal(h.run("S.pending"), null);
});

test("asleep: volume and gain are disabled, wake is previewed and reads the level", () => {
  const h = open();
  act(h, "power", { to: "sleep" });
  assert.match(bar(h), /Preview · nothing sent yet/);
  h.run("sendPending()");
  h.t.advance(800);
  assert.equal(h.room().power.v, "Asleep");
  assert.equal(h.room().power.src, "written", "an asleep read is unmeasured, so it's shown as written");
  assert.doesNotMatch(app(h), /data-act="(step|gain)"/);
  assert.match(app(h), /Wake Office to change this/);
  assert.match(app(h), /Undo \(preview: wake\)/);
  act(h, "remoteloud");                     // stored level is now -6
  act(h, "undopower", { to: "wake" });
  h.run("sendPending()");
  h.t.advance(800);
  assert.equal(h.room().power.v, "Awake");
  assert.equal(h.room().vol.v, -6);
  assert.match(h.run("S.toast.html"), /woke at −6\.0 dB/);
  assert.match(h.run("S.toast.html"), /Lower to −30 dB/);
});

test("Lower to -30 dB sends at once, with no unlock and no quiet time", () => {
  const h = open();
  h.run(`room().dev = -6; room().vol = { v: -6, src: "read", age: "x" }; refreshVolume();`);
  assert.match(h.doc.els.get("#vol-loud").innerHTML, /Lower to −30 dB/);
  act(h, "lower");
  assert.equal(h.room().ui.flight, -30, "no 300 ms wait");
  h.t.advance(1700);
  assert.equal(h.room().dev, -30);
  assert.equal(h.run("S.unlock"), false);
});

test("a failed read shows values as unknown and disables changes until a read works", () => {
  const h = open();
  h.doc.dispatch("change", { id: "readfailnext", checked: true });
  act(h, "read");
  h.t.advance(900);
  assert.equal(h.room().state, "readfail");
  assert.equal(h.room().vol.v, null);
  assert.match(app(h), /Read failed/);
  assert.doesNotMatch(app(h), /data-act="(step|gain|power)"/);
  act(h, "read");
  h.t.advance(900);
  assert.equal(h.room().state, "ok");
  assert.equal(h.room().vol.v, -32);
});

test("r2 has no scene recall", () => {
  assert.doesNotMatch(script, /recall|scene|\bC[12]\b|71 1[12]/i);
});
