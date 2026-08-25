# Presets

Apply any of these with `./toppingctl.py apply presets/<file>`. Add `--dry-run` first to see
the frames without sending them.

## Headphone corrections

Named `<model>-<source>-<target>.txt`. Two corrections for the same headphone against different
curves are different presets and neither should silently shadow the other.

| File | Headphone | Target | Preamp |
| --- | --- | --- | --- |
| `zero-red-crinacle-harman-ie.txt` | Truthear x Crinacle Zero:RED | Harman IE 2019v2 | -3.3 dB |
| `dusk-oratory-harman-ie.txt` | Moondrop x Crinacle Blessing 2: Dusk | Harman IE 2019v2 | -1.6 dB |
| `quarks-oratory-harman-ie.txt` | Moondrop Quarks | Harman IE 2019v2 | -2.7 dB |
| `sundara-post2020pads-oratory-harman-oe.txt` | HiFiMan Sundara, post-2020 pads | Harman OE 2018 | -6.7 dB |
| `e3-oratory-harman-oe.txt` | Dan Clark Audio E3 | Harman OE 2018 | -1.1 dB |
| `e3-oratory-harman-8k.txt` | Dan Clark Audio E3 | Harman OE 2018, capped at 8 kHz (Topping Auto EQ) | -1.5 dB |
| `e3-kuulokenurkka-autoeq.txt` | Dan Clark Audio E3 | Harman OE 2018 (Kuulokenurkka measurement) | -1.7 dB |

All are 10 filters, which is the device's real limit -- band 11 accepts writes and drives nothing.
All use only PK / LSC / HSC, the three types this tool supports.

No Ananda preset: that unit is being sold.

### Preamps come from upstream's README, not its ParametricEQ.txt

AutoEq publishes two preamp figures per headphone and they differ by 0.1 dB. The value inside
`ParametricEQ.txt` is the less conservative of the two, and summing the actual biquad responses
shows it clipping by 0.01 to 0.05 dB on every one of these. The per-model README value is used
here instead, because a preamp that does not quite prevent clipping is worse than useless -- it
looks like the problem is handled.

### Don't EQ above ~8 kHz

Headphone measurements above roughly 8 kHz are dominated by ear-coupling artefacts specific to
the measurement rig rather than by the headphone. **Every AutoEq preset here carries an
`HSC Fc 10000 Hz` as filter 6**, and the Quarks additionally has a `PK Fc 10000 Hz`. Those
filters are correcting the fixture as much as the driver. If the top octave sounds wrong, that
is the first thing to disable.

### Two hardware caveats

**Check which pads are on the Sundara.** Upstream publishes three corrections -- pre-2020 stock,
post-2020 stock and Dekoni sheepskin -- and they differ substantially (post-2020 is -6.7 dB with
a +8.9 dB shelf; the other two are -5.2 dB with roughly +5.3 dB). The file here is post-2020.

**The Sundara boost is large.** +8.9 dB low shelf, and the preamp is computed over 20 Hz-20 kHz;
the shelf keeps rising below that. EQ corrects frequency response, not distortion, and planars
rise in bass THD at high SPL. Keep levels moderate rather than pushing the boost harder.

Do not lower the volume by hand to compensate for the preamp. `apply` writes it.

## Other

`bass1.json` and `bass1-plus-1k-probe.json` are shelf-tilt experiments, not corrections. Neither
declares a preamp, so `apply` warns and you should set one first.
