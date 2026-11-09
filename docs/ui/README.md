# UI docs

- [task-analysis.md](task-analysis.md): what the multi-room web UI has to do, and the design
  rules and open decisions behind it (TOPPINGCTL-1).
- [prototype-r1.html](prototype-r1.html): the first low-fidelity prototype. Kept unchanged.
- [prototype-r2.html](prototype-r2.html): the second prototype. Volume changes send without a
  preview, debounced; presets and sleep/wake keep their preview (TOPPINGCTL-10). Completed for
  the architecture plan's build step 3 with gain, the room states, the "DAC volume" label and a
  read after waking (TOPPINGCTL-12). George's answers to the #40 questions applied
  (TOPPINGCTL-14): the room page opens on DAC volume with gain, power and EQ preset under
  More; one unlock named "Allow high gain or above −10 dB for 60 s"; "Lower to −30 dB"
  re-reads and lowers instead of refusing; headphone gain only on models known to have a
  headphone output. No scene recall. Sample data only.
- [prototype-r2.test.js](prototype-r2.test.js): offline checks for r2, run with
  `node --test docs/ui/prototype-r2.test.js`. Not in CI, which runs Python only.

The prototypes are single files with no network requests. Open them in a browser.
