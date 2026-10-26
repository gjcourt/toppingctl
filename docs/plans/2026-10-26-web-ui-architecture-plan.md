# Networked, multi-room toppingctl: architecture plan

Ticket: [TOPPINGCTL-2](https://plane.burntbytes.com/home/browse/TOPPINGCTL-2/). Status:
**proposed**. This PR only adds the plan, with no code.

Inputs:

- George's decisions of 2026-10-25 (TOPPINGCTL-1) and 2026-10-26 (TOPPINGCTL-2), answering the
  open questions in [`docs/ui/task-analysis.md`](../ui/task-analysis.md) (PR #27).
- The r1 prototype, [`docs/ui/prototype-r1.html`](../ui/prototype-r1.html).
- The protocol spec in `gjcourt/lab`,
  `01-audio-midi/_reference/topping-dx5ii-hid-protocol.md`. AGENTS.md makes it the source of
  truth.
- The lab's earlier design for this problem, `01-audio-midi/01-022-multi-dac-control-plane.md`.
  This plan follows its shape: an agent at the metal, coordination in the cluster, and
  unverified models read-only, enforced away from the UI.
- `gjcourt/homelab`: `hosts/dietpi-audio/`, the Authelia app, the gateway and
  `apps/production/external-services/`.
- The `gjcourt/plane-mobile` architecture plan (`docs/plans/2026-10-25-architecture-plan.md`),
  for how sign-in is meant to work.

Anything not established by those sources is marked **(proposal)**, **(unknown)** or
**(unmeasured)**. Numbered questions for George are at the end.

## What the plan has to work with

These facts were checked in the sources above. Each one constrains the design.

| Fact | Source | Consequence |
| --- | --- | --- |
| toppingctl is already on every DietPi audio node. It lives in a venv at `/opt/toppingctl-venv` (PyPI `hid`) with sources in `/opt/toppingctl/*.py`, and is installed by the first-boot `Automation_Custom_Script.sh`, section 8. There is no service, unit or listening port | homelab `hosts/dietpi-audio/` | The agent can be one more top-level `*.py` that the existing installer already copies. It needs a systemd unit and one more pip package, not a new install path |
| The installer runs once, as root. There is no Ansible and no agent on the nodes | same | Deploying the agent is a homelab PR to that script, plus a re-run on each existing node |
| The hostname is the room name (`kitchen`, `living-room`, `office`, and a reference node `snap-test`) | same, README | One agent per host, one DAC per agent. The host name is the room's identity, as 01-022 §3 concludes |
| Rooms and DACs I could confirm: `living-room` has a **D90 III Discrete** (unverified; volume isn't on HID). `kitchen` has a **D50s** per `snapcast.md`, which is not in `DEVICES`. The docs disagree about kitchen: `physical-topology.md` still says HifiBerry. **Which host the DX5 II is attached to: not found** | homelab docs | Today, possibly no room has a writable DAC through the agent. See question 1 |
| The DAC's own volume is a stage **in series** with the UAC2/ALSA volume that snapclient and Home Assistant drive, and the two add in dB | homelab `snapcast.md` | The −10 dB rule only governs the DAC's own stage. The plan says so in the UI rather than implying the level at the ear is known. See question 12 |
| Home Assistant has IR-blaster buttons for the DX5 II's volume, mute, power and C1/C2 | homelab `homeassistant/files/dashboards/control.yaml` | That is another writer the agent can't see, alongside the front panel, the remote and the vendor app |
| Authelia runs at `auth.burntbytes.com`. OIDC clients are inline in `apps/production/authelia/configuration.yaml`, each with a hashed secret and `authorization_policy: two_factor`. The plaintext secret goes in the app's SOPS secret | homelab Authelia, `docs/operations/apps/authelia.md` | The UI is its own OIDC client. George adds the secret, since SOPS is operator-only |
| The gateway's Authelia `ext_authz` filter **does not enforce** on Cilium Gateway API listeners. Incident status: "Ongoing" | homelab `cilium-envoy-config-production.yaml`, the 2026-02-27 incident doc | Forward-auth can't be relied on. Sign-in has to live in the app, which is also what plane-mobile plans |
| plane-mobile's sign-in is **planned, not built**: server-side authorization-code flow with `authlib`, then its own `HttpOnly; Secure; SameSite=Lax` session cookie, then an `allowedUser` check, on Starlette | plane-mobile plan, lines ~195–198 and ~742–753 | "As plane-mobile does" means copying that design. There is no working code to reuse yet |
| An app is LAN-only by default: an HTTPRoute on `app-gateway-production` and nothing in the Cloudflare tunnel. WireGuard reaches the LAN, so it reaches the app | homelab gateway, cloudflare-tunnel README | Nothing needs to be exposed to the internet |
| Pods reach LAN hosts through a CiliumNetworkPolicy `toCIDR` egress rule. "LAN hosts are classified as world by Cilium" | homelab `homeassistant/networkpolicy.yaml` | The cluster frontend gets an egress rule to the audio hosts' agent port and nothing else on that VLAN |
| The device "never reports volume or PEQ state back" on its own (§6), and nobody has tested whether front-panel changes produce a push. GetSettings (`71 0c`, readNack) returns the whole configuration and is read-safe on the DX5 II | spec §5, §6 | Stale state is detected by **polling and reading before every write**, not by listening |
| Only one client gets the device: "the vendor web app claims it over WebHID, and only one client gets it", so `read_settings` retries the open | `devstate.py:128–130` | The agent mustn't hold the handle permanently, or the vendor app and the CLI lose the DAC. It opens once **per operation**, so a read, write and read-back share one handle. Lab 01-022 §5 still calls the agent-plus-vendor-app case untested, and sleep and hot-plug "undesigned" |
| `read_settings()` listens for a fixed 2 s window | `devstate.py:126` | A fresh read costs about 2 s today **(unmeasured whether it can stop once every record has arrived)**. That matters for "+/− sends at once", so build step 1 makes the read stop early |
| Scene recall (`71 11`/`71 12`, CallC1/CallC2) is **vendor-sourced and not in the spec's confirmed table**. A recall can change input, output and PEQ at once | spec §5; `scenes.py` docstring | AGENTS.md allows writes only to confirmed registers. Decision 2 says how recall is guarded, but not that it may skip confirmation. See question 15 |
| The preamp (`0x9c`) **can't be read back**. GetSettings has no preamp field, so "current preamp" only ever means "last written by toppingctl" | `vendor_commands.py` `SETTINGS_FIELDS`; `toppingctl.py` `cmd_apply` | Decision 12's preamp-rise rule always rests on a value that may be stale. See question 7 |
| `cmd_*` functions end with `sys.exit()` and `print()` | `toppingctl.py` | The guards need extracting into functions that return frames or raise. The agent can't call the CLI functions as they are |

**One spec discrepancy to flag.** Spec §5 says the volume scale under `volumeStep = one_db` is
unknown. `toppingctl.py` (`cmd_vol`) records a front-panel measurement from 2026-08-27 that the
raw unit follows `volumeStep`. The code's note is newer and measured, so this plan keeps the
code's behaviour: read `volumeStep` and refuse if it can't be read. The spec should get the
measurement too, in a separate `gjcourt/lab` change.

## Topology

```
 phone / laptop ──HTTPS──► app-gateway-production ──► toppingweb (cluster, namespace toppingctl)
 (LAN or WireGuard)          *.burntbytes.com            │  OIDC client of Authelia
                                                         │  holds the per-host agent keys
                                                         │  egress: toCIDR audio hosts, agent port only
                       ┌─────────────────────────────────┼───────────────────────┐
                       ▼                                 ▼                       ▼
                toppingd @ office               toppingd @ living-room     toppingd @ kitchen
                (systemd, DietPi)               (systemd, DietPi)          (systemd, DietPi)
                       │ USB HID                         │                       │
                    DAC (model ?)                   D90 III (unverified)    D50s (not in DEVICES)
```

- **`toppingd`**: one small agent on each audio host. It is the only process that opens the DAC
  on its own initiative, and **it is where the hardware rules are enforced**. It speaks a
  small HTTP+JSON API on the LAN. Every request is signed by `toppingweb` (see "Agent
  authentication"). Browsers never talk to it.
- **`toppingweb`**: one frontend in the cluster. It serves the UI, handles sign-in, keeps the
  room list, and forwards requests to the right agent. It never touches USB and holds no
  device rules of its own beyond repeating the agent's answers for display. If it disagrees
  with an agent, the agent wins because the agent sends the frames.

This is decision 4 ("a small agent on each audio host serving an authenticated API; one UI
talks to all of them"). The only refinement is that "one UI" is a thin server, not a static
page calling agents directly. A browser that called agents directly would need a key for
every agent and a route through to every IoT host, and that conflicts with "without exposing
per-host agents directly" in the ticket.

### Direction: the frontend calls the agents

01-022 §2 has agents **register outbound** to the hub, because the machines in that design
were laptops that sleep, roam and sit behind NAT. The audio nodes are fixed Pis in fixed rooms
on router reservations, so **(proposal)** `toppingweb` calls each agent at an address listed
in its config. That means:

- no registry or heartbeat protocol to build,
- no inbound path from the IoT VLAN into the cluster (`snapcast.md` notes that LAN-to-LB
  traffic is SNATed, so `fromEntities: world` would be needed),
- and "is the room reachable?" is simply "did the last call answer?".

The cost is that a host whose IP moves silently drops out until the config is updated.
`physical-topology.md` warns about exactly that for `living-room`. See question 2.

### Discovery

There is none to build. The room list is static YAML in the cluster **(proposal)**:

```yaml
# rooms.yaml (ConfigMap)
rooms:
  - id: office
    name: Office
    agent: http://office.<lan-domain>:8740   # (proposal) port; name or reserved IP, see Q2
    expect_model: dx5ii                       # cross-check against what the agent reports
  - id: living-room
    name: Living room
    agent: http://living-room.<lan-domain>:8740
    expect_model: d90iii
```

Each agent reports what it actually found: product string, matched `DEVICES` key, `status`
from `DEVICES`, and its capability list. `expect_model` is a cross-check, not a grant. If the
agent reports a different model (a DAC was swapped), the room shows **"Different DAC than
expected: D90 III Discrete"** and is read-only until the YAML is edited. Write permission
always comes from the agent's `DEVICES` table, never from config.

### When a room is offline

`toppingweb` sorts each room into one of these, and the UI uses the same words:

| State | How it's detected | What the room card shows | Writes |
| --- | --- | --- | --- |
| Reachable, DAC readable | agent answers, last read succeeded | values, each with source and age | per capability |
| **Host unreachable** | connect error, or no connection within 1.5 s (proposal). That is the *connect* timeout. The *response* timeout is longer (proposal: 10 s), because a fresh read alone takes about 2 s | "Unreachable · office · last seen 14 min ago". Last-known values greyed and labelled "last known" | disabled |
| **Agent up, no DAC** | agent answers, `find_devices()` finds no match | "No DAC found on office". Lists unrecognised Thesycon devices if there are any | disabled |
| **Unrecognised DAC** | product string not in `DEVICES` | "Unrecognised DAC: <product string>. Read-only" | disabled |
| **Read failed** | device opened but GetSettings returned nothing (seen on the D90 III, `toppingctl.py` `DEVICES["d90iii"]` notes) | values "Unknown", with the agent's error in one line | disabled for anything that needs a fresh read |
| **Asleep** | **(unknown)**. The spec doesn't say what GetSettings returns in standby. The capabilities doc notes writes to a sleeping unit were accepted silently | to be decided once measured (build step 2) | decided in build step 6 |

Writes are **never queued**. A change sent while a room is unreachable fails at once with
"office is unreachable. Nothing was sent." It isn't replayed when the host comes back, because
a write replayed minutes later acts on a state nobody has looked at.

## Where each part runs and how it's deployed

### `toppingd` on each audio host

- **Code:** `toppingd.py` at the top level of this repo. The existing installer copies
  `*.py` into `/opt/toppingctl/`, so it arrives with no change to how sources are fetched.
- **Runtime:** the existing `/opt/toppingctl-venv`, plus `pyyaml` for its config (house rule:
  YAML). The installer only runs `pip install` when it creates the venv
  (`if [ ! -x /opt/toppingctl-venv/bin/python ]`), so the homelab PR adds `pyyaml` outside
  that guard, or existing nodes never get it. The HTTP server is stdlib `http.server.ThreadingHTTPServer`, so `hid` and `pyyaml` are
  the only dependencies on the Pis. **Python 3.11** has to keep working (bookworm nodes, and
  the CI matrix comment). `living-room` runs Trixie, which has a newer Python.
- **Service:** a systemd unit, `toppingd.service`, running as a dedicated unprivileged user
  `toppingd`. A udev rule grants that user's group access to the Topping hidraw nodes
  (`SUBSYSTEM=="hidraw", ATTRS{idVendor}=="152a", GROUP="toppingd", MODE="0660"`, proposal).
  The CLI keeps working as root.
- **Config:** `/etc/toppingd/toppingd.yaml` (shipped as `toppingd.yaml.example`):

  ```yaml
  listen: 0.0.0.0:8740          # (proposal)
  room: office                  # defaults to the hostname
  device: auto                  # or a DEVICES key; auto = match the product string
  mode: dry-run                 # dry-run | live. Ships as dry-run, see "Dry-run first"
  key_file: /etc/toppingd/key   # HMAC key shared with toppingweb, mode 0600
  state_dir: /var/lib/toppingctl
  ```

  Nothing in this file can make an unverified model writable. `--unverified` has no
  equivalent in the agent.
- **Deploying it** is a PR to `gjcourt/homelab` that extends section 8 of
  `Automation_Custom_Script.sh` with the user, udev rule, unit, `pip install pyyaml` and an
  example config. George re-runs it on the existing nodes and places each host's key. The
  agent doesn't create keys.
- **Version pinning:** the installer fetches `TOPPINGCTL_REF` (default `main`). An agent on
  `main` would change behaviour whenever `main` moves. **(proposal)** Pin nodes running
  `toppingd` to a tag. Changing the variable isn't enough: the installer fetches
  `archive/refs/heads/${TOPPINGCTL_REF}.tar.gz`, which only resolves branches, and on a failed
  fetch it logs a warning and keeps the old sources. So the homelab PR changes the URL as well.
  See question 9.

### `toppingweb` in the cluster

- **Code:** `web/` in this repo (see "Repo layout"). It is Python 3.12+ with Starlette, uvicorn,
  `authlib` and `itsdangerous`, which is the stack the plane-mobile plan picked for the same
  sign-in. The UI is static HTML, CSS and JS built from the approved prototype, with no build
  step.
- **Image:** `ghcr.io/gjcourt/toppingweb`, tagged `YYYY-MM-DD-sha7` like the homelab's other
  images (Renovate's `ghcr.io/gjcourt/**` rule). It needs a `build.yml` workflow in this repo,
  and **George has to add that**, because agents have no Workflows permission. Most homelab
  image builds target `linux/amd64` only. I haven't checked every cluster node's architecture.
- **Manifests** live in a `gjcourt/homelab` PR: `apps/base/toppingctl/` plus a production
  overlay containing:
  - a namespace labelled `http-ingress: "true"`;
  - a Deployment with 1 replica and a Service. Plans and unlocks live in the agents, and the
    session is a signed cookie, so a second replica would only lose the in-memory "last known"
    cache. One replica also means one poller per room;
  - an HTTPRoute on `app-gateway-production` for `toppingctl.burntbytes.com` (proposal,
    question 10);
  - the `rooms.yaml` ConfigMap;
  - a CiliumNetworkPolicy: egress `toCIDR` to the audio hosts on the agent port only, plus
    Authelia and DNS;
  - a `secret.yaml.example` listing the keys George must create under SOPS:
    `oidc-client-secret`, `session-key`, and one agent key per room.
- **Not in the Cloudflare tunnel.** It is reachable on the LAN and over WireGuard only, like
  Plane and the plane-mobile plan.

### Sign-in

The design is the same as plane-mobile's plan:

1. `toppingweb` is a confidential Authelia OIDC client, `toppingctl`, using the
   authorization-code flow at `two_factor`. PKCE is used if Authelia accepts it alongside a
   client secret. The plane-mobile plan notes no existing client configures PKCE, so it needs
   checking. The redirect is `https://toppingctl.burntbytes.com/auth/callback`.
2. On callback it checks the ID token and admits **only George** (decision 6). The check is an
   `allowed_user` setting, like plane-mobile's `allowedUser`, matched against the
   `preferred_username` claim (proposal: plane-mobile's plan doesn't name the claim). Anyone else gets "This
   app is for one account. You're signed in as <name>." and no session.
3. It then issues its own `HttpOnly; Secure; SameSite=Lax` session cookie. Every write needs a
   custom header (`X-Requested-With: toppingctl`) as a CSRF check, the same as plane-mobile.
4. The Authelia client is registered by adding an entry to `configuration.yaml` with the
   secret's hash. George generates the secret (`openssl rand -hex 32`) and puts the plaintext
   in the app's SOPS secret.

How long a session lasts is question 11. The phone is the main device (decision 10), so a
short session means signing in two-factor every time George walks into a room.

### Agent authentication

The agents sit on the IoT VLAN, which other devices share. Volume levels aren't secret, but
**a forged or replayed write is a safety problem**. **(proposal)** Every request from
`toppingweb` to an agent carries an HMAC-SHA256 signature over method, path, body, a timestamp
and a nonce, keyed with that host's key. The agent rejects:

- a bad signature,
- a timestamp more than 30 s off,
- a nonce it has already seen in that window.

It also rejects any timestamp earlier than its own start, so the nonce cache being lost on a
restart doesn't reopen the replay window. The Pis have no real-time clock, so the agent refuses
writes until the system clock reports NTP sync (`timedatectl show -p NTPSynchronized`).
**Responses are signed with the same key.** Otherwise anyone on the VLAN who can intercept
traffic could forge a reply such as `loud_result: false` and hide a loud room.

That is stdlib `hmac` and needs no certificates. A captured request or response can't be
replayed or altered. It doesn't encrypt anything. If George wants encryption on the LAN leg too, the
alternative is mTLS with a private CA (question 3). A plain bearer token is ruled out: on a
shared VLAN, anyone who captures one request can replay it.

Each host has its own key, so a key leaked from one Pi can't drive another room.

## The hardware rules, enforced in the agent

The UI makes the rules hard to break. **The agent makes them impossible to break through the
API.** Each rule below is enforced in `toppingd` from the same functions the CLI uses, so the
CLI and the agent can't drift apart. Each one gets an offline test that calls the agent's API
(not the UI) and shows the refusal.

| Rule (AGENTS.md / decisions) | How the agent enforces it |
| --- | --- |
| **Only registers confirmed in the spec** | The API has **no raw-frame, register or probe endpoint**. It exposes named operations only. A per-model capability table maps each one to an existing frame builder. A test runs every operation on every model and checks that every `(reg, sub)` emitted is in the confirmed set |
| **Never implement scene save (`71 35`)** | No operation builds it. The agent's send path checks every frame against a deny-list: SaveC1/SaveC2 (`0x7135`/`0x7136`), FactoryReset (`0x710b`) and FirmwareUpdate (`0x712e`). That is the `NEVER` set in `listen.py`, resolved by name through `vendor_commands.py`. A test asserts that each one is refused even when built by hand |
| **Only the DX5 II is confirmed; other models are read-only** | Capabilities come from `DEVICES[key]["status"]`. Anything not `"confirmed"` gets an empty write list. The agent has no `--unverified` and no config switch that grants it. A write to a D90 III returns `403 model_unverified` with the reason in plain words |
| **Input, output and mute: read-only for now** (decision 1) | They are absent from the write list for **every** model, including the DX5 II, until a hardware-confirmation ticket marks each one confirmed in code |
| **Volume above −10 dB needs an explicit force** | A plan whose result is above −10 dB is refused unless the Send request carries `allow_loud: true` for that plan. The agent checks **both** the requested level and the level after quantising to the device's step. `cmd_vol` today checks only the requested `db`, which is safe with Python's `round` but should be explicit |
| **The unlock covers one change, re-locks after Send or Cancel, and times out** (decision 3) | `allow_loud` lives on a single plan, not a session or a room. A plan is single-use and expires **(proposal: 60 s, question 5)**. Cancelling, expiring, sending, or a Send rejected with `409 changed` all discard it, and its unlock with it. The fast-step route refuses `allow_loud` outright |
| **High gain is treated like the unlock** (decision 8) | A plan that switches gain on is "loud" and needs `allow_loud`. Switching it off needs only the normal preview |
| **A preset that raises the preamp needs the unlock if volume + rise > −10 dB** (decision 12) | The agent computes `rise = new_preamp − current_preamp` and requires `allow_loud` if `volume + rise > −10`. The preamp can't be read, so `current_preamp` is always the **last-written** value: correct unless the vendor app changed it since. If nothing was ever written, the rise is unknown, and **(proposal)** that counts as loud, needing the unlock. A preset that declares no preamp leaves `0x9c` alone (`cmd_apply`), so its preamp rise is 0. Its positive band gains still raise the level, and **(proposal)** the largest boost counts as a rise. All of this is question 7 |
| **Scene recall needs the unlock; a loud result offers "Lower to −30 dB"** (decision 2) | **First, recall isn't a confirmed register** (`71 11`/`71 12` are vendor-sourced, and the spec's confirmed table doesn't list them). So `scene.recall` is off for every model until it is confirmed on hardware, like input, output and mute (question 15). Once it is on: a recall plan always needs `allow_loud`, because its result can't be known in advance (`scenes.py`). The agent reads before and after, as `scenes.py` does. If the volume afterwards is above −10 dB, the result carries `loud_result: true` and the UI shows "Lower to −30 dB" first. A recall can also change input and output, which are read-only by decision 1. The diff shows that, and undo restores **volume only** (task analysis, design rule 3) |
| **"Lower to −30 dB" sends at once** (design rule 5) | `POST /v1/volume` with `quieter_only: true` is accepted without a plan **only if** a read taken just before, in the same operation, shows the current level is louder than the target, and the target is at or below −10 dB |
| **Undo of a volume change sends at once if it lands at or below −10 dB; it never goes back up to a loud level in one tap** (design rule 5) | Each result carries an `undo` token naming the previous level and the level just set. `POST /v1/volume {undo}` is accepted without a plan only if the target is ≤ −10 dB and a fresh read still shows the level that change set. Otherwise the undo becomes a normal previewed plan. An undo back above −10 dB is refused on this route. Preset and gain undo are always previewed |
| **+/− steps send at once in the safe range, from a value read this session** (decision 11) | `POST /v1/volume {target_db, expected_db}` without a plan reads the device **in the same operation, just before writing**, and sends only if: (a) target ≤ −10 dB, before and after quantising; (b) the target is one `volumeStep` from `expected_db`, compared in raw device units, not float dB; (c) `expected_db` equals that fresh read. Anything else gets `409` with the fresh state, and the UI falls back to the preview. The fresh read is what stops a "+" from a stale −30 dB jumping 30 dB after the knob went to −60 elsewhere, which is the hazard the task analysis names. Its cost is the read time (question 4). The step's response reports the level as "sent", and the next step's read confirms it |
| **Dry-run first** | (1) Every non-exempt write is two-phase. `POST /v1/plans` builds the frames and returns them, as `--dry-run` does, and only `POST /v1/plans/{id}/send` sends. Unlike today's CLI dry-run, which assumes 0.5 dB per step (`cmd_vol`), a plan reads `volumeStep` from the device, so the frames shown are the frames sent. (2) A new agent ships with `mode: dry-run`, where Send returns the frames and sends nothing. George switches each host to `live` after watching one dry-run plan on the front panel (build step 4) |
| **Only absolute targets** (task analysis) | The API has no relative volume. `target_db` is always absolute, so a retried request gives the same result |

Two rules the agent **can't** enforce, stated so the UI doesn't imply it does:

- **The level at the ear.** The UAC2/ALSA stage that Home Assistant and snapclient drive sits
  in series with the DAC's volume (homelab `snapcast.md`). −10 dB on the DAC is a ceiling on
  one stage, not the listening level. Whether the agent should also *read* the ALSA stage for
  display is question 12.
- **Writes from elsewhere.** The front panel, the IR remote, Home Assistant's IR blaster and
  the vendor app can all set a loud level, and the agent only sees it at its next read.

One gap is in this repo, not the agent: **`scenes.py recall` has no −10 dB guard and no
`--force` today.** Sharing guards with the CLI only helps if the CLI has them. Question 16
asks whether step 1 closes it.

### The agent API

This is a sketch **(proposal)**. JSON throughout, and every response says where each value
came from.

| Route | Purpose |
| --- | --- |
| `GET /v1/room` | Identity: host, room, product string, `DEVICES` key, `status`, capabilities, `mode` (dry-run or live), agent version |
| `GET /v1/state?fresh=1` | Current state. Every field is `{value, source: "device" \| "written" \| "unknown", at}`. `fresh=1` forces a GetSettings read. Without it, the cached read comes back with its age |
| `POST /v1/plans` | `{op, args, expected}`. The agent reads the device, applies the rules and builds the frames, then returns `{plan_id, summary, before, after_predicted, frames[], needs_unlock, expires_at}`. Nothing is sent |
| `POST /v1/plans/{id}/send` | `{allow_loud?}`. The agent re-reads. If any field **the operation depends on** differs from the plan's `before`, it returns `409 changed`, sends nothing, and discards the plan. For volume and gain that means volume, `volumeStep`, gain and output; fields such as `sampleRate` change with playback and are ignored. Otherwise it sends exactly the planned frames, reads again, and returns `{before, after, loud_result, undo}`, all under one device open and one lock |
| `POST /v1/plans/{id}/cancel` | Discards the plan, and its unlock with it |
| `POST /v1/volume` | Fast path only: a safe step, `quieter_only`, or a safe `undo`. Everything else is refused with a pointer to `/v1/plans` |

The operations are `volume.set`, `gain.set`, `preset.apply` and `preset.flat`, then
`scene.recall` once it is confirmed (question 15), then `power.set` (decision 7). That is the
whole write surface.

**Latency (estimate, unmeasured):** with today's fixed 2 s read, a previewed change costs about
2 s for the plan, then about 4 s for Send (re-read, write, read-back), and a fast step about
2 s. Making the read stop early (build step 1) is what makes the phone usable.

`toppingweb` mirrors these under `/api/rooms/{room}/…`, adds the signature, and passes answers
through unchanged.

## Concurrency and stale reads

George is the only user (decision 6), but there are still several writers.

| Writer | What the plan does |
| --- | --- |
| George on two devices (phone and laptop tabs) | Compare-and-set. Every write carries `expected`, and the agent checks it against a fresh read. A mismatch returns `409` with the new state, and the UI says "Changed elsewhere: now −24.0 dB (read 0 s ago)". It doesn't resend |
| The CLI on the same host | **(proposal)** An advisory `flock` on `device.lock` in the state directory, taken **once per command or agent operation**, at the top. It is not taken inside `Device` or `open_checked`: `cmd_vol` opens the device twice (the `volumeStep` read, then the write), and with a non-reentrant `flock` the second open would block on the first. Putting the lock in the state directory, not `/run`, keeps it working on macOS. The CLI waits a few seconds and then says "toppingd is using the DAC". The agent opens the device per operation and closes it after, so the CLI works whenever the agent is idle |
| Front panel, IR remote, Home Assistant's IR blaster | Invisible until the next read. The agent reads before every plan, every Send and every fast step. While a UI is open on a room, `toppingweb` polls `state?fresh=1` (proposal: every 10 s, one poller per room however many tabs are open). With no viewer, it doesn't poll. At 2 s per read, that polling holds the DAC about 20 % of the time, which is another reason for the early-stopping read |
| Vendor web app | The code already records that the vendor app takes the device and "only one client gets it" (`devstate.py:128–130`). The agent retries an open a few times, as `read_settings` does, then reports "The DAC is in use by another app" and disables writes. The flock can't cover the vendor app, so between two of the agent's operations it can take the device. That is why every operation re-reads. If the DAC is plugged into another computer, the agent reports "No DAC found" |
| PEQ writes in progress | A preset is 110 band frames, 4 preamp frames and a commit. Under the per-device lock, nothing else interleaves. If the HID write fails partway, the result says "Preset partly written. Re-apply or apply Flat" and the preset record is set to `unknown`, not to the new name |

**Stale state is labelled, never hidden.** The agent never turns a cached value into a device
value. Every field is one of:

- **read from device**, with its age;
- **last written by toppingctl**, which covers the PEQ preset (decision 9), whose bands can't
  be read back;
- **unknown**.

`toppingweb` keeps the last answer from each agent in memory only so it can show "last known"
while a host is down, always marked as such.

**Where "last written" lives.** Today the CLI caches it in `~/.toppingctl/state.json` for
whoever ran it. That is root on the nodes, so the agent can't see it. **(proposal)** Add a
`TOPPINGCTL_STATE_DIR` environment override to `toppingctl.py` and point both the CLI wrapper
and the agent at `/var/lib/toppingctl`. A preset written from the CLI then shows in the UI as
"last written by toppingctl". `save_state` writes with the default umask, so a root CLI run
would leave a file the `toppingd` user can't rewrite. To prevent that, the directory is
`root:toppingd`, mode `2775`, and the CLI wrapper sets `umask 002`. See question 8.

## Repo layout and language

This follows AGENTS.md: `toppingctl.py` stays a single-file CLI and the source of the frame
builders and guards. New entry points sit beside it, as `scenes.py` and `setctl.py` already do.

```
toppingctl.py            + plan_* functions that return frames or raise Refused (see below)
                         + TOPPINGCTL_STATE_DIR override, + device flock
toppingd.py              per-host agent: stdlib HTTP server, HMAC check, policy, one HID worker
toppingd.yaml.example    agent config (YAML)
test_offline.py          + policy tests for the new plan_* functions
test_toppingd.py         agent API tests against a fake HID device, no hardware
docs/plans/              this plan
docs/ui/                 task analysis, prototype r1, prototype r2 (build step 3)
web/
  pyproject.toml         uv project: starlette, uvicorn, authlib, itsdangerous, httpx, pyyaml
  src/toppingweb/        app, OIDC, room registry, agent client (signing)
  static/                the UI from the approved prototype: HTML, CSS, JS
  tests/
  rooms.yaml.example
  Dockerfile
```

- **The first code change is a refactor in `toppingctl.py`.** Pure functions such as
  `plan_volume(spec, db, step_db, allow_loud) -> list[Frame]` raise `Refused(reason)` instead
  of calling `sys.exit`. The `cmd_*` functions become thin wrappers that print and exit as
  they do now. The agent calls the same `plan_*` functions, so there is **one** −10 dB check,
  **one** unverified check and **one** frame builder for both. The test of the refactor: every
  `--dry-run` command in CI prints byte-identical output before and after. That doesn't
  exercise the live `volumeStep` read, which dry-run skips, so the refactor also gets an
  offline test with a fake device answering `one_db`.
- `read_settings` takes an already-open handle as an alternative to opening its own, so one
  agent operation (read, write, read-back) is one open.
- **Language:** Python in both parts. The agent has to be Python to reuse the frame builders.
  The web side matches plane-mobile's stack, so the sign-in design carries over.
- **Configuration is YAML:** `toppingd.yaml` and `rooms.yaml`. The one TOML file is
  `web/pyproject.toml`, which uv requires (house rule). It holds no ruff settings, so the
  existing root `ruff.toml` still governs lint. This PR doesn't move or change that file.
- **Lint and tests:** the existing gate (`ruff check`, `ruff format --check`, `python3 -m
  unittest test_offline`), plus `test_toppingd` on both CI interpreters and `web/` tests
  (pytest) on 3.12+. CI changes need a workflow edit, which George makes.

## Build order

Each step ends in something George can check. Stopping after step 3 is a real option, as
01-022 §6 says: it answers "what is each room doing?" with no write risk.

0. **Approve this plan.** Separately, open the hardware-confirmation tickets for input,
   output and mute (decision 1), and for scene recall if question 15 is (a). Settle question 1
   (which room has a writable DAC).
1. **Refactor `toppingctl.py`**: the `plan_*` functions, `Refused`, the state-dir override, the
   flock, `read_settings` on a shared handle, and a GetSettings read that stops once every
   record has arrived instead of waiting out the 2 s window. Tests: byte-identical dry-run output
   for every CI command, a fake-device `one_db` test, and one test for each guard in the rules
   table, called directly. If question 16 is (a), `scenes.py` gets its loud guard here too.
2. **`toppingd`, read-only, `mode: dry-run`.** It provides `/v1/room`, `/v1/state` and the
   HMAC check, with tests against a fake HID device. A homelab PR installs it on **one** host.
   Measure on hardware and record in the spec or this repo:
   - how long a GetSettings read really takes, and whether it can stop early;
   - what reads return while the DAC is asleep;
   - what happens with the CLI and the agent in turn, and with the vendor app open on the
     same host;
   - what happens when the DAC is unplugged and replugged.
3. **`toppingweb`, read-only**, preceded by **prototype r2** (UI.md: the prototype comes
   before production UI). r2 adds:
   - fast +/− steps,
   - the unlock timeout,
   - gain,
   - sleep and wake with preview and undo (decision 7: "in r2"),
   - the offline states above,
   - and the "Changed elsewhere" message.

   George evaluates it. Then build sign-in, the
   room list and live state behind Authelia, and deploy via a homelab PR.
4. **Volume writes on the DX5 II**: the fast path, plans, the unlock and "Lower to −30 dB".
   Switch that host to `mode: live` only after watching a dry-run plan, then one live −1 dB
   step on the front panel, then `smoke.py` (the AGENTS.md sequence).
5. **Presets and gain**, each with its own front-panel check. **Scene recall** joins once it
   is confirmed on hardware (question 15).
6. **Sleep and wake**, built from prototype r2, with preview and undo. It is blocked on the
   asleep-state measurement from step 2. Waking resumes the stored volume, so question 13
   asks whether waking should be treated like a scene recall.

### Tests that prove the rules hold without the UI

All of these run offline in CI and call the agent API, not the UI:

- A write to an `unverified` or unknown model is refused for every operation.
- No sequence of API calls emits SaveC1/SaveC2 (`71 35`, `71 36`), FactoryReset or
  FirmwareUpdate, or any `(reg, sub)` outside the confirmed set. While it is unconfirmed, that
  includes scene recall.
- A volume above −10 dB is refused without `allow_loud`. It is also refused with `allow_loud`
  on the fast path, and with `allow_loud` on an expired, cancelled, already-sent or
  409-rejected plan.
- Rounding to the device step can't take a level that was accepted as "≤ −10 dB" above it,
  under either `volumeStep`.
- Gain on, scene recall (once enabled), and a preset whose rise crosses −10 dB each need the
  unlock. That covers an unknown current preamp, and a preset with no preamp but positive band
  gain.
- A fast step is refused when `expected_db` doesn't match the read taken in the same
  operation, or when the step is bigger than one `volumeStep`. A fake device that "moves the
  knob" between two steps proves it.
- `quieter_only` is refused when the target isn't quieter than a fresh read. A fast `undo` is
  refused above −10 dB, or when the level has changed since the change it undoes.
- Send ignores changes to fields the operation doesn't depend on, such as `sampleRate`.
- Signature failures, replayed nonces, skewed timestamps and timestamps from before the agent
  started are rejected, on requests and on responses.
- Every `toppingweb` route except `/healthz` and `/auth/*` needs a session belonging to the
  allowed user, and writes need the CSRF header.

## Questions for George

1. **Which audio host has the DX5 II?** I couldn't find it in `gjcourt/homelab`. The two rooms I
   could place have a D90 III (unverified, and its volume isn't on HID) and a D50s (not in
   `DEVICES`). If no room has a DX5 II, the web UI starts read-only everywhere.
   - (a) Tell me the host, and step 2 starts there.
   - (b) The DX5 II stays on a desk machine, so the agent has to run there too (not DietPi;
     this needs an install path).
   - (c) Ship read-only everywhere for now. Writes wait until a room has a confirmed DAC.
2. **How does `toppingweb` reach the agents?**
   - (a) It calls each agent at an address in `rooms.yaml`, preferably a DNS name (whether the
     audio hosts resolve by name from the cluster is **unknown**). *(proposal)*
   - (b) Agents register outbound with heartbeats, as in lab 01-022. This handles moving IPs
     and roaming machines, but costs a registry and an inbound route into the cluster.
   - (c) (a) now, and (b) later if a non-Pi host joins.
3. **How should the agent authenticate requests from the cluster?**
   - (a) A per-host HMAC signature with a timestamp and nonce: no replay, no certificates, no
     encryption. *(proposal)*
   - (b) mTLS with a private CA: encryption as well, at the cost of certificates on every Pi.
   - (c) (a), plus a host firewall on each Pi allowing the agent port only from the cluster's
     egress addresses.
4. **How fresh must the read be for +/− to send at once?** Decision 11 says "a value read this
   session". Anything older than the moment of the tap lets a "+" from −30 dB land at −29 dB
   after someone turned the knob down to −60. That stays under −10 dB, but it is a 31 dB jump.
   - (a) The agent reads just before each step, in the same operation. Each tap costs one read:
     about 2 s today, less after build step 1's early-stopping read (unmeasured).
     *(proposal)*
   - (b) A read up to 30 s old, with `toppingweb` polling every 10 s while the room is open.
     This is faster, but it accepts the jump above.
   - (c) (a), but taps made while a step is in flight merge into one target, capped at
     3 dB louder than the fresh read.
5. **How long is the unlock's "short timeout"?**
   - (a) 30 s.
   - (b) 60 s. *(proposal)*
   - (c) 120 s.
6. **Where do plans and unlocks live?**
   - (a) In the agent's memory only. They are lost on restart, which is safe: George just
     previews again. *(proposal)*
   - (b) Also in `toppingweb`, so the UI can resume a pending preview after a reload.
7. **The preamp can't be read**, so decision 12's rule always rests on the last-written
   value, which the vendor app could have changed since. Which base should the rise be measured
   from?
   - (a) The last-written preamp when there is one. When there isn't, the rise is unknown and
     needs the unlock. A preset with no preamp counts its largest positive band gain as the
     rise. *(proposal)*
   - (b) Always assume the worst case: the new preamp minus the lowest preamp in `presets/`
     (−6.7 dB today). Require the unlock if that would cross −10 dB. This ignores the
     last-written value entirely.
   - (c) Lower the volume by the worst-case rise first, then apply, and say so.
8. **Should the CLI and the agent share the "last written" record** (`/var/lib/toppingctl`)?
   - (a) Yes, so a preset applied from the CLI shows up in the UI. *(proposal)*
   - (b) No. The UI only knows what the UI wrote.
9. **Which toppingctl version should the nodes run?**
   - (a) Pin `TOPPINGCTL_REF` to a release tag on nodes running `toppingd`, bumped by a homelab
     PR. *(proposal)*
   - (b) Keep tracking `main`.
10. **Hostname:**
    - (a) `toppingctl.burntbytes.com`. *(proposal)*
    - (b) `dac.burntbytes.com`.
    - (c) Something else.
11. **How long should a session last before signing in again?**
    - (a) 30 days, the plane-mobile proposal. A stolen unlocked phone can then reach the unlock
      checkbox.
    - (b) 7 days.
    - (c) Authelia's own 1 h session. That means signing in two-factor most times he picks up
      the phone.
12. **The UAC2/ALSA volume stage** (snapclient / Home Assistant) also sets the level, in series:
    - (a) Show it read-only in the room view, so the level George hears is less of a guess.
      This means the agent runs `amixer` read-only.
    - (b) Leave it out. The UI says "DAC volume" and nothing more.
13. **Wake (build step 6) resumes the stored volume, which may be loud.**
    - (a) Treat wake like a scene recall: read afterwards, and offer "Lower to −30 dB" if it is
      loud. *(proposal)*
    - (b) Wake needs the unlock when the last read volume was above −10 dB.
    - (c) No special handling.
14. **Where does `web/` live?**
    - (a) In this repo, so the agent API and its client are tested together. *(proposal)*
    - (b) A new repo, `gjcourt/toppingweb`, which keeps this repo a CLI.
15. **Scene recall isn't a confirmed register.** `71 11`/`71 12` are vendor-sourced, and the
    spec's confirmed table doesn't list them. AGENTS.md allows writes only to confirmed
    registers. Decision 2 covers how recall is guarded, not whether it may skip confirmation. A
    recall can also change input and output, which decision 1 keeps read-only.
    - (a) Leave it off in the web UI until a hardware-confirmation ticket confirms it (watch the
      front panel through a C1 and a C2 recall, then update the spec). *(proposal)*
    - (b) Enable it now behind the unlock (decision 2), as a deliberate exception to the
      AGENTS.md rule, and record that in AGENTS.md.
    - (c) Leave it out of the web UI entirely, so it stays on the remote and the CLI.
16. **`scenes.py recall` has no −10 dB guard today.** A CLI recall can land at any level
    without `--force`.
    - (a) In build step 1, give it the same post-recall check: require `--force`, and print
      "Lower with: toppingctl vol -30" when the result is loud. *(proposal)*
    - (b) Leave the CLI as it is. The guard is web-only.
