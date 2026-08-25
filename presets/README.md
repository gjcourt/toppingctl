# Presets

Apply any of these with `./toppingctl.py apply presets/<file>`. Add `--dry-run` first to see
the frames without sending them.

## Headphone corrections

Each file is named `<model>-<source>-<target>.txt`. The target is in the name deliberately:
two presets for the same headphone against different curves are different presets, and one
should not silently shadow the other.

| File | Headphone | Target | Preamp |
|---|---|---|---|
| `zero-red-crinacle-harman-ie.txt` | Truthear x Crinacle Zero:RED | Harman IE 2019v2 | -3.2 dB |
| `dusk-oratory-harman-ie.txt` | Moondrop x Crinacle Blessing 2: Dusk | Harman IE 2019v2 | -1.5 dB |
| `quark-oratory-harman-ie.txt` | Moondrop Quark | Harman IE 2019v2 | -2.6 dB |
| `sundara-post2020pads-oratory-harman-oe.txt` | HiFiMan Sundara, post-2020 pads | Harman OE 2018 | -6.6 dB |
| `e3-oratory-harman-oe.txt` | Dan Clark Audio E3 | Harman OE 2018 | -1.0 dB |
| `e3-oratory-harman-8k.txt` | Dan Clark Audio E3 | Harman 8k | -1.5 dB |
| `e3-kuulokenurkka-autoeq.txt` | Dan Clark Audio E3 | see file | see file |

All are 10 filters, which is the device's real limit — band 11 accepts writes and drives
nothing. All use only PK / LSC / HSC, the three types this tool supports.

**The Sundara preset assumes STOCK pads, post-2020 revision.** Upstream publishes three separate
corrections - pre-2020 stock, post-2020 stock, and Dekoni sheepskin - and they differ. "Stock"
rules out the Dekoni file but does not by itself settle pre- versus post-2020.

**The planar bass boosts are large** — +8.9 dB on the Sundara, +6.4 dB on the Ananda. EQ corrects
frequency response, not distortion, and both rise in bass THD at high SPL. Keep levels moderate
rather than pushing the boost harder.

Do not lower the volume by hand to compensate for the preamp. `apply` writes it.

## Other

`bass1.json` and `bass1-plus-1k-probe.json` are shelf-tilt experiments, not corrections.
Neither declares a preamp, so `apply` warns and you should set one first.
