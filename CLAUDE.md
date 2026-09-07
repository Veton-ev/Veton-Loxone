# Veton-Loxone — Claude Code instructions

<!-- Template from the internal handbook's CLAUDE.md template. Keep every section; write
"none" rather than deleting one. Facts here must be checked against the code, not remembered.
Cross-cutting knowledge (fleet, runbooks, access, suppliers) lives in the internal handbook —
link, don't copy. -->

> **THIS REPOSITORY IS PUBLIC** (`github.com/Veton-ev/Veton-Loxone`, MIT).
> Nothing internal may be committed: no hostnames, no customer or site names, no fleet numbers,
> no charger IPs/UIDs, no Miniserver credentials or APPKEYs. The shipped project deliberately
> has the Modbus server IP **blank** and the document APPKEY **empty** — keep it that way.

## What this is
A ready-to-import **Loxone Config** integration for a Veton charger (Phoenix Contact CHARX)
over **Modbus/TCP `:502`**, surfaced through Loxone's native **Wallbox** block. Four shipped
artefacts plus two scripts (`tools/fix-loxone-project.py` and the offline harness
`tools/simulate-project.py`, see Run/test/build):
`Veton.Loxone` (a Loxone Config 16.x project file, XML: Modbus server + device with
the CHARX register map, inputs for energy/power/V/I/vehicle status/error/release mode/SOC
plus the last RFID card X275 (a String input wired to the Wallbox block's Uid input, since
2026-09-07), outputs for max current X301 (pause-aware since 2026-09-04: Wallbox target 0 → X301 = 0 =
pause, anything above → 6–80 A; re-sent every 60 s) plus the CHARX safety watchdog X306
(fallback current 6 A) and X307 (timer 300 s) — the X300 release and X303 lock writes were
dropped 2026-09-03, see Gotchas — a Wallbox block + visualisation page),
`Veton-2CP.Loxone` (the same project for a **two-charging-point** cabinet: one Modbus
device carrying CP1 on `1xxx` and CP2 on `2xxx`, with the Wallbox block, Vc/Cac formulas and
visualisation mirrored per point), `MB_Veton.xml` (a Loxone **Modbus device template**, the
format the official Loxone Library accepts, charging point 1) and `MB_Veton_CP2.xml` (the same
template for charging point 2, `2xxx`).
**Writes only take effect once the charger is in external-control mode** (internal CHARX
load management detached — see Gotchas); reads work regardless.
Consumers: Loxone installers/end customers and the
Loxone Library. It is the Loxone counterpart of `Veton-ev/HA-Veton`; the protocol reference
behind both is `Veton-ev/Veton-EMS-Integration`. **Not deployed.**
Handbook: the internal handbook's fleet section.

## Ownership
Owners and escalation: see `CLAUDE.local.md` (not committed) / the internal handbook.

## Run / test / build
```bash
# install: nothing to install here. Loxone Config is Windows-only; it is the only thing that
#          can open/validate Veton.Loxone for real (not possible on this Linux box).
# run locally: open Veton.Loxone in Loxone Config >= 16.0, set the Veton Modbus server
#          Address to the charger IP, save to a Miniserver. Template: Device templates ->
#          Import template -> MB_Veton.xml (filename MUST keep the MB_ prefix).
# test — there is NO test suite. The mechanical check is strict XML parsing of all four
#        artefacts (since 2026-09-03 the projects parse too — see Gotchas), same as CI:
python3 -c "import xml.etree.ElementTree as ET; [ET.parse(f) for f in ('MB_Veton.xml','MB_Veton_CP2.xml','Veton.Loxone','Veton-2CP.Loxone')]"
# build: none

# fix pass — the only executable in the repo:
python3 tools/fix-loxone-project.py Veton.Loxone Veton-2CP.Loxone   # apply + verify
python3 tools/fix-loxone-project.py --check Veton.Loxone            # verify only, no writes
# offline harness (graph health, formulas, X301 sweep, X275 -> Wallbox Uid wiring on canned
# registers); the sim-driven mode needs pymodbus + a CHARX simulator, absent on this box:
python3 tools/simulate-project.py --no-sim Veton.Loxone Veton-2CP.Loxone
```
`tools/fix-loxone-project.py` re-applies the thirteen hand fixes to the two `.Loxone` projects:
blank the document password salt (F1), Active Power X244 correction mW→kW (F2), Counter
Active Energy X250 `MaxVal` ceiling 10 000 → 100 000 000 (F3), drop the X300/X303 writes
(F4), clamp the X301 setpoint to 6–80 A and unwire `mono` so only `3 fase` drives it (F5),
add the X306/X307 watchdog (F6), audit **every** `ModbusASensor`'s `MinVal`/`MaxVal` against
the physically possible span of its register and widen the ones that cannot show it (F7 —
today that is only **Error Code X293**, a 32-bit *bitfield* where bit N = `2**(N-1)`, so
±10 000 hides every fault from bit 15 up; set to `0`/`4294967295`; a register marked
`"string"` in `REGISTER_SPAN` — X275 — is text and is skipped, neither widened nor reported
NOT AUDITED), wrap the X301 division in
`INT()` so the setpoint **truncates** instead of rounding up past the budget the Wallbox block
asked for (F8 — 7.4 kW was 10.68 A → 11 A ≈ 7.62 kW, now 10 A), and guard the single-phase
`mono` formula's divide-by-zero (F9 — the guard is
`IF(I2&gt;100;MIN(MAX(INT((I1*1000)/MAX(I2;100));6);80);6)`, with `>` XML-escaped like the
existing `IF(I1&gt;16944;…)`; F11 below wraps it into the stored final form; the divisor is
floored as well as guarded, because Loxone's `IF` laziness is undocumented; I2 is the
*measured* L1 voltage and reads 0 whenever the charger is unreachable, and 0 A on X301
withdraws the charging release),
remove the `12hTF` document attribute so the file is well-formed XML (F10 — Config writes
it back on every save, so this fix runs every time), make both setpoint formulas
**pause-aware** (F11 — outer `IF(I1&gt;0;…;0)`: Wallbox target 0 → X301 = 0 = pause, else the
F5/F8/F9 clamp; final stored forms
`IF(I1&gt;0;MIN(MAX(INT((I1*1000)/(400*1,732));6);80);0)` and
`IF(I1&gt;0;IF(I2&gt;100;MIN(MAX(INT((I1*1000)/MAX(I2;100));6);80);6);0)`), and set the X301
actuator's `RepeatRate` 3600 → **60** (F12 — re-asserts the setpoint every minute; some CHARX
firmware has been seen to drop an external X301 back to 0 after a controller-side restart or a
stray write, and the re-send restores it within one interval. Whether a 0 is re-sent too is
unconfirmed — the pause is written on change either way), and add the **last RFID card**
(F13 — a `ModbusASensor` on X275 per charging point, `ModbusDataType="101"` = Loxone String,
FC03, poll 5 s, plus an InputRef on the Wallbox page whose AQ is the only source of that
point's Wallbox block `<Co K="user">` = the **Uid** input; the block is found by its `power`
wiring, not its title; read-only, no new write — see the X275 gotcha).
F11/F12/F13 apply to both projects; the two templates carry F12 and the F13 input too
(hand-edited). `mono` stays
unwired, but README tells single-phase
installers to swap to it, so it has to be safe first. It matches on stable attribute patterns
(`ModbusAddress=`, `Title=`, `Formula=`, `Ref=`, `Co K=`) — never on line numbers — preserves
BOM + CRLF, and **is idempotent**: every fix detects "already applied" and skips, so a second
run rewrites nothing (byte-identical). It then self-verifies (BOM, line endings, unique `U=`,
no dangling `<In Input>`/`Ref=`, `<C>`/`</C>` balance, strict XML well-formedness *and* the
absence of `12hTF`, salt and `APPKEY`/`Address`/`Serial` blank, expected `ModbusAddress` set, no overlapping
page rectangles, every sensor range wide enough for its register, both setpoint formulas
in their final pause-aware/clamped/truncated/guarded form, X301 `RepeatRate="60"`, and per
charging point an X275 String/FC03 sensor whose InputRef is the sole feed of that point's
Wallbox `user` input, one Wallbox block per point, no actuator on X275/X308).
The register spans F7 audits live in `REGISTER_SPAN` and are sourced from
`charx-doctor/references/modbus-register-map.yaml` + `error-codes.yaml`; a sensor whose
register is not in that table is reported as **NOT AUDITED** rather than silently passed —
add the register there when you add a sensor.
**Re-run it after any re-save from Loxone Config** — Config re-embeds the document APPKEY,
the Modbus server IP and the password salt, and reverts the hand fixes. The two templates
are *not* touched by it; they were brought into line separately (2026-09-03).
CI (since 2026-08-26): `.github/workflows/ci.yml`, job id `ci`, every push/PR — **secret scan
only** (`gitleaks git --config .gitleaks.toml --exit-code 1 --log-opts=--all .`, whole history;
public repo) **plus** `xmllint --noout` on **all four artefacts** (`MB_Veton.xml`,
`MB_Veton_CP2.xml`, `Veton.Loxone`, `Veton-2CP.Loxone` — extended 2026-09-03, once the
projects became well-formed). The
blank-`APPKEY`/`Address` grep is NOT in CI — run it yourself before pushing. Locally:
`git config core.hooksPath .githooks` once per clone enables
the same scan as a pre-commit hook; `.claude/settings.json` + `.claude/hooks/guard.sh` fence
agents off `git push` to main and force pushes. Branch protection (require `ci`) is a GitHub
setting still to be clicked.

## Deploy
**Not deployed.** Not in the 2026-08-26 deploy survey (internal handbook). Publishing = merging to
`main` (GitHub) and, separately, a manual submission of `MB_Veton.xml` to the Loxone Library
(curated by Loxone, no automation). Rollback = revert the commit.
Blast radius of a bad change: Loxone customers import wrong register numbers / an unsafe write
into their Miniserver — nothing on the Veton fleet or cloud changes.
Path to `main`: PR with the `ci` check green (secret scan); agents cannot push to `main` directly.

## Talks to (seams)
- **CHARX Modbus register map** — hard-coded **four times** here (`Veton.Loxone` and
  `Veton-2CP.Loxone` device `ModbusAddress="1xxx"`/`"2xxx"` attributes, plus `MB_Veton.xml` and
  `MB_Veton_CP2.xml` `ModbusAddress`), charging point 1 = `1xxx`, point 2 = `2xxx`, others at
  `connector × 1000`. X275 (last RFID card) is in all four since 2026-09-07; the sibling HA
  integration reads it too. Same map in `Veton-EMS-Integration/docs/modbus.md`,
  `vetonlm/registers`, `savings-collector`, `charx-doctor/references`, `veton-ha` and the
  internal charger-agent repos. Change a register → change **all four files here** and check
  the other copies.
- **`Veton-ev/Veton-EMS-Integration`** is the documented reference and states the integration
  policy; **`Veton-ev/HA-Veton`** is the sibling integration. Keep the three consistent.
See the internal handbook's seams page.

## Gotchas (this repo only)
- **Four copies of the register map.** `Veton.Loxone` and `MB_Veton.xml` were authored
  separately by hand (commits `73d533f`, `bcda557`); `Veton-2CP.Loxone` and `MB_Veton_CP2.xml`
  are generated from those two (see the generator gotcha below). Nothing checks that the four
  agree. Diff all of them after any register change.
- **Re-saving from Loxone Config re-embeds secrets.** A project saved against a real
  Miniserver carries the document `APPKEY` and the Modbus server IP. The shipped file has
  `APPKEY=""` and `Address=""` on purpose (README "Notes"). Before committing a re-exported
  `Veton.Loxone`, grep both back to empty — this is a public repo.
- **X300 charging release / X303 locking: RESOLVED 2026-09-03 — the writes were dropped.**
  History: this project used to write X300 (and X303), whereas `Veton-EMS-Integration`
  (commit `e7b3555`) documents that an EMS must leave release to OCPP and only cap X301.
  That inconsistency was raised with the owner, who **decided to drop the writes** — from
  both `.Loxone` projects (`tools/fix-loxone-project.py` F4) and from both templates. Two
  reasons: (a) policy — Veton chargers ship with release mode = OCPP, and driving X300 breaks
  authorization, session/transaction records and app/backend visibility; (b) they never
  worked — both were emitted as `ModbusCmd="5"` (FC05 Write Single Coil) and a CHARX exposes
  **holding registers only** (FC03/FC06), so every such write was rejected. The knowledge is
  kept in README "Why X300 / X303 are not written", including that a standalone **non-OCPP**
  charger may add them by hand with **FC06 (`ModbusCmd="6"`), never FC05**. Do not re-add
  them to the shipped artefacts without a new owner decision.
- **Internal LM overwrites X301.** A Veton charger ships with the CHARX **internal load
  management active**: the charging points sit in a CHARX load circuit and the internal
  manager rewrites X301 every cycle. In that state every read works (power, energy, Vc/Cac,
  errors) but every Loxone X301 write — the pause included — is overwritten within seconds,
  and "the Wallbox block does nothing" is the symptom. Loxone only controls current once the
  charger is in **external-control mode** (points detached from the load circuit; on a
  Veton-managed charger that is done by Veton via app/backend, never from Loxone). README
  "Who controls the current" + Requirements say so; keep them. Corollaries: in that mode the
  X306/X307 watchdog is the only protection when the Miniserver stops (never drop F6), and
  two writers (Loxone + Veton solar/smart modes, or any second EMS) fight and leave cars
  paused. Source: Veton's own charger agents (which detach the load circuit before writing
  X301) and earlier live measurements — not re-tested from this repo (2026-09-04).
- **X301 = 0 is the deliberate pause; never let any other path write 0.** Since 2026-09-04
  (F11) the formulas write X301 = 0 exactly when the Wallbox block's target power is 0
  (paused / shed / Off / no charging mode); on a CHARX that withdraws the charging release
  and the car pauses, while OCPP keeps the session. Every other path stays ≥ 6 A by design:
  the mono voltage guard (F9) falls back to 6, sub-6 A requests floor at 6 (F5), the
  watchdog fallback X306 is 6. A stray 0 from any other source looks identical to a pause
  and is very hard to see from Loxone. When touching a formula, keep the outer `IF(I1&gt;0;…;0)`
  as the **only** producer of 0 and re-run `--check`. **Corollary: no charging mode selected,
  or the block's *Mode after unplugging vehicle* parameter = Off, now means a paused car** —
  the block outputs target 0 in that state, the project ships with no mode selected, and the
  block falls back to that parameter after anything it treats as an unplug (including an
  unreachable charger: Vehicle status reads 0, Vc drops, session ends). README Install step 6
  requires the setting; keep that step. Known caveats documented in README
  "Pause semantics" (sleeping EVs need unplug/replug, watchdog resumes at 6 A after 300 s
  when the Miniserver dies mid-pause, CHARX 1.9.1 opens the contactor on a sub-6 A setpoint
  during ISO 15118 — README gives the opt-out: drop the outer `IF(I1&gt;0;…;0)`, and `--check`
  then reports "not pause-aware", expected there). F12 (`RepeatRate="60"`) exists because some
  firmware has been seen to drop an external X301 back to 0 after a controller-side restart
  or a stray write — do not raise it back to 3600.
- **`12hTF`: RESOLVED 2026-09-03 — the attribute was removed and all four artefacts now
  parse strictly.** History: line 3 of both `.Loxone` projects carried a document attribute
  `12hTF="true"` — a 12-hour-clock display setting whose name starts with a digit, which XML
  forbids. Loxone Config accepts it; `xml.etree`, `xmllint` and every other strict parser
  rejected the whole file ("not well-formed (invalid token): line 3, column 559"), so the two
  projects could only be checked on an in-memory copy with the attribute renamed. The owner
  decided on **2026-09-03** to drop it: it buys nothing (a 12-hour clock is wrong on a
  `Ctry="BE"` project anyway) and it cost every mechanical check the projects could have had.
  **All four artefacts — `Veton.Loxone`, `Veton-2CP.Loxone`, `MB_Veton.xml`,
  `MB_Veton_CP2.xml` — now parse with a strict XML parser**, and CI runs `xmllint --noout`
  over all four. `tools/fix-loxone-project.py` **F10 re-removes the attribute**: Loxone Config
  writes it back on save, so re-run the fix pass after every re-export (see Run/test/build).
  Still do not run these files through an XML formatter/pretty-printer — the BOM, the CRLF
  line endings and the one-element-per-line layout matter (next gotcha).
- **All four artefacts are UTF-8 with BOM and CRLF line endings** (Windows-authored). Edit
  them with a byte-level script (read `rb`, split on `\r\n`), never with `sed`/editors that
  normalise line endings. `MB_Veton.xml`
  declares `minVersion="16000610"` (Config 16.0.6.10+). Editors that strip the BOM, convert to
  LF, or pretty-print may break the Loxone import; the only real validation is Loxone Config,
  which does not run on this Linux box.
- **Charging points 1 and 2 are covered; 3 and up are not.** `Veton.Loxone` / `MB_Veton.xml`
  cover CP1 (`1xxx`); `Veton-2CP.Loxone` / `MB_Veton_CP2.xml` add CP2 (`2xxx`). A third point
  and beyond is still a manual duplicate-and-offset procedure (README "Multiple charging
  points").
- **The CP2 artefacts are machine-generated, but the generator is not in the repo.**
  `Veton-2CP.Loxone` and `MB_Veton_CP2.xml` were produced from the single-point files by a
  script that exists only in a session scratchpad outside the repo (throwaway). It is a
  **known gap**: nothing in the repo can
  regenerate CP2, so today a register change means editing the CP2 files by hand (or writing
  the generator again). Committing that script is the fix.
- **`MinVal`/`MaxVal` cannot be expressed in a Modbus device template.** They are valid on
  a `<C Type="Modbus*Sensor">` inside a `.Loxone` project, but they are **not** template
  attributes — evidence: Loxone's own shipped template
  `C:\ProgramData\Loxone\Loxone Config 17.0.3.31\Templates\Comm\MB_Froeling.xml` uses
  none anywhere. So the three validity-range fixes exist only in the projects (X250 `MaxVal`
  10 000 → 100 000 000; X299 0 → 65535; X293 0 → 4294967295) and are **README instructions**
  for template users
  ("After importing the template"). Do not invent those attributes in `MB_Veton*.xml` —
  Loxone Config would reject or ignore the import.
- **X275 (last RFID card) is sticky, X308 is inert, and the String register count is
  unverified in Config.** X275 holds the UID of the card *last presented* as ASCII (10
  holding registers on the charger, big-endian, NUL-padded; 14 hex characters for a 7-byte
  card) and keeps it until the next tap — verified on production chargers in **OCPP release
  mode** (firmware 1.7.3) against the charger's own RFID events; Modbus, Always and other
  release modes and firmware 1.9.x were **not** measured (a unit in Always mode read empty
  because no card had been presented). X275 only *changes* when a **different** card is
  tapped — the same card twice produces no change, so a single-driver household shows a
  constant Uid after the first tap; whether the Wallbox block attributes each session from
  the standing Uid (sampled at session start) or only reacts to a Uid change is unverified
  on the Loxone side (README gives the two-session/two-card recipe). *If* the block samples
  Uid: at plug-in it first sees the *previous* card and only reassigns the session on the
  next tap (and only to a Loxone user with a *User ID*); an app/remote start without a tap
  inherits the previous card, and the block's cost outputs follow the Uid. X308 ("reset last
  RFID", write > 0) was **measured inert on firmware 1.7.3** (acknowledged, X275 unchanged;
  not re-tested on 1.9.x) — so it is **deliberately not written**, and `--check` fails on
  any actuator at X275/X308. Loxone's attribute vocabulary has **no string-length
  attribute**, so how many registers Config reads for `ModbusDataType="101"` is
  **unverified**: the simulator's 10-word decode is what the charger exposes, not what
  Loxone reads; a read longer than 10 spills into X285–X294 (extra characters after the
  UID, or the input offline on a Modbus exception). **F13's sensor shape is cloned, not
  designed:** `MinVal/MaxVal/MinChange/MinTime/MaxTime/SourceValHigh/DestValHigh`, `ValOT`,
  the InputRef's `Analog="true"`/`LinkRefType="153"` and `Display Unit="<v>"` are copied
  from the analog X299 sensor for *shape only*; their effect on a String input is
  unverified (worst case: a numeric `MinChange` parse suppresses updates between similar
  UIDs). The right fix is to create one String Modbus input in Loxone Config, save, diff
  what Config writes, and adopt that shape in F13. Config checklist (also in README
  "confirm in Loxone Config"): String connector shown as text; `user` wire survives
  save/reload; value shows the full UID; two similar cards both propagate; the same card
  twice attributes both sessions. Do: keep the input read-only, keep the README note, tell
  users to set *User ID* to what the input actually shows, keep the privacy note (the UID
  is personal data and, with a whitelist, the credential). Don't: add an X308 write, invent
  a length attribute, claim the 14 characters are verified on the Loxone side, claim X275
  is populated regardless of release mode, or "fix" the F7 `"string"` marker into a numeric range.
- `docs/images/wallbox-preview.svg` is a **mockup**, not a screenshot (README says so). Do not
  present it as the real Loxone UI.
- `Veton.Loxone` is 130 KB of generated XML with GUIDs — review diffs semantically (which
  `Address`/`Title` changed), not line by line.

## Agent rules
- Work on a branch; open a PR; never push to `main` (to be enforced by `.claude/settings.json`
  + `.claude/hooks/guard.sh` and by branch protection — HANDOVER-PLAN Phase 4/5).
- Never run deploy/ssh-to-production commands; there is nothing to deploy from this repo.
- **Public repo:** never commit secrets, Miniserver APPKEYs, charger IPs, customer names or
  fleet numbers; `gitleaks` pre-commit + CI will fail the commit/PR once Phase 4 lands. The
  pre-commit hook also refuses staged content carrying internal names, personal paths or
  session-scratchpad paths. No secrets belong to this repo; the internal handbook's access
  section says where fleet secrets live, never the value.
- Write durable lessons to the internal handbook's lessons folder (one file per lesson),
  not to personal memory.
