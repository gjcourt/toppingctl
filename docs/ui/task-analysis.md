# Multi-room web UI: task analysis

Ticket: [TOPPINGCTL-1](https://plane.burntbytes.com/home/browse/TOPPINGCTL-1/). Prototype:
[`prototype-r1.html`](prototype-r1.html). This document follows the process in `UI.md` in
`gjcourt/agents`: the task analysis and a low-fidelity prototype come before production UI.

## What George asked for

Decisions dated 2026-10-25, quoted from the ticket:

- A web frontend for toppingctl, reachable over the network.
- It controls **several rooms' DACs**: one UI, more than one DAC, each on a different host.
- It is reachable over the **LAN and WireGuard**, and it needs **sign-in**.

Everything else in this document is analysis. Anything not confirmed by George or by the code
or spec is marked **(assumption)** or **(estimate)**, and the list of open questions is at the
end.

## What the hardware actually allows today

The UI can only offer what the tool can do safely. This table comes from `DEVICES` in
`toppingctl.py`, the per-command guards, and the protocol spec in `gjcourt/lab`
(`01-audio-midi/_reference/topping-dx5ii-hid-protocol.md`, §4a and §5). The spec wins where the
two disagree.

| Control | DX5 II (status `confirmed`) | DX1 II, D90 III Discrete (status `unverified`) |
| --- | --- | --- |
| Read live state (`readsettings.py`) | Yes | DX1 II: only a read-safe register set. Elsewhere a read *is* a write, so some state (input among it) is known only from the device's own pushes. D90 III: the DX5 II settings read (`71 0c`) returns nothing. The code records a separate `0x4a` field enumeration |
| Volume (`71 02`) | **Confirmed** in the spec. Scale depends on `volumeStep`, which the tool reads first | DX1 II: a different register block, front-panel-confirmed, but the model is unverified. D90 III: not on the HID interface at all (host-side volume is the UAC2/ALSA mixer, USB input only). Read-only in the UI |
| Headphone gain (`71 17`) | **Confirmed** | Read-only |
| Power, sleep and wake (`71 01`) | **Confirmed** | Read-only |
| PEQ bands and preamp (`0x91`–`0x9c`) | **Confirmed**, 10 usable bands. **Preset names aren't stored on the device** (spec §2). A bulk dump (`12 06`) returns band frequency and Q but not gain, and the spec calls that capture racy. So the UI can only show which preset toppingctl last wrote. `toppingctl flat` writes the bands only, and leaves the preamp (`0x9c`) as it was | DX1 II: the active stored slot (1–3) is readable. Read-only in the UI |
| Scene recall, C1/C2 (`71 11`, `71 12`) | Vendor-sourced, implemented in `scenes.py`. Not listed as confirmed in the spec's §4a | Read-only |
| Input select (`71 04`) | Spec §4a: confidence **Medium**. `toppingctl input` refuses on the DX5 II ("0x7104 unverified on hardware"). `setctl.py inputType` can write it with a read-back check, but that verifies storage, not effect | DX1 II: `toppingctl input` writes it behind `--unverified`. Read-only in the UI |
| Output select (`71 05`) | Spec §4a rates the observed 1–6 ordering **High**. §5 replaces it with the vendor's seven-value enum, which has no hardware rating. No toppingctl command writes it. `setctl.py outputType` can, with read-back | Read-only |
| Mute (`71 03`) | **Not implemented**: `toppingctl mute` refuses ("vendor-sourced but hardware-unverified"). `setctl.py muted` maps to it with confidence "unchecked" | DX1 II: `toppingctl mute` writes it behind `--unverified`. Read-only in the UI |
| Scene save (`71 35`, `71 36`) | **Never.** It overwrites the C1/C2 presets stored on the device | Never |
| Factory reset, unmapped registers | Never | Never |

So on the one confirmed model, three of the tasks in the ticket (**input, output and mute**)
have no hardware-confirmed write. `setctl.py` can send them with a read-back check. A read-back
proves the value was stored, not that the DAC did anything (band 11 stored values too), so the
UI doesn't treat it as confirmation. The prototype shows their state when it was read, and
shows each control as disabled with the reason next to it. See question 1.

## Who uses it

- **George**: the primary user. He knows the protocol and the risks, but in daily use he wants
  to change the volume, not think about registers.
- **Other people in the house** (assumption): someone in another room might use the same UI.
  They may not know that a D90 III's volume isn't on its control interface, or what "unverified" means. The UI
  is designed so that a first-time user cannot hurt anyone. See question 6.

## Where and how

| Context | Device | Network | What's different |
| --- | --- | --- | --- |
| In the room, headphones on or about to be | Phone, one hand | LAN | The most common context (estimate). Small screen and a quick glance. **The person at risk is the person holding the phone.** |
| At a desk | Laptop | LAN | Keyboard use. Might be managing a room they can't hear |
| Away from home | Phone or laptop | WireGuard | Can't hear or see any room. Most likely to check status or put a room to sleep (assumption). Slower and lossier link, so taps might be retried |

Two consequences shape the design:

1. **You can act on a room you can't hear.** The room name is in the page header, in every
   preview ("Set Office volume −32.0 dB → −30.0 dB"), and on the Send button.
2. **A slow link can repeat a request.** The UI only ever sends absolute targets ("set
   −30.0 dB"). It never sends relative ones ("+1 dB"), so sending a request twice gives the same
   result as sending it once.

## Tasks, by frequency

Frequencies are **estimates** for George to correct. Order follows importance × frequency, and
that order drives the layout.

| # | Task | Frequency (estimate) | Where it lives in the prototype |
| --- | --- | --- | --- |
| T1 | **See every room**: which DAC, whether its host is reachable, its volume, and whether that value is fresh | Every time the UI opens | The default view, the room list |
| T2 | **Change volume safely** in one room | Several times per listening session | Room view, top. Big step buttons and a slider capped at −10 dB |
| T3 | **Recall a PEQ preset** after swapping headphones | Weekly | Room view, "EQ preset" |
| T4 | **Recall scene C1 or C2** | Occasionally | Room view, "Scenes" |
| T5 | See or change **input** (USB, optical, coax, Bluetooth) | Occasionally | Room view, shown read-only for now |
| T6 | See or change **output** (headphones or line, single-ended or balanced) | Occasionally | Room view, shown read-only for now |
| T7 | **Mute** | Unknown. The remote may cover it already | Room view, shown read-only for now |
| T8 | Sleep or wake a room (not in the ticket, but power is a confirmed register) | Occasionally, maybe remotely | **Not in r1.** See question 7 |
| T9 | Sign in | Once per device per session length | Sign-in screen |

The most common question is "what is each room doing, and can I reach it?" The room list
answers it without a tap.

## The costly mistakes

Ranked by cost. Each row says how the UI makes the mistake hard to make. A warning alone
doesn't count.

| Mistake | Cost | How it could happen | What the design does |
| --- | --- | --- | --- |
| **Loud volume into headphones** | Irreversible: hearing, drivers | Mistyped value. A slider dragged too far. A "+" step from a stale cached value. The wrong `volumeStep` scale | The slider and steppers **stop at −10 dB**. If a room is already louder than that, they can only go quieter. Above −10 dB, a separate **"Allow above −10 dB"** control in the room view unlocks the range for **one change**, then re-locks. A "+" step needs a volume **read from the device this session**. From a cached value, only a typed absolute target is allowed, and there is no undo back to the cached value. The step size shown comes from the device read |
| **Loud volume from a scene recall** | Same as above | C1 and C2 can hold any volume, and their contents can't be read before recall (`scenes.py`) | A recall needs its own "Allow above −10 dB for this change" tick in the preview. As `scenes.py` does, the state is read just before and just after the recall. If the volume ended above −10 dB, a large **"Lower to −30 dB"** button comes first, then the before/after diff |
| **Louder after a PEQ preset change** | Loud. The preamp sets the overall level, and the filters then cut or boost single frequencies on top | Each preset carries its own preamp (from −1.1 to −6.7 dB in `presets/`). Going from E3 (−1.1 dB) to Sundara (−6.7 dB) and back raises the overall level 5.6 dB. "Flat" leaves the preamp alone, so frequencies the old preset cut get louder | The preview states a preamp rise and its size in dB. For Flat, it says the preamp stays where it is. When the current preamp is unknown, the preview says the level change is unknown. Whether a rise also needs the unlock is question 12 |
| **Gain switched to high** | Louder by an amount **not measured** here | Toggling gain on a headphone output | **Not in r1.** Gain is a loudness control and needs the same treatment as volume. See question 8 |
| **Output moved from line to headphones at a line-level volume** | Loud | Output select | Output is read-only for now. If it is enabled later, switching to a headphone output must check the volume first |
| **Acting on the wrong room** | Possibly loud, in someone else's ears | Remote control of a room you can't hear | The room name is in the header, in every preview, on Send, and in the accessible names of the step buttons and the slider. There is no "apply to all rooms" |
| **Writing an unconfirmed register** | Unknown device state, maybe a lost user setting | An unverified model, or a control the spec hasn't confirmed | Unverified models are **view-only**. Their controls are shown disabled with the reason, not hidden (so users don't go looking for them) and not merely warned about. The one exception is a control the model has no register for at all, such as D90 III volume. That gets one line saying so. There is no override in the UI. `--unverified` stays a CLI-only flag |
| **Overwriting stored scenes** | Loses the C1/C2 presets on the device | Scene save | **Absent.** One line says why, so nobody goes looking for it |
| **Acting on stale state** | Wrong decisions, and "+" from the wrong base | Changes from the front panel, remote or vendor app are invisible to the cache | Every value carries its **source and age**: "read from device 12 s ago" or "last written by toppingctl 2 h ago, may be stale". A value nobody has ever read shows **"Unknown"**, never 0 or blank |
| **An unreachable host mistaken for a quiet one** | Confusion, and repeated retries | Network, host down | The room card says **"Unreachable"** with the host name and when it was last seen. Its values are greyed and labelled as last known. Its controls are disabled |
| **Dry-run skipped** | A wrong frame sent | Habit | **Every write is a preview first**: the room, the change, the frames in hex (one tap away) and then **Send**. The only exceptions are the quieter-only actions in design rule 5. This is the dry-run built into the flow, so there's no flag to forget. (The prototype lists frames for volume and scenes. For presets it only counts them: 4 preamp frames, 110 band frames and a commit) |

## Design rules that follow

1. The default view is the room list. Each card shows the room, model and host, whether the
   host is reachable, the volume with its source and age, and the verification status.
2. Every value is labelled as **read from device** (solid dot), **last written by toppingctl**
   (hollow dot), or **unknown** (dash). The label is always text, never colour alone.
3. Writes go **Preview → Send → feedback → Undo** where undo is possible. Volume and preset can
   be undone by writing the previous value. A scene recall can be undone only for the confirmed
   registers it changed (volume), so the diff says which fields can be restored.
4. A disabled control always says why, in one line, in plain words.
5. **One exception to preview-first:** after a scene recall leaves a room above −10 dB,
   "Lower to −30 dB" sends at once. It can only make the room quieter, and a preview would only
   slow that down. An undo of a volume change that lands at or below −10 dB also sends at once.
   Undo never goes back up to a loud level in one tap: that has to be set again, with the
   unlock. An undo of a preset is previewed like any other change.
6. Only absolute targets are sent.
7. Units on every figure (dB, with a sign), and tabular figures.
8. Phone width first (360 px), dark mode, focusable by keyboard, real labels on controls.

## Architecture notes (not decided here)

The UI needs something on each audio host that holds the USB device. The README says
toppingctl is installed on every `dietpi-audio` node. How the UI reaches them is a decision for
George (question 4). The UI design above doesn't depend on the answer, except that
reachability must be reported per host.

## Open design questions for George

1. **Input, output and mute are not writable on the DX5 II yet.** The spec rates input select
   Medium and output select High, and mute was never driven.
   - (a) Ship r1 with them read-only and open a hardware-confirmation ticket for each. *(the
     prototype's default)*
   - (b) Confirm them on hardware first (the AGENTS.md sequence), then build them into the UI.
   - (c) Leave them out of the UI until they are confirmed.
2. **Scene recall can set a loud volume, and its contents can't be read beforehand.** How
   guarded should it be?
   - (a) It needs the "Allow above −10 dB" unlock, and a loud result offers "Lower to −30 dB".
     *(the prototype's default)*
   - (b) After the recall, the server automatically lowers the volume to −10 dB if it ended
     louder, then reports the change.
   - (c) Leave scene recall out of the web UI. It stays on the remote and the CLI.
3. **How does unlocking above −10 dB work?**
   - (a) A checkbox in the room view that covers one change and re-locks after Send or Cancel.
     *(the prototype's default. It could also expire after a timeout, which the prototype
     doesn't do.)*
   - (b) Type the target in dB to confirm, like typing a repo name to delete it.
   - (c) Not available in the web UI at all. Loud levels only via the CLI's `--force`.
4. **How does the UI reach each room's DAC?**
   - (a) A small agent on each audio host, serving an authenticated API over the LAN and
     WireGuard. The UI talks to all of them.
   - (b) One central server that runs toppingctl on each host over SSH.
   - (c) Each host serves its own UI, plus a page that links them together (this isn't one UI,
     so it probably fails the brief).
5. **How does sign-in work?**
   - (a) Passkey (WebAuthn).
   - (b) The homelab's existing SSO, if there is one. I haven't checked `gjcourt/homelab`.
   - (c) A shared password per household.
   - (d) Trust the network (LAN or WireGuard) and skip sign-in. This contradicts the 2026-10-25
     decision, and is listed only for completeness.
6. **Who besides George uses it?**
   - (a) Only George.
   - (b) The household, with the same powers.
   - (c) The household, but only George can unlock above −10 dB or recall scenes.
7. **Should sleep and wake (power) be in the UI?** It's a confirmed register, and it may be the
   most useful thing to do remotely.
   - (a) Yes, in r2, with preview and undo.
   - (b) No.
8. **Headphone gain is a loudness control.** How should it work?
   - (a) Treat switching to high gain like unlocking above −10 dB.
   - (b) Show gain read-only.
   - (c) Leave it out.
9. **Is the active PEQ preset worth showing when it can't be read back?** The band registers
   echo but don't report, so the UI can only say "last written by toppingctl".
   - (a) Show it, labelled as last written. *(the prototype's default)*
   - (b) Hide it.
   - (c) Show it, plus a "re-apply to be sure" action.
10. **Are the frequencies in "Tasks, by frequency" right?**
    - (a) Yes. Mostly in-room, on a phone, with volume as the main task.
    - (b) Mostly remote (laptop or WireGuard), so status and sleep matter more than volume.
    - (c) Something else. Tell me, and r2 reorders the room view.
11. **Preview-first costs a tap on the most frequent task.** In the prototype, each volume
    change is staged and then sent, and repeated taps on +/− add up into one pending change.
    - (a) Keep preview-first for every change. *(the prototype's default)*
    - (b) Within the safe range (at or below −10 dB) and from a value read this session, send
      steps at once. Keep the preview for typed targets, presets, scenes and anything loud.
    - (c) Send *quieter* steps at once, and preview louder ones.
12. **Should a preset that raises the preamp need the unlock?** Going from Sundara (−6.7 dB
    preamp) to E3 (−1.1 dB) raises the overall level by 5.6 dB.
    - (a) Warn in the preview, with the dB difference, but don't require the unlock. *(the
      prototype's default)*
    - (b) Require "Allow above −10 dB" when the volume plus the preamp rise would go above −10 dB.
    - (c) Always step the volume down by the preamp difference before applying, then tell the
      user.
