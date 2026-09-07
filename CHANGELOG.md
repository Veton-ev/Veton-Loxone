# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

This repository has never been tagged with a version number, so entries are
dated instead of numbered.

## 2026-09-07

### Added

- **The last RFID card is read and fed to the Wallbox block's Uid input**, so a
  charging session is attributed to the card holder in the block's charge log.
  A new *Last RFID card* input reads CHARX `X275` (the UID of the card last
  presented, ASCII text, 10 holding registers on the charger) every 5 s as a
  Loxone **String** (`ModbusDataType="101"`, FC03 — nothing new is written) in
  both projects (`1275` / `2275`, one per charging point, on that point's own
  Wallbox block) and both templates. `tools/fix-loxone-project.py` applies and
  verifies it as **F13**; `tools/simulate-project.py` checks the sensor → InputRef
  → Wallbox `user` wiring and pushes canned registers through it offline.
  Caveats, all in README [*RFID card → Wallbox Uid*](README.md#rfid-card--wallbox-uid-user-attribution):
  each Loxone user's *User ID* must equal the card UID as the input shows it;
  `X275` is **sticky** (the previous card stays until the next tap, so an
  app/remote start inherits it); the `X308` reset register was measured inert
  on the current firmware and is deliberately not written; and how many
  registers Loxone reads for a String is undocumented — confirm in Loxone
  Config that the full 14-character UID shows.

## 2026-09-04

### Fixed

- **A pause from the Wallbox block is now a real pause.** Since 2026-09-03 the
  setpoint formulas clamped every request to a 6 A floor, so when the block set
  its target power to 0 — session paused, load shedding, block *Off*, no mode
  allowing charging — the car kept charging at 6 A. Both formulas now pass a
  0 kW request through as **`X301 = 0`**, which on a CHARX withdraws the
  charging release and pauses the car; any request above 0 kW behaves as
  before (converted, truncated, clamped to 6–80 A, never above the block's
  budget). The pause does not touch OCPP: the session stays authorised and
  recorded, the cap is simply zero. Read the README section
  [*Pause semantics*](README.md#pause-semantics-what-a-0-kw-request-does)
  before relying on it — some EVs sleep through a long pause and need an
  unplug/replug, a dead Miniserver resumes the car at 6 A via the watchdog, and
  CHARX firmware **1.9.1** has a known issue with sub-6 A setpoints during an
  ISO 15118 session.
  `3 fase`: `IF(I1>0;MIN(MAX(INT((I1*1000)/(400*1,732));6);80);0)` —
  `mono`: `IF(I1>0;IF(I2>100;MIN(MAX(INT((I1*1000)/MAX(I2;100));6);80);6);0)`.
- **The `X301` setpoint is re-sent every 60 s instead of every hour**
  (`RepeatRate` 3600 → 60 on the *Max Charging Rate I* actuator, in both
  projects and both templates). Some CHARX firmware has been seen to drop an
  externally written `X301` back to 0 after a controller-side restart or a
  stray write; a client that re-sends its setpoint restores it within one
  `RepeatRate` interval, one that writes once an hour leaves the car at 0 for
  up to an hour.
- `tools/fix-loxone-project.py` applies and verifies both: the pause-aware
  formulas (F11) and the 60 s `RepeatRate` on X301 (F12).

### Changed

- **The README now states a requirement that was missing: the charger must be
  in external-control mode** for anything Loxone writes to `X301` to take
  effect. As shipped, a Veton charger's own CHARX load management is active and
  rewrites `X301` on every cycle, so reads work but every write — the pause
  included — is overwritten within seconds. Detaching the charging points from
  the CHARX load circuit is done by Veton (app or backend), not from Loxone.
  Once it is done the X306/X307 watchdog is the only protection left when the
  Miniserver stops, and no second energy manager (Veton solar/smart modes, a
  second EMS) may run on the same charger at the same time. See
  [*Who controls the current*](README.md#who-controls-the-current-internal-load-management-vs-loxone).
- **The README now requires a charging mode to be set.** Since a 0 kW request
  is a real pause, a Wallbox block with **no charging mode selected**, or with
  *Mode after unplugging vehicle* left at *Off*, asks for 0 kW and keeps the
  car paused — a fresh import in external-control mode writes `X301 = 0` and
  the car never starts, and the same happens after anything the block treats
  as an unplug (including the charger being unreachable for a while: `Vc`
  drops, the session ends, the block falls to its mode-after-unplug). The
  README adds an Install step (select a mode in the app; set *Mode after
  unplugging vehicle* to a charging mode, e.g. *Max. charging power*) and lists
  this as the #1 reason a car will not charge. It also notes that the block's
  `allow` input ships inverted and unwired (unconnected = charging allowed).
- The README's explanation of `RepeatRate` no longer claims Loxone only
  re-sends values greater than 0 — that condition belongs to *virtual*
  outputs. It now quotes Loxone Config 17's help text for the Modbus output
  repeat property ("Interval in seconds, at which the output value is sent
  again") and says plainly that whether a value of 0 is also re-sent has not
  been confirmed on a Miniserver. Either way the pause is written once on
  change, which is what pauses the car; the 60 s re-send matters for the
  non-zero setpoints.

## 2026-09-03

The first entry in this changelog — it covers everything that changed since the
project was first published, not just one day's work.

### Fixed

- **The Wallbox block's `Vc` (Vehicle connected) input had no signal at all.**
  This is what the Loxone Library rejected the submission over: the charger was
  wired up, but the block never learned that a car was plugged in, so it could
  not show a session or run its charging logic. `Vc` is now derived from the
  CHARX vehicle-status register X299 through a small Formula block
  (`IF(I1>16944;IF(I1<17459;1;0);0)` — true for IEC 61851 states B, C and D).
  **`Cac` (Charging active)** is wired the same way, true for C and D only.
  Both stay off for "no vehicle" and for the fault states E0 / F0 / IN.
- **Charging power was displayed 1000× too high.** X244 reports milliwatts and
  the input scaled them to watts, but the Wallbox block's power input expects
  **kilowatts** — so a 7.4 kW session showed up as 7400 in the app. The
  correction is now mW → kW, and the unit on the input says `kW`.
- **The energy counter stopped reporting past 10 000 kWh.** *Counter Active
  Energy* (X250) is a **lifetime** kWh total, but the input kept Loxone's
  default validity range of ±10000. Anything outside that range is discarded,
  so the counter simply went quiet once the charger passed 10 MWh. The charger
  on the bench already reads over **12 000 kWh**, so the old ceiling is not a
  distant-future problem. The range is now 0 … 100 000 000.
- **The fault display showed a wrong number while the charger was in a fault.**
  X293 is a **32-bit error bitfield** (bit *N* = 2^(N-1)), not a fault number,
  so a single bit from 15 upward is already past the same ±10000 default — and
  a bit-22 fault is over 2 million. Out-of-range values were dropped, so the
  fault display stayed clean while the charger was in error. The range is now
  0 … 4 294 967 295.
- **Two formulas were driving the max-current setpoint at once.** Both the
  single-phase `mono` block and the three-phase `3 fase` block were connected
  to *Max Charging Rate I* (X301), writing conflicting currents into the same
  register, last writer wins. Only `3 fase` is connected now; `mono` is still
  on the Program page for single-phase sites to swap in (see the README).
- **The setpoint could be written as 0 A, which withdraws the charging
  release** on a CHARX — the car stops instead of dropping to a low current.
  Both formulas now clamp to the documented **6–80 A** range, so a 0 kW request
  from the Wallbox block lands on the 6 A minimum (superseded on 2026-09-04:
  0 kW now pauses, see above).
- **The setpoint now truncates instead of rounding.** X301 is a whole-ampere
  register; rounding up asked the charger for more than the Wallbox block had
  budgeted (7.4 kW → 10.68 A → 11 A ≈ 7.62 kW, real current on a real fuse).
  The division is wrapped in `INT()`, so Loxone never asks for more current
  than the block allocated.
- **The single-phase `mono` formula divided by the measured L1 voltage**, which
  reads **0** whenever the charger is unreachable, the first poll has not
  landed, or the contactor is open — and dividing by that produced the 0 A
  write described above. It now falls back to 6 A unless the L1 reading is
  above 100 V.

### Removed

- **The X300 charging-release and X303 locking writes** are gone from both
  projects and both templates, for two independent reasons:
  1. **They never worked.** Both were issued as `ModbusCmd="5"` — *FC05 Write
     Single Coil*. A CHARX exposes **holding registers only** (FC03 read /
     FC06 write); it has no coils, so every one of those writes was rejected.
  2. **Driving X300 is wrong on a Veton charger.** Veton chargers ship with
     release mode = **OCPP**: the backend decides *whether* a car may charge
     (authorization, start/stop, session records, app visibility), and an
     energy manager only decides *how fast*. Taking release away from OCPP
     breaks authorization, session/transaction records and app visibility.
     Capping current via **X301 works in every release mode**, which is what
     the project does.

  If you run a **standalone, non-OCPP** charger (no backend, release mode =
  `Modbus`) and legitimately need these registers, the README section
  [*Why X300 / X303 are not written*](README.md#why-x300--x303-are-not-written)
  tells you how to add them back by hand — as **FC06 Write Single Register
  (`ModbusCmd="6"`), never FC05 Write Single Coil**.
- **The `12hTF` document attribute** (a 12-hour-clock display setting) has been
  dropped from both `.Loxone` projects. Its name starts with a digit, which XML
  forbids, so the project files could not be read by any strict XML parser.
  Removing it means the shipped files can now be checked by ordinary tooling;
  the clock format falls back to Loxone's default, which is the right one for a
  Belgian project anyway.

### Added

- **`Veton-2CP.Loxone`** — a ready-made **two-charging-point** variant of the
  project. One CHARX controller is a single Modbus TCP endpoint, so both points
  live in one Modbus device (CP1 on `1xxx`, CP2 on `2xxx`), with the Wallbox
  block, the Vc/Cac formulas, the setpoint formulas and the visualization
  mirrored per point.
- **`MB_Veton_CP2.xml`** — the Modbus device template for **charging point 2**
  (`2xxx`), to import alongside `MB_Veton.xml` on a two-point cabinet.
- **The CHARX safety watchdog** (X306 fallback current **6 A**, X307 timer
  **300 s**), in both projects and both templates. In plain terms: if the
  Miniserver dies or the network drops in the middle of a session, the charger
  no longer holds whatever current was last written — after 300 seconds without
  a refresh it falls back to 6 A. That is the IEC 61851 minimum, so a dead
  Miniserver **slows the car down instead of cutting it off**. The timer is fed
  by the actuator's `RepeatRate` (60 s, five refreshes of margin inside the
  300 s window); keep `RepeatRate` well under the timer value or the watchdog
  will fire during normal operation.
- **`tools/fix-loxone-project.py`** — a maintenance script that re-applies all
  of the fixes above to the two `.Loxone` project files and then verifies them.
  **Re-run it after any save from Loxone Config**: saving re-embeds the
  Miniserver address, the document APPKEY and the password salt, reverts the
  hand fixes, and writes the `12hTF` attribute back.
- **`tools/simulate-project.py`** — an offline test harness that parses a
  project, checks the block wiring, and replays the Modbus decoding, value
  corrections, validity ranges and Formula arithmetic against real register
  data, so the numbers can be checked without a Miniserver.

### Changed

- **Vehicle status (X299) is polled every 5 s instead of every 60 s** in both
  templates and both projects, so `Vc` / `Cac` — and with them the Wallbox
  block's session — react to plugging in within seconds rather than up to a
  minute.
- **The README now documents the manual steps required after importing a
  template.** A Loxone Modbus *device template* cannot carry an input's
  validity range (`MinVal` / `MaxVal` are not template attributes), so the
  three ranges above — *Counter Active Energy*, *Vehicle status*, *Error Code*
  — have to be widened by hand, once, in Loxone Config after adding the device.
  See [*After importing the template*](README.md#after-importing-the-template-required).
  The full `.Loxone` projects already ship with all three applied.
- The shipped project files no longer carry anything from the machine they were
  authored on: the Miniserver serial, the admin password hash and the document
  password salt are blank, alongside the Modbus server address and the APPKEY
  that were already blank by design. Set your own on import.

## Upgrading from an earlier copy of this project

If you already deployed an older copy of `Veton.Loxone` and would rather patch
it than re-import, check these three things — they are the ones that were
silently wrong:

1. **The three validity ranges.** In the periphery tree, select each input →
   *Properties* → *Minimum value* / *Maximum value*:
   *Counter Active Energy* (X250) → `0` … `100000000`,
   *Vehicle status* (X299) → `0` … `65535`,
   *Error Code* (X293) → `0` … `4294967295`.
   Without these the energy counter stops past 10 000 kWh, `Vc` / `Cac` never
   go true, and faults do not show.
2. **The setpoint wiring.** Open the Program page and confirm that only **one**
   formula block is connected to *Max Charging Rate I* — `3 fase` on a
   three-phase supply, `mono` on a single-phase one, never both. While you are
   there, check that the formula pauses on 0, clamps and truncates:
   `IF(I1>0;MIN(MAX(INT((I1*1000)/(400*1,732));6);80);0)`
   (single-phase: `IF(I1>0;IF(I2>100;MIN(MAX(INT((I1*1000)/MAX(I2;100));6);80);6);0)`),
   and that the *Max Charging Rate I* actuator's `RepeatRate` is **60**, not
   3600. A copy from 2026-09-03 has the clamp but not the pause: a 0 kW
   request from the Wallbox block lands on 6 A instead of pausing the car.
3. **X300 / X303 never worked.** If your copy still has *Charging Release* or
   *Locking* actuators, they were sending FC05 write-coil requests that the
   CHARX rejected — nothing you observed was caused by them, and nothing breaks
   when you delete them. Do not "fix" them into FC06 writes on a charger that
   talks to the Veton backend; see the section above.

Also check the *Active Power* input: it should scale mW → **kW**
(`SourceValHigh="1000000"`, `DestValHigh="1"`) and display `kW`. If your power
reading is a factor of 1000 out, that is this.

## What has and has not been tested

Being explicit, because none of the above has been through a Miniserver:

- **Verified against a real Veton charger** (read-only Modbus): the register
  semantics used here, and the 12 182 kWh lifetime energy reading that showed
  the old 10 000 kWh ceiling was already being exceeded.
- **Taken from Veton's own charger software and earlier measurements, not
  re-tested here**: that the CHARX internal load management overwrites an
  external `X301` write within seconds while it is active, that `X301 = 0`
  withdraws the charging release (Veton's own agent pauses cars exactly this
  way), and that some firmware has been seen to drop an externally written
  `X301` back to 0 after a controller-side restart or a stray write — the
  reason `X301` is now re-sent every 60 s. No Modbus write was issued from
  this repository to any charger.
- **Verified offline** (2026-09-04): only the static half of
  `tools/simulate-project.py` ran (`--no-sim`): block wiring and the formula
  sweep, including the 0 kW → 0 A pause path — plus strict XML validation of
  all four shipped files. The live-register replay (Modbus decoding, value
  corrections, validity ranges against real register data) needs `pymodbus`,
  which was not available, so it did not run this time.
- **Not verified**: neither project has been opened in Loxone Config or loaded
  onto a Miniserver — still true on 2026-09-04. The pause path (target power
  0 → formula → 0 A on the wire, and the 60 s re-send) has only been evaluated
  offline; it has not been observed end to end. Whether a Modbus output with a
  value of **0** is re-sent at `RepeatRate` at all is unconfirmed (Loxone's help
  only says the value is sent again at the interval). A Formula block with no
  wired inputs is **assumed** to emit its constant — that is what feeds
  X306/X307 in the projects. Loxone Config is the only thing that can truly
  validate these files, and it does not run on the machine they were prepared
  on. Treat the first import as the real test, and please report what you
  find.
