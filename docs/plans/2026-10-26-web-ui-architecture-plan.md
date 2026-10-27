# Networked, multi-room toppingctl: architecture plan

Ticket: [TOPPINGCTL-2](https://plane.burntbytes.com/home/browse/TOPPINGCTL-2/). Status:
**revised 2026-10-27** with George's answers to the plan's questions and his review of PR #28.
This PR only adds the plan, with no code.

Inputs:

- George's decisions, quoted in full under "Decisions" below.
- The task analysis, [`docs/ui/task-analysis.md`](../ui/task-analysis.md), and the r1
  prototype, [`docs/ui/prototype-r1.html`](../ui/prototype-r1.html) (PR #27).
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
- PR #25, which marks the DX1 II's `0x1106` PEQ dump as an unverified read. **It should merge
  before the step-1 refactor starts** (George, review of PR #28).

Anything not established by those sources or by George is marked **(proposal)**,
**(unknown)** or **(unmeasured)**.

## Decisions

### From TOPPINGCTL-2 (George, 2026-10-26), answering the task analysis in PR #27

Quoted from the ticket:

1. "Input, output and mute: read-only for now; a hardware-confirmation ticket for each."
2. "Scene recall needs the "Allow above −10 dB" unlock; a loud result offers "Lower to
   −30 dB"." *(Superseded for the web build by answer Q15 below: scene recall is out for now.)*
3. "The −10 dB unlock: a checkbox covering one change, re-locking after Send or Cancel, plus a
   short timeout."
4. "Topology: a small agent on each audio host serving an authenticated API; one UI talks to
   all of them."
5. "Sign-in: the homelab's existing Authelia SSO (OIDC), as plane-mobile does."
6. "Users: only George."
7. "Sleep and wake: yes, in r2, with preview and undo."
8. "Headphone gain: switching to high gain is treated like unlocking above −10 dB."
9. "PEQ preset: shown, labelled as last written by toppingctl."
10. "Use: mostly in the room, on his phone, with volume the main task."
11. "Volume steps: within the safe range (at or below −10 dB) and from a value read this
    session, +/− steps send at once; typed targets, presets, scenes and anything loud keep the
    preview."
12. "A preset that raises the preamp requires the unlock when volume plus the preamp rise would
    go above −10 dB."

### Answers to this plan's questions (George, 2026-10-26, ticket comment)

Quoted from the comment:

- **Q1, DX5 II host:** "it is in the office and plugged in. Step 4's first writes target the
  office host."
- **Q3, agent auth:** "signed requests (per-host HMAC with timestamp and nonce, signed
  responses), as proposed. No mTLS, no host firewall."
- **Q4, +/− freshness:** "re-read the device in the same operation before every step, as
  proposed."
- **Q5, unlock:** "60 s, covering one planned change, as proposed."
- **Q7, preamp-rise base:** "the last-written value; unknown counts as loud; with no preamp,
  the largest band boost counts, as proposed."
- **Q11, session length:** "30 days."
- **Q12, ALSA stage:** "don't show it in the web UI."
- **Q15, scene recall:** "leave it out of the web build for now. A later ticket adds a
  front-panel confirmation and then the feature."
- **Q16, CLI guard:** "yes, give scenes.py recall the −10 dB guard in build step 1."
- **Q2, Q6, Q8, Q9, Q10, Q13, Q14:** "accept the plan's defaults."

The accepted defaults are:

| Question | Default accepted |
| --- | --- |
| Q2, how the frontend reaches agents | `toppingweb` calls each agent at an address in `rooms.yaml`, preferably a DNS name |
| Q6, where plans and unlocks live | In the agent's memory only. They are lost on restart, which is safe |
| Q8, shared "last written" record | Yes: the CLI and the agent share `/var/lib/toppingctl` |
| Q9, version on the nodes | Pin `TOPPINGCTL_REF` to a release tag on nodes running `toppingd`, bumped by a homelab PR |
| Q10, hostname | `toppingctl.burntbytes.com` |
| Q13, wake resumes a stored, possibly loud volume | Read after waking, and offer "Lower to −30 dB" if it is loud |
| Q14, where `web/` lives | In this repo |

The review of PR #28 also asked for these changes, all made below:

- say whether the agent reads non-DX5 protocols, and exclude `0x1106`;
- fix the step order around the early-stopping read;
- split step 1 into about four tickets;
- create the confirmed register set in step 1;
- mark cluster-to-IoT-VLAN reachability as unknown;
- fix tag pinning for `v`-prefixed tags;
- state the CLI's lock wait against `listen.py`'s 10 s;
- take scene recall out of steps 4–5 and the wake question.

## What the plan has to work with

These facts were checked in the sources above. Each one constrains the design.

| Fact | Source | Consequence |
| --- | --- | --- |
| toppingctl is already on every DietPi audio node. It lives in a venv at `/opt/toppingctl-venv` (PyPI `hid`) with sources in `/opt/toppingctl/*.py`, and is installed by the first-boot `Automation_Custom_Script.sh`, section 8. There is no service, unit or listening port | homelab `hosts/dietpi-audio/` | The agent can be one more top-level `*.py` that the existing installer already copies. It needs a systemd unit and one more pip package, not a new install path |
| The installer runs once, as root. There is no Ansible and no agent on the nodes | same | Deploying the agent is a homelab PR to that script, plus a re-run on each existing node |
| The hostname is the room name (`kitchen`, `living-room`, `office`, and a reference node `snap-test`) | same, README | One agent per host, one DAC per agent. The host name is the room's identity, as 01-022 §3 concludes |
| **The DX5 II is in the office and plugged in.** `living-room` has a **D90 III Discrete** (unverified; volume isn't on HID). `kitchen` has a **D50s** per `snapcast.md`, which is not in `DEVICES` | George (Q1); homelab docs | The office is the one room with a writable DAC. Step 4's first writes go there |
| The DAC's own volume is a stage **in series** with the UAC2/ALSA volume that snapclient and Home Assistant drive, and the two add in dB | homelab `snapcast.md` | The −10 dB rule governs the DAC's own stage only. The ALSA stage isn't shown (Q12), so the UI labels the figure "DAC volume" and doesn't imply the level at the ear is known |
| Home Assistant has IR-blaster buttons for the DX5 II's volume, mute, power and C1/C2 | homelab `homeassistant/files/dashboards/control.yaml` | That is another writer the agent can't see, alongside the front panel, the remote and the vendor app |
| Authelia runs at `auth.burntbytes.com`. OIDC clients are inline in `apps/production/authelia/configuration.yaml`, each with a hashed secret and `authorization_policy: two_factor`. The plaintext secret goes in the app's SOPS secret | homelab Authelia, `docs/operations/apps/authelia.md` | The UI is its own OIDC client. George adds the secret, since SOPS is operator-only |
| The gateway's Authelia `ext_authz` filter **does not enforce** on Cilium Gateway API listeners. Incident status: "Ongoing" | homelab `cilium-envoy-config-production.yaml`, the 2026-02-27 incident doc | Forward-auth can't be relied on. Sign-in has to live in the app, which is also what plane-mobile plans |
| plane-mobile's sign-in is **planned, not built**: server-side authorization-code flow with `authlib`, then its own `HttpOnly; Secure; SameSite=Lax` session cookie, then an `allowedUser` check, on Starlette | plane-mobile plan, lines ~195–198 and ~742–753 | "As plane-mobile does" means copying that design. There is no working code to reuse yet |
| An app is LAN-only by default: an HTTPRoute on `app-gateway-production` and nothing in the Cloudflare tunnel. WireGuard reaches the LAN, so it reaches the app | homelab gateway, cloudflare-tunnel README | Nothing needs to be exposed to the internet |
| Pods reach LAN hosts through a CiliumNetworkPolicy `toCIDR` egress rule. "LAN hosts are classified as world by Cilium". That covers the cluster's own policy. **Whether the router lets the cluster VLAN open connections into the IoT VLAN, on port 8740 or at all, is (unknown)** | homelab `homeassistant/networkpolicy.yaml`; the gateway firewall config isn't in the repo | The frontend gets an egress rule to the audio hosts' agent port and nothing else. Router reachability is checked in step 2, before anything depends on it |
| The device "never reports volume or PEQ state back" on its own (§6), and nobody has tested whether front-panel changes produce a push. GetSettings (`71 0c`, readNack) returns the whole configuration and is read-safe on the DX5 II (§5; §4a still lists it as "Unknown") | spec §4a, §5, §6 | Stale state is detected by **polling and reading before every write**, not by listening |
| The DX1 II speaks a different protocol (`"protocol": "dx1"`). A readNack is only a read for registers on the vendor's read-safe list; on any other register **it writes the data field**. `devstate.dx1_state` also reads the `0x1106` PEQ dump, whose read-safety PR #25 marks as an unverified contributor claim | `toppingctl.py` `DEVICES["dx1ii"]`; `devstate.py` `dx1_query`, `dx1_state`; PR #25 | Reading is per protocol. The agent's DX1 II reader leaves out `0x1106` until it is confirmed (see "What the agent reads") |
| Only one client gets the device: "the vendor web app claims it over WebHID, and only one client gets it", so `read_settings` retries the open | `devstate.py:128–130` | The agent mustn't hold the handle permanently, or the vendor app and the CLI lose the DAC. It opens once **per operation**, so a read, write and read-back share one handle. Lab 01-022 §5 still calls the agent-plus-vendor-app case untested, and sleep and hot-plug "undesigned" |
| `read_settings()` listens for a fixed 2 s window. The spec gives no single record count to stop at: §5 measured "61 reply frames", and the capabilities doc says "52 settings records exposed on firmware ≥ 2.40" | `devstate.py:126`; spec §5; `topping-dac-capabilities.md` | A fresh read costs about 2 s today. Stopping early needs a measured count first, so it comes **after** the step-2 measurement |
| Scene recall (`71 11`/`71 12`, CallC1/CallC2) is **vendor-sourced and not in the spec's confirmed table**. A recall can change input, output and PEQ at once | spec §5; `scenes.py` docstring | Out of the web build (Q15). A later ticket confirms it on the front panel, then adds it |
| The preamp (`0x9c`) **can't be read back**. GetSettings has no preamp field, so "current preamp" only ever means "last written by toppingctl" | `vendor_commands.py` `SETTINGS_FIELDS`; `toppingctl.py` `cmd_apply` | Decision 12's rule uses the last-written value. Unknown counts as loud (Q7) |
| `cmd_*` functions end with `sys.exit()` and `print()` | `toppingctl.py` | The guards need extracting into functions that return frames or raise. The agent can't call the CLI functions as they are |
| No list of confirmed registers exists in code. `DEVICES[...]["status"]` gates whole models, not registers | `toppingctl.py` | Step 1 creates the list from the spec's confirmed tables (see "The confirmed register set") |

**One spec discrepancy to flag.** Spec §5 says the volume scale under `volumeStep = one_db` is
unknown. `toppingctl.py` (`cmd_vol`) records a front-panel measurement from 2026-08-27 that the
raw unit follows `volumeStep`. The code's note is newer and measured, so this plan keeps the
code's behaviour: read `volumeStep` and refuse if it can't be read. The spec should get the
measurement too, in a separate `gjcourt/lab` change.

## Topology

```
 phone / laptop ──HTTPS──► app-gateway-production ──► toppingweb (cluster, namespace toppingctl)
 (LAN or WireGuard)    toppingctl.burntbytes.com         │  OIDC client of Authelia
                                                         │  holds the per-host agent keys
                                                         │  egress: toCIDR audio hosts, agent port only
                       ┌─────────────────────────────────┼───────────────────────┐
                       ▼                                 ▼                       ▼
                toppingd @ office               toppingd @ living-room     toppingd @ kitchen
                (systemd, DietPi)               (systemd, DietPi)          (systemd, DietPi)
                       │ USB HID                         │                       │
                DX5 II (confirmed)              D90 III (unverified)    D50s (not in DEVICES)
```

- **`toppingd`**: one small agent on each audio host. It is the only process that opens the DAC
  on its own initiative, and **it is where the hardware rules are enforced**. It speaks a small
  HTTP+JSON API on the LAN. Every request and every response is signed (see "Agent
  authentication"). Browsers never talk to it.
- **`toppingweb`**: one frontend in the cluster. It serves the UI, handles sign-in, keeps the
  room list, and forwards requests to the right agent. It never touches USB and holds no
  device rules of its own beyond repeating the agent's answers for display. If it disagrees
  with an agent, the agent wins because the agent sends the frames.

This is decision 4. The only refinement is that "one UI" is a thin server, not a static page
calling agents directly. A browser that called agents directly would need a key for every agent
and a route through to every IoT host, and that conflicts with "without exposing per-host agents
directly" in the ticket.

### Direction: the frontend calls the agents (Q2)

01-022 §2 has agents **register outbound** to the hub, because the machines in that design were
laptops that sleep, roam and sit behind NAT. The audio nodes are fixed Pis in fixed rooms on
router reservations, so `toppingweb` calls each agent at an address listed in its config. That
means:

- no registry or heartbeat protocol to build,
- no inbound path from the IoT VLAN into the cluster (`snapcast.md` notes that LAN-to-LB traffic
  is SNATed, so `fromEntities: world` would be needed),
- and "is the room reachable?" is simply "did the last call answer?".

The cost is that a host whose IP moves silently drops out until the config is updated.
`physical-topology.md` warns about exactly that for `living-room`. Two things are **(unknown)**
and checked in step 2: whether the audio hosts resolve by name from the cluster, and whether the
router lets the cluster VLAN connect into the IoT VLAN at all.

### Discovery

There is none to build. The room list is static YAML in the cluster:

```yaml
# rooms.yaml (ConfigMap)
rooms:
  - id: office
    name: Office
    agent: http://office.<lan-domain>:8740   # (proposal) port; name or reserved IP, see step 2
    expect_model: dx5ii                       # cross-check against what the agent reports
  - id: living-room
    name: Living room
    agent: http://living-room.<lan-domain>:8740
    expect_model: d90iii
```

Each agent reports what it actually found: product string, matched `DEVICES` key, `status` from
`DEVICES`, its read path and its capability list. `expect_model` is a cross-check, not a grant.
If the agent reports a different model (a DAC was swapped), the room shows **"Different DAC
than expected: D90 III Discrete"** and is read-only until the YAML is edited. Write permission
always comes from the agent's `DEVICES` table, never from config.

### What the agent reads, per model

Reading isn't automatically safe. On the DX1 II a readNack to the wrong register writes it. So
the agent reads a model only through a path that is established for that model:

| Model | Read path | Writes |
| --- | --- | --- |
| DX5 II (`confirmed`) | GetSettings (`71 0c` readNack), as `devstate.read_settings` does | Per the rules below |
| DX1 II (`unverified`, `protocol: dx1`) | The vendor read-safe list only, via `devstate.dx1_read_block` (`0x810a`) and `dx1_query` on the listed registers. **Not the `0x1106` PEQ dump** until it is confirmed (PR #25). This needs `dx1_state` to skip the dump on request, which is ticket 1c. Gain, filter, input and other write-only registers aren't read at all. Their values show as "Unknown" | None |
| D90 III Discrete (`unverified`) | **(proposal)** None: identity only, taken from USB enumeration, which sends no frames. The DX5 II GetSettings returned no records on this model (measured 2026-08-28, before a framing fix), and no D90 III read path is established. The room card shows "D90 III Discrete · not read: no confirmed read path for this model" | None |
| Anything else | None: identity only | None |

### Room states

`toppingweb` sorts each room into one of these, and the UI uses the same words:

| State | How it's detected | What the room card shows | Writes |
| --- | --- | --- | --- |
| Reachable, DAC readable | agent answers, last read succeeded | values, each with source and age | per capability |
| **Identified, not read** | agent answers, the model has no read path (table above) | model and product string. Values "Unknown", with the reason in one line | none |
| **Host unreachable** | connect error, or no connection within 1.5 s (proposal). That is the *connect* timeout. The *response* timeout is 10 s (proposal), sized for the slowest Send: a re-read, 115 paced preset frames and a read-back, roughly 4–5 s today (estimate) | "Unreachable · office · last seen 14 min ago". Last-known values greyed and labelled "last known" | disabled |
| **DAC busy** | the agent couldn't take the device lock or open the device in time (see "Concurrency") | "In use by the toppingctl CLI on office" or "In use by another app". Last read kept, with its age | disabled until free |
| **Agent up, no DAC** | agent answers, `find_devices()` finds no match | "No DAC found on office". Lists unrecognised Thesycon devices if there are any | disabled |
| **Unrecognised DAC** | product string not in `DEVICES` | "Unrecognised DAC: <product string>. Read-only" | disabled |
| **Read failed** | device opened but the model's read returned nothing | values "Unknown", with the agent's error in one line | disabled for anything that needs a fresh read |
| **Asleep** | **(unknown)**. The spec doesn't say what GetSettings returns in standby. The capabilities doc notes writes to a sleeping unit were accepted silently | to be decided once measured (step 2) | decided in step 6 |

Writes are **never queued**. A change sent while a room is unreachable fails at once with
"office is unreachable. Nothing was sent." It isn't replayed when the host comes back, because a
write replayed minutes later acts on a state nobody has looked at.

## Where each part runs and how it's deployed

### `toppingd` on each audio host

- **Code:** `toppingd.py` at the top level of this repo. The existing installer copies `*.py`
  into `/opt/toppingctl/`, so it arrives with no change to how sources are fetched.
- **Runtime:** the existing `/opt/toppingctl-venv`, plus `pyyaml` for its config (house rule:
  YAML). The installer only runs `pip install` when it creates the venv
  (`if [ ! -x /opt/toppingctl-venv/bin/python ]`), so the homelab PR adds `pyyaml` outside that
  guard, or existing nodes never get it. The HTTP server is stdlib
  `http.server.ThreadingHTTPServer`, so `hid` and `pyyaml` are the only dependencies on the Pis.
  **Python 3.11** has to keep working (bookworm nodes, and the CI matrix comment). `living-room`
  runs Trixie, which has a newer Python.
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

  Nothing in this file can make an unverified model writable. `--unverified` has no equivalent
  in the agent.
- **Deploying it** is a PR to `gjcourt/homelab` that extends section 8 of
  `Automation_Custom_Script.sh` with the user, udev rule, unit, `pip install pyyaml` and an
  example config. George re-runs it on the existing nodes and places each host's key. The agent
  doesn't create keys.
- **Version pinning (Q9):** nodes running `toppingd` are pinned to a release tag, bumped by a
  homelab PR. The installer needs three changes for that:
  1. It fetches `archive/refs/heads/${TOPPINGCTL_REF}.tar.gz`, which only resolves branches.
     On a failed fetch it logs a warning and keeps the old sources. So the URL changes to one
     that resolves tags (`archive/refs/tags/…`).
  2. It copies from `/tmp/toppingctl-${TOPPINGCTL_REF}/`. George's review notes that GitHub
     strips a leading `v` from the archive directory, so a tag like `v1.0` would break that
     path. **(proposal)** Glob for the extracted directory (`/tmp/toppingctl-*/`) rather than
     building its name. Tags then work with or without a `v`.
  3. It checks that `toppingd.py` arrived before restarting the unit.

### `toppingweb` in the cluster

- **Code:** `web/` in this repo (Q14; see "Repo layout"). It is Python 3.12+ with Starlette,
  uvicorn, `authlib` and `itsdangerous`, which is the stack the plane-mobile plan picked for the
  same sign-in. The UI is static HTML, CSS and JS built from the approved prototype, with no
  build step.
- **Image:** `ghcr.io/gjcourt/toppingweb`, tagged `YYYY-MM-DD-sha7` like the homelab's other
  images (Renovate's `ghcr.io/gjcourt/**` rule). It needs a `build.yml` workflow in this repo,
  and **George has to add that**, because agents have no Workflows permission. Most homelab
  image builds target `linux/amd64` only. I haven't checked every cluster node's architecture.
- **Manifests** live in a `gjcourt/homelab` PR: `apps/base/toppingctl/` plus a production
  overlay containing:
  - a namespace labelled `http-ingress: "true"`;
  - a Deployment with 1 replica and a Service. Plans and unlocks live in the agents (Q6), and
    the session is a signed cookie, so a second replica would only lose the in-memory "last
    known" cache. One replica also means one poller per room;
  - an HTTPRoute on `app-gateway-production` for `toppingctl.burntbytes.com` (Q10);
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
   `preferred_username` claim (proposal: plane-mobile's plan doesn't name the claim). Anyone
   else gets "This app is for one account. You're signed in as <name>." and no session.
3. It then issues its own `HttpOnly; Secure; SameSite=Lax` session cookie, **valid for 30
   days** (Q11). Every write needs a custom header (`X-Requested-With: toppingctl`) as a CSRF
   check, the same as plane-mobile.
4. The Authelia client is registered by adding an entry to `configuration.yaml` with the
   secret's hash. George generates the secret (`openssl rand -hex 32`) and puts the plaintext
   in the app's SOPS secret.

With a 30-day session, a stolen, unlocked phone can reach the unlock checkbox. George accepted
that in Q11, where the option was described that way.

### Agent authentication (Q3)

The agents sit on the IoT VLAN, which other devices share. Volume levels aren't secret, but
**a forged or replayed write is a safety problem**. Every request from `toppingweb` to an agent
carries an HMAC-SHA256 signature over method, path, body, a timestamp and a nonce, keyed with
that host's key. The agent rejects:

- a bad signature,
- a timestamp more than 30 s off,
- a nonce it has already seen in that window.

The Pis have no real-time clock, so the agent accepts nothing until the system clock reports NTP
sync (`timedatectl show -p NTPSynchronized`). At that moment it records a floor and rejects any
timestamp earlier than it, so losing the nonce cache on a restart doesn't reopen the replay
window. The floor is taken at first sync, not at process start: a start-time floor taken from a
stale `fake-hwclock` clock would let old requests through.

**Responses are signed with the same key, and the signature covers the request's nonce.**
`toppingweb` rejects a response whose nonce isn't the one it sent. Otherwise anyone on the VLAN
who can intercept traffic could forge, or replay, a reply such as `loud_result: false` and hide
a loud room.

That is stdlib `hmac` and needs no certificates. A captured request or response can't be
replayed or altered. Nothing is encrypted. Per Q3 there is no mTLS and no host firewall. Each
host has its own key, so a key leaked from one Pi can't drive another room.

## The hardware rules, enforced in the agent

The UI makes the rules hard to break. **The agent makes them impossible to break through the
API.** Each rule below is enforced in `toppingd` from the same functions the CLI uses, so the
CLI and the agent can't drift apart. Each one gets an offline test that calls the agent's API
(not the UI) and shows the refusal.

### The confirmed register set

Step 1 (ticket 1a) adds a `CONFIRMED_WRITES` table to `toppingctl.py`, built from the spec's
confirmed tables and keyed by model. Today only the DX5 II has entries:

| Register | Meaning | Spec |
| --- | --- | --- |
| `71 02` | Volume | §4a, Confirmed |
| `71 17` | Headphone gain | §4a, Confirmed |
| `71 01` | Power (sleep / wake) | §4a, Confirmed |
| `71 34` | Heartbeat, used as commit | §4a, Confirmed |
| `91`–`9b`, subs `01`–`0a` | PEQ bands. `9b` is inert but written so that a stale band 11 gets cleared | §3 "PEQ — confirmed"; §5 band 11 |
| `9c`, subs `01`–`04` | Preamp | §3, CONFIRMED |

Anything not in the table can't be sent. Scene recall (`71 11`/`71 12`), input (`71 04`) and
output (`71 05`) are absent until their hardware-confirmation tickets add them, and mute was
never driven. The table is a code constant, not config.

| Rule (AGENTS.md / decisions) | How the agent enforces it |
| --- | --- |
| **Only registers confirmed in the spec** | The API has **no raw-frame, register or probe endpoint**. It exposes named operations only. The send path refuses any frame whose `(reg, sub)` isn't in `CONFIRMED_WRITES` for the model. A test runs every operation on every model and checks this |
| **Never implement scene save (`71 35`)** | No operation builds it. The send path also checks every frame against a deny-list: SaveC1/SaveC2 (`0x7135`/`0x7136`), FactoryReset (`0x710b`) and FirmwareUpdate (`0x712e`). That is the `NEVER` set in `listen.py`, resolved by name through `vendor_commands.py`. A test asserts that each one is refused even when built by hand |
| **Only the DX5 II is confirmed; other models are read-only** | Capabilities come from `DEVICES[key]["status"]`. Anything not `"confirmed"` gets an empty write list. The agent has no `--unverified` and no config switch that grants it. A write to a D90 III returns `403 model_unverified` with the reason in plain words |
| **Input, output and mute: read-only for now** (decision 1) | They are absent from the write list for **every** model, including the DX5 II, until a hardware-confirmation ticket marks each one confirmed in code |
| **Scene recall: out of the web build for now** (Q15) | No `scene.recall` operation exists, and `71 11`/`71 12` aren't in `CONFIRMED_WRITES`. A later ticket confirms recall on the front panel, then adds the feature with decision 2's guard (the unlock, plus "Lower to −30 dB" after a loud result) |
| **Volume above −10 dB needs an explicit force** | A plan whose result is above −10 dB is refused unless the Send request carries `allow_loud: true` for that plan. The agent checks **both** the requested level and the level after quantising to the device's step. `cmd_vol` today checks only the requested `db`, which is safe with Python's `round` but should be explicit |
| **The unlock covers one change, re-locks after Send or Cancel, and times out after 60 s** (decision 3, Q5) | `allow_loud` lives on a single plan, not a session or a room. A plan is single-use and expires 60 s after it is created. Cancelling, expiring, sending, or a Send rejected with `409 changed` all discard it, and its unlock with it. The fast-step route refuses `allow_loud` outright |
| **High gain is treated like the unlock** (decision 8) | A plan that switches gain on is "loud" and needs `allow_loud`. Switching it off needs only the normal preview |
| **A preset that raises the preamp needs the unlock if volume + rise > −10 dB** (decision 12, Q7) | The agent computes `rise = new_preamp − last_written_preamp` and requires `allow_loud` if `volume + rise > −10`. If nothing was ever written, the rise is unknown and counts as loud. A preset that declares no preamp leaves `0x9c` alone (`cmd_apply`), so its largest positive band gain counts as the rise. The last-written value can be wrong if the vendor app changed the preamp since. The preview says the base is "last written by toppingctl" |
| **"Lower to −30 dB" sends at once** (design rule 5) | `POST /v1/volume` with `quieter_only: true` is accepted without a plan **only if** a read taken just before, in the same operation, shows the current level is louder than the target, and the target is at or below −10 dB. In step 4 the UI offers it whenever a read shows the room above −10 dB, for example after the knob was turned up. In step 6 it also appears after a loud wake (Q13) |
| **Undo of a volume change sends at once if it lands at or below −10 dB; it never goes back up to a loud level in one tap** (design rule 5) | Each result carries an `undo` token naming the previous level and the level just set. `POST /v1/volume {undo}` is accepted without a plan only if the target is ≤ −10 dB and a fresh read still shows the level that change set. Otherwise the undo becomes a normal previewed plan. An undo back above −10 dB is refused on this route. Preset and gain undo are always previewed |
| **+/− steps send at once in the safe range, from a value read this session** (decision 11, Q4) | `POST /v1/volume {target_db, expected_db}` without a plan reads the device **in the same operation, just before writing**, and sends only if: (a) target ≤ −10 dB, before and after quantising; (b) the target is one `volumeStep` from `expected_db`, compared in raw device units, not float dB; (c) `expected_db` equals that fresh read. Anything else gets `409` with the fresh state, and the UI falls back to the preview. The fresh read is what stops a "+" from a stale −30 dB jumping 30 dB after the knob went to −60 elsewhere. The step's response reports the level as "sent", and the next step's read confirms it |
| **Dry-run first** | (1) Every non-exempt write is two-phase. `POST /v1/plans` builds the frames and returns them, as `--dry-run` does, and only `POST /v1/plans/{id}/send` sends. Unlike today's CLI dry-run, which assumes 0.5 dB per step (`cmd_vol`), a plan reads `volumeStep` from the device, so the frames shown are the frames sent. (2) A new agent ships with `mode: dry-run`, where **every** sending route, including Send and all of `POST /v1/volume` (step, `quieter_only`, `undo`), returns the frames and sends nothing. A test covers each route. George switches the office host to `live` after watching one dry-run plan on the front panel (step 4) |
| **Only absolute targets** (task analysis) | The API has no relative volume. `target_db` is always absolute, so a retried request gives the same result |

Two things the agent **can't** enforce, stated so the UI doesn't imply it does:

- **The level at the ear.** The UAC2/ALSA stage that Home Assistant and snapclient drive sits in
  series with the DAC's volume (homelab `snapcast.md`). −10 dB on the DAC is a ceiling on one
  stage, not the listening level. Per Q12 the ALSA stage isn't shown, so the UI labels the
  figure **"DAC volume"**.
- **Writes from elsewhere.** The front panel, the IR remote, Home Assistant's IR blaster and the
  vendor app can all set a loud level, and the agent only sees it at its next read.

One gap is in this repo, not the agent: **`scenes.py recall` has no −10 dB guard and no
`--force` today.** Per Q16, ticket 1d closes it.

### The agent API

This is a sketch **(proposal)**. JSON throughout, and every response says where each value came
from.

| Route | Purpose |
| --- | --- |
| `GET /v1/room` | Identity: host, room, product string, `DEVICES` key, `status`, read path, capabilities, `mode` (dry-run or live), agent version |
| `GET /v1/state?fresh=1` | Current state, from the model's read path ("What the agent reads, per model"). Every field is `{value, source: "device" \| "written" \| "unknown", at}`. `fresh=1` forces a read. Without it, the cached read comes back with its age. A model with no read path returns identity and every field `unknown`, with the reason |
| `POST /v1/plans` | `{op, args, expected}`. The agent reads the device, applies the rules and builds the frames, then returns `{plan_id, summary, before, after_predicted, frames[], needs_unlock, expires_at}`. Nothing is sent |
| `POST /v1/plans/{id}/send` | `{allow_loud?}`. The agent re-reads. If any field **the operation depends on** differs from the plan's `before`, it returns `409 changed`, sends nothing, and discards the plan. By default that means volume, `volumeStep`, gain, output and power, for every operation. Volume is in the set for presets too, because of the preamp-rise rule. Fields such as `sampleRate` change with playback and are ignored. Otherwise it sends exactly the planned frames, reads again, and returns `{before, after, loud_result, undo}`, all under one device open and one lock |
| `POST /v1/plans/{id}/cancel` | Discards the plan, and its unlock with it |
| `POST /v1/volume` | Fast path only: a safe step, `quieter_only`, or a safe `undo`. Everything else is refused with a pointer to `/v1/plans` |

The operations are `volume.set`, `gain.set`, `preset.apply` and `preset.flat`, then
`power.set` (step 6). That is the whole write surface. `scene.recall` waits for its own ticket
(Q15).

**Latency (estimate, unmeasured):** with today's fixed 2 s read, a previewed change costs about
2 s for the plan, then about 4 s for Send (re-read, write, read-back), and a fast step about 2 s.
The early-stopping read (step 2b) is what makes the phone usable, once step 2 has measured
what it can stop on.

`toppingweb` mirrors these under `/api/rooms/{room}/…`, adds the signature, checks the response
signature, and passes answers through unchanged.

## Concurrency and stale reads

George is the only user (decision 6), but there are still several writers.

| Writer | What the plan does |
| --- | --- |
| George on two devices (phone and laptop tabs) | Compare-and-set. Every write carries `expected`, and the agent checks it against a fresh read. A mismatch returns `409` with the new state, and the UI says "Changed elsewhere: now −24.0 dB (read 0 s ago)". It doesn't resend |
| The CLI on the same host | An advisory `flock` on a fixed `device.lock`, taken **once per command or agent operation**, at the top. See "The device lock" below |
| Front panel, IR remote, Home Assistant's IR blaster | Invisible until the next read. The agent reads before every plan, every Send and every fast step. While a UI is open on a room, `toppingweb` polls `state?fresh=1` (proposal: every 10 s, one poller per room however many tabs are open). With no viewer, it doesn't poll. At 2 s per read, that polling holds the DAC about 20 % of the time, which is another reason for the early-stopping read |
| Vendor web app | The code already records that the vendor app takes the device and "only one client gets it" (`devstate.py:128–130`). The agent retries an open a few times, as `read_settings` does, then reports "In use by another app" and disables writes. The flock can't cover the vendor app, so between two of the agent's operations it can take the device. That is why every operation re-reads. If the DAC is plugged into another computer, the agent reports "No DAC found" |
| PEQ writes in progress | A preset is 110 band frames, 4 preamp frames and a commit. Under the per-device lock, nothing else interleaves. If the HID write fails partway, the result says "Preset partly written. Re-apply or apply Flat" and the preset record is set to `unknown`, not to the new name |

### The device lock

- **Who takes it.** A lock helper in `toppingctl.py` is taken by **every entry point that opens
  the device**, because the device grants one handle. That means:
  - `toppingctl.py`, `scenes.py`, `setctl.py`, `probe.py` and `smoke.py`;
  - `meters.py`, `listen.py`, and `readsettings.py`/`devstate.py`;
  - the agent.
- **Why per operation.** The lock is taken once per command or operation, not per open.
  `cmd_vol`, for example, reads `volumeStep` and closes before it opens the writer, and a
  per-open lock would let another process slip in between that read and the write.
- **Where it lives.** The path is fixed:
  - `/var/lib/toppingctl/device.lock` when that directory exists, which it does on every node
    and on any other agent host, because the agent's install creates it;
  - otherwise `~/.toppingctl/device.lock`, on a CLI-only machine such as a Mac.

  It doesn't follow `TOPPINGCTL_STATE_DIR`, so a CLI run under `sudo` without the wrapper still
  contends for the same lock. If the lock file can't be opened (for example, a user outside the
  `toppingd` group), the CLI refuses to write and says why. Reads warn and continue.
- **How long each side waits** **(proposal)**. The longest normal holders are:
  - `listen.py`, whose `--secs` defaults to 10 s and which holds the device for that long;
  - a preset Send, at about 4–5 s.

  So:
  - The **CLI** waits up to **15 s** for the lock, which covers a default `listen.py` run or a
    preset Send with margin. Then it exits with "The DAC on office is in use by toppingd (or
    another toppingctl command). Try again." A longer `listen.py --secs` simply makes the CLI
    give up, which is the right outcome.
  - The **agent** waits up to **2 s**, then returns `423 busy` naming the holder (the lock file
    records the holder's program name and PID). The room card shows "DAC busy · in use by the
    toppingctl CLI on office". The UI's response timeout (10 s) is longer than that wait, so a
    busy DAC never looks like an unreachable host.

### Stale state is labelled, never hidden

The agent never turns a cached value into a device value. Every field is one of:

- **read from device**, with its age;
- **last written by toppingctl**, which covers the PEQ preset (decision 9) and the preamp, which
  can't be read back;
- **unknown**.

`toppingweb` keeps the last answer from each agent in memory only so it can show "last known"
while a host is down, always marked as such.

**Where "last written" lives (Q8).** Today the CLI caches it in `~/.toppingctl/state.json` for
whoever ran it. That is root on the nodes, so the agent can't see it. Ticket 1b adds a
`TOPPINGCTL_STATE_DIR` environment override to `toppingctl.py`, and both the CLI wrapper and
the agent point it at `/var/lib/toppingctl`. A preset written from the CLI then shows in the UI
as "last written by toppingctl". `save_state` writes with the default umask, so a root CLI run
would leave a file the `toppingd` user can't rewrite. To prevent that, the directory is
`root:toppingd`, mode `2775`, and the CLI wrapper sets `umask 002`.

## Repo layout and language

This follows AGENTS.md: `toppingctl.py` stays a single-file CLI and the source of the frame
builders and guards. New entry points sit beside it, as `scenes.py` and `setctl.py` already do.

```
toppingctl.py            + plan_* functions that return frames or raise Refused, CONFIRMED_WRITES
                         + TOPPINGCTL_STATE_DIR override, + device lock helper
devstate.py              + read_settings on a passed-in handle; dx1_state without 0x1106 on request
scenes.py                + −10 dB guard and --force
toppingd.py              per-host agent: stdlib HTTP server, HMAC, policy, one HID worker
toppingd.yaml.example    agent config (YAML)
test_offline.py          + tests for plan_*, CONFIRMED_WRITES, the lock, the scenes.py guard
test_toppingd.py         agent API tests against a fake HID device, no hardware
docs/plans/              this plan
docs/ui/                 task analysis, prototype r1, prototype r2 (step 3)
web/
  pyproject.toml         uv project: starlette, uvicorn, authlib, itsdangerous, httpx, pyyaml
  src/toppingweb/        app, OIDC, room registry, agent client (signing)
  static/                the UI from the approved prototype: HTML, CSS, JS
  tests/
  rooms.yaml.example
  Dockerfile
```

- **Language:** Python in both parts. The agent has to be Python to reuse the frame builders.
  The web side matches plane-mobile's stack, so the sign-in design carries over.
- **Configuration is YAML:** `toppingd.yaml` and `rooms.yaml`. The one TOML file is
  `web/pyproject.toml`, which uv requires (house rule). It holds no ruff settings, so the
  existing root `ruff.toml` still governs lint. This PR doesn't move or change that file.
- **Lint and tests:** the existing gate (`ruff check`, `ruff format --check`, `python3 -m
  unittest test_offline`), plus `test_toppingd` on both CI interpreters and `web/` tests
  (pytest) on 3.12+. CI changes need a workflow edit, which George makes.

## Build order

Each step ends in something George can check. Stopping after step 3 is a real option, as 01-022
§6 says: it answers "what is each room doing?" with no write risk.

0. **Approve this plan.** Open the hardware-confirmation tickets for input, output and mute
   (decision 1). Merge PR #25 before ticket 1a starts.
1. **Step 1 is four tickets.** Each is safe to merge on its own, and in this order:
   - **1a. `plan_*` / `Refused` refactor and `CONFIRMED_WRITES`.** Pure functions such as
     `plan_volume(spec, db, step_db, allow_loud) -> list[Frame]` raise `Refused(reason)`
     instead of calling `sys.exit`. The `cmd_*` functions become thin wrappers that print and
     exit as they do now, so there is **one** −10 dB check, **one** unverified check and
     **one** frame builder for the CLI and the agent. `CONFIRMED_WRITES` is created from the
     spec's confirmed tables ("The confirmed register set"), and `Device.send` refuses anything
     outside it.
     - Tests: every `--dry-run` command in CI prints **byte-identical output** before and after.
     - A fake-device test answering `one_db`, because dry-run skips the live `volumeStep` read.
     - A test that every CLI command's frames fall inside `CONFIRMED_WRITES`.
     - One test per guard.
   - **1b. State dir and device lock across all entry points.** It covers the
     `TOPPINGCTL_STATE_DIR` override and the lock helper with its fixed path, taken by every
     entry point listed under "The device lock". The CLI waits 15 s and names the holder.
     - Tests: two processes contend for the lock.
     - The CLI's message names the holder.
     - A run under `sudo` without the wrapper still finds the same lock.
   - **1c. Shared-handle settings read.** `read_settings` takes a passed-in handle as an
     alternative to opening its own, so one operation (read, write, read-back) is one open.
     `dx1_state` gains a way to skip the `0x1106` dump, which the agent always uses.
     - Tests: a fake device counts opens.
     - The DX1 II reader never sends `11 06`.
   - **1d. `scenes.py` −10 dB guard** (Q16). A recall needs `--force`, because its result can't
     be known in advance. After the recall the script reads the state, as it already does. If
     the volume is above −10 dB it says so first and prints "Lower with: toppingctl vol -30".
     - Tests: a recall without `--force` is refused.
     - A loud result prints the warning.
2. **`toppingd`, read-only, `mode: dry-run`, on the office host.** It provides `/v1/room`,
   `/v1/state` (with the per-model read paths) and the HMAC check, with tests against a fake HID
   device. A homelab PR installs it on the office node. Measure on hardware and record in the
   spec or this repo:
   - whether the cluster can reach the office agent on 8740 through the router, and whether
     `office` resolves by name from a pod (both **unknown**);
   - how long a GetSettings read really takes, and exactly how many records or frames it returns
     on this firmware;
   - what reads return while the DAC is asleep;
   - what happens with the CLI and the agent in turn, and with the vendor app open on the same
     host;
   - what happens when the DAC is unplugged and replugged.
   - **2b. Early-stopping read.** Using the record count measured in step 2, the settings read
     returns as soon as the full set has arrived, and keeps the 2 s window as a ceiling.
     - Tests: a fake device that sends the full set returns early.
     - One that sends a short set still waits out the ceiling and reports the gap.
3. **`toppingweb`, read-only**, preceded by **prototype r2** (UI.md: the prototype comes before
   production UI). r2 adds:
   - fast +/− steps,
   - the 60 s unlock,
   - gain,
   - sleep and wake with preview and undo (decision 7: "in r2"),
   - the room states above, including "Identified, not read" and "DAC busy",
   - the "DAC volume" label,
   - and the "Changed elsewhere" message.

   r2 drops scene recall (Q15). George evaluates it. Then build sign-in, the room list and live
   state behind Authelia, and deploy via a homelab PR.
4. **Volume writes on the DX5 II in the office** (Q1): the fast path, plans, the unlock,
   "Lower to −30 dB" and undo. Switch the office host to `mode: live` only after watching a
   dry-run plan, then one live −1 dB step on the front panel, then `smoke.py` (the AGENTS.md
   sequence).
5. **Presets and gain**, each with its own front-panel check.
6. **Sleep and wake**, built from prototype r2, with preview and undo. It is blocked on the
   asleep-state measurement from step 2. Waking resumes the stored volume, so the agent reads
   after waking, and if the volume is above −10 dB the UI offers "Lower to −30 dB" first (Q13).

**Later tickets, not in this build:**

- scene recall: a front-panel confirmation of `71 11`/`71 12`, then the feature with decision 2's
  guard (Q15);
- input, output and mute: one hardware-confirmation ticket each (decision 1);
- a D90 III read path;
- confirming the DX1 II's `0x1106` read.

### Tests that prove the rules hold without the UI

All of these run offline in CI and call the agent API, not the UI:

- A write to an `unverified` or unknown model is refused for every operation.
- No sequence of API calls emits a frame outside `CONFIRMED_WRITES`. That includes:
  - SaveC1/SaveC2 (`71 35`, `71 36`), FactoryReset and FirmwareUpdate;
  - scene recall (`71 11`, `71 12`);
  - input (`71 04`) and output (`71 05`).
- The DX1 II reader never sends `11 06`, and never sends a readNack outside the vendor's
  read-safe list. The D90 III gets no frames at all.
- A volume above −10 dB is refused without `allow_loud`. It is also refused with `allow_loud`
  on the fast path, and with `allow_loud` on an expired (60 s), cancelled, already-sent or
  409-rejected plan.
- Rounding to the device step can't take a level that was accepted as "≤ −10 dB" above it,
  under either `volumeStep`.
- Gain on, and a preset whose rise crosses −10 dB, each need the unlock. That covers an unknown
  last-written preamp, and a preset with no preamp but positive band gain.
- A fast step is refused when `expected_db` doesn't match the read taken in the same operation,
  or when the step is bigger than one `volumeStep`. A fake device that "moves the knob" between
  two steps proves it.
- `quieter_only` is refused when the target isn't quieter than a fresh read. A fast `undo` is
  refused above −10 dB, or when the level has changed since the change it undoes.
- Send ignores changes to fields the operation doesn't depend on, such as `sampleRate`.
- When the CLI holds the lock, the agent answers `423 busy` within its 2 s wait, naming the
  holder.
- Signature failures, replayed nonces, skewed timestamps and timestamps from before the first
  NTP sync are rejected by the agent. `toppingweb` rejects any response whose nonce isn't the
  one it sent. The tests include a clock that starts in the past and then syncs, and a response
  replayed against a different request nonce.
- In `mode: dry-run`, no route sends a frame.
- Every `toppingweb` route except `/healthz` and `/auth/*` needs a session belonging to the
  allowed user, and writes need the CSRF header.

## Questions for George

All sixteen questions from the first draft are answered (see "Decisions"). There is one
remaining question, raised by the per-model read table:

17. **The D90 III in the living room: should the agent read it at all?** No read path is
    established for it. The DX5 II GetSettings returned no records on it (2026-08-28, before a
    framing fix), and on the DX1 II a readNack to the wrong register turned out to be a write.
    - (a) Identity only, from USB enumeration, sending no frames, until a ticket establishes a
      read path. *(proposal; what the plan says now)*
    - (b) Send the DX5 II GetSettings with this model's report-id framing, and show whatever
      comes back as "read from device". That would be new code: `devstate.read_settings` writes
      the unprefixed frame, which the D90 III drops (`Device._wire`), so `readsettings.py
      --device d90iii` doesn't do this today.
    - (c) Leave the living room out of `rooms.yaml` until it has a read path.

Still **(unknown)**, to be measured in step 2 rather than asked:

- whether the cluster VLAN can reach the IoT VLAN on 8740;
- whether the hosts resolve by name from the cluster;
- the GetSettings record count and timing;
- what reads return while the DAC is asleep;
- whether the vendor app and the agent can coexist on one host.
