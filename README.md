# Veton EV Charger — Loxone integration

A ready-to-import **Loxone Config** project that integrates a **Veton EV charger**
(Phoenix Contact **CHARX** controller) into Loxone over **Modbus/TCP**, surfaced
through Loxone's native **Wallbox** block.

This is the Loxone counterpart of the [Home Assistant integration](https://github.com/Veton-ev/HA-Veton).

What changed recently — and what to re-check if you already deployed an earlier copy of
this project — is in the [changelog](CHANGELOG.md).

## Screenshot

![Loxone Wallbox visualization](docs/images/wallbox-preview.svg)

> Illustrative mockup of the Wallbox visualization. A real screenshot from the
> Loxone app/Config is welcome — drop a PNG into `docs/images/` and update this.

## What's included

- **`Veton.Loxone`** — a Loxone Config project (Config 16.x) containing:
  - a **Modbus server + device** preconfigured with the CHARX register map,
  - the charger's metering + status mapped to inputs (energy, power, per-phase
    voltage & current, vehicle status, error code, release mode, SOC),
  - write outputs for **max charging current** (X301) and the **CHARX safety
    watchdog** (X306 fallback current + X307 timer),
  - a **Wallbox** function block and a visualization page.
- **`Veton-2CP.Loxone`** — the same project for a **two-charging-point** cabinet:
  one Modbus device carrying both points (CP1 on `1xxx`, CP2 on `2xxx`), with the
  Wallbox block, formulas and visualization mirrored per point.
- **`MB_Veton.xml`** — a **Modbus device template** (the format the Loxone
  Library expects). Importing it adds the CHARX register map (12 sensors +
  3 actuators) to a Modbus device in one step, without the rest of the
  project. Targets **charging point 1** (`1xxx`). Two of the inputs need a
  manual validity-range fix after import — see
  [After importing the template](#after-importing-the-template-required).
- **`MB_Veton_CP2.xml`** — the same template for **charging point 2** (`2xxx`),
  to add alongside the first one on a two-point cabinet.

## Requirements

- **Loxone Miniserver Gen 2** with **Loxone Config 16.0.6.10** or newer (the
  templates declare `minVersion="16000610"`). Both projects are saved as Gen 2
  (`Miniserver="2"`) and have only been tried there — Gen 1 is untested, not known
  to be unsupported.
- A **Veton / CHARX** charger reachable on the LAN with **Modbus/TCP enabled** (port `502`).

## Install

1. Open `Veton.Loxone` in Loxone Config (or copy the **Veton** Modbus device +
   Wallbox page into your existing project).
2. Select the **Veton** Modbus server and set its **Address** to your charger's
   IP (port `502`). *The project ships with this field blank on purpose.*
3. The register addresses target **connector 1** (`1xxx`). For another connector,
   offset by `connector × 1000` (connector 2 → `2xxx`: `2120`, `2232`, `2244`,
   `2250`, `2301` …).
4. Save to the Miniserver.

## Register map (CHARX)

| Function | Register | Operation |
|---|---|---|
| Counter active energy | X250 | read |
| Active power | X244 | read (5 s) |
| Voltage L1 / L2 / L3 | X232 / X234 / X236 | read |
| Current L1 / L2 / L3 | X238 / X240 / X242 | read |
| Vehicle status | X299 | read |
| Charging release mode | X120 | read |
| SOC | X264 | read |
| Error code | X293 | read |
| Max charging rate (A) | X301 | **write** (FC06, re-sent hourly) |
| Watchdog fallback current (A) | X306 | **write** (FC06, re-sent hourly) |
| Watchdog timer (s) | X307 | **write** (FC06, re-sent every 60 s) |
| Charging release | X300 | *not written* — see below |
| Locking | X303 | *not written* — see below |

> Connector offset = `connector × 1000`, so connector 1 → registers `1xxx`.

All values are FC03 holding-register reads / FC06 holding-register writes — the
CHARX has **no coils**. Voltages, currents and power arrive in **milli-units**;
the inputs carry a two-point correction that scales them:

| Input | Wire | Displayed as | Correction |
|---|---|---|---|
| Active power (X244) | mW | **kW** | `SourceValHigh="1000000" DestValHigh="1"` (×1e-6) |
| Voltage / current | mV / mA | V / A | `1 → 0.001`, `100 → 0.1` (×1e-3) |
| Counter active energy (X250) | Wh | kWh | `1 → 0.001`, `100 → 0.1` (×1e-3) |

Active power feeds the Wallbox block's power input, which is **kilowatts** — an
earlier mW→W correction there made the block read 1000× high.

### Why X300 / X303 are not written

Earlier versions of this project and of the templates wrote **X300 (charging
release)** and **X303 (locking)**. Both writes are gone as of 2026-09-03, for
two independent reasons:

1. **Policy.** Veton chargers ship with **release mode = OCPP**. The OCPP
   backend decides *whether* a car may charge (authorization, start/stop,
   billing, app visibility); an EMS only decides *how fast*, via `X301` — which
   works in every release mode. Driving `X300` means taking release away from
   OCPP, which breaks authorization, session/transaction records and
   app/backend visibility. See
   [`Veton-EMS-Integration/docs/modbus.md`](https://github.com/Veton-ev/Veton-EMS-Integration/blob/main/docs/modbus.md).
2. **They never worked anyway.** Both were issued as `ModbusCmd="5"` —
   *FC05 Write Single Coil*. A CHARX exposes **holding registers only**
   (FC03 read / FC06 write, FC16 for multi-register writes); it has no coils, so
   every one of those writes was rejected.

A **standalone, non-OCPP** deployment (no backend, release mode = `Modbus`) may
legitimately need them. Add them by hand, as **FC06 Write Single Register
(`ModbusCmd="6"`), never FC05 Write Single Coil**:

| Function | Register | Values | Requires |
|---|---|---|---|
| Charging release | X300 | 0 = off, 1 = allow | release mode = `Modbus` (X120 = 5) |
| Locking | X303 | 0 / 1 | locking mode = external |

Do not do this on a Veton charger that talks to the Veton backend.

### Watchdog (X306 / X307)

The CHARX has a **safety watchdog** on the current setpoint: if nothing writes
**X307** for the number of seconds it holds, the charger falls back to the
current in **X306**. It protects against a Miniserver that dies mid-session
while `X301` is sitting at a high value.

| Register | Ships as | Meaning |
|---|---|---|
| X306 | **6 A** | fallback current when the timer expires (valid range 6–80 A; a value outside it withdraws the charging release) |
| X307 | **300 s** | the deadline; `65535` disables the watchdog |

6 A is the IEC 61851 minimum, so a dead Miniserver **slows the car down rather
than cutting it off**.

Loxone has no "keep-alive" primitive — what feeds the watchdog is the actuator's
**`RepeatRate`**: Loxone re-sends an analogue output's value every `RepeatRate`
seconds as long as that value is greater than 0. X307 therefore ships with
`RepeatRate="60"` — five re-sends of margin inside the 300 s window — and X306,
which only needs to be present, with `RepeatRate="3600"`. In the projects both
registers are driven by a constant **Formula** block feeding an output ref; in
the templates they are plain actuators you feed with a constant (`6` and `300`).

**`RepeatRate` must stay well below the X307 value — keep at least a 2× margin.**
If you raise it above the timer (or lengthen the timer's re-send interval), the
watchdog fires during normal operation and pegs the charger at the 6 A fallback.

### Vehicle status (X299) → Wallbox **Vc** / **Cac**

`X299` is **one register holding two ASCII characters** (IEC 61851 state), so it
arrives in Loxone as a plain number:

| State | Meaning | Register value |
|---|---|---|
| `A1` / `A2` | no vehicle connected | 16689 / 16690 |
| `B1` / `B2` | vehicle connected, not charging | 16945 / 16946 |
| `C1` / `C2` | charging (no ventilation) | 17201 / 17202 |
| `D1` / `D2` \* | charging (ventilation required) | 17457 / 17458 |
| `E0` | CP short circuit / PE lost | 17712 |
| `F0` | EVSE fault / unavailable | 17968 |
| `IN` | invalid state | 18766 |

\* `D1` / `D2` are defined by IEC 61851 (charging with ventilation required,
CP at 3 V) but are **not among the states CHARX actually emits on X299** — its
register map lists only `A1, A2, B1, B2, C1, C2, E0, F0, IN`. The rows are kept
because the formulas below bound their range harmlessly; do not expect to see
them on a Veton charger.

The Wallbox block's **Vc (Vehicle connected)** and **Cac (Charging active)**
inputs therefore need digital signals, not the raw register. The project derives
both with **Formula** blocks fed from the *Vehicle status* input:

| Block | Formula | On for |
|---|---|---|
| `Vehicle connected` → **Vc** | `IF(I1>16944;IF(I1<17459;1;0);0)` | B/C/D — a car is plugged in |
| `Charging active` → **Cac** | `IF(I1>17200;IF(I1<17459;1;0);0)` | C/D — the car is drawing |

Both are **0 for A (no vehicle) and for the fault states E0/F0/IN**. Wire the
same two formulas if you build the project yourself from `MB_Veton.xml`.

## Setpoint: three-phase (default) vs single-phase (`mono`)

The Wallbox block asks for **power (kW)**; `X301` wants **current (A)**. A
**Formula** block converts, and the project ships **two** of them on the Program
page:

| Block | Formula | Inputs | For |
|---|---|---|---|
| `3 fase` | `MIN(MAX(INT((I1*1000)/(400*1,732));6);80)` | I1 = Wallbox `outLimit` [kW] | three-phase 400 V — **wired by default** |
| `mono` | `IF(I2>100;MIN(MAX(INT((I1*1000)/I2);6);80);6)` | I1 = Wallbox `outLimit` [kW], I2 = *Voltage of phase L1* [V] | single-phase — **shipped on the page, deliberately not wired** |

**Only one of the two may be connected to `Max Charging Rate I`.** Both were
connected in earlier versions, so two blocks wrote conflicting values into the
same register, last-writer-wins. `mono` was disconnected on 2026-09-03 and
`3 fase` kept.

`X301`'s valid range is **6–80 A** and **writing 0 withdraws the charging
release** entirely (documented in `Veton-EMS-Integration/docs/modbus.md`), so a
0 kW request from the Wallbox block must land on 6 A, not on 0 — that is what
the `MIN`/`MAX` clamp is for.

### The setpoint truncates, it never rounds up

`X301` is a whole-ampere register, so the value Loxone computes has to become an
integer somewhere. Left to the register write it would **round**, and rounding
up asks the charger for more than the Wallbox block budgeted: 7.4 kW works out
to 10.68 A, which rounds to **11 A ≈ 7.62 kW** — about 0.22 kW over budget, and
up to ~0.5 A in the worst case. On a site whose load management handed out
exactly 7.4 kW, that overshoot is real current on a real fuse.

Both formulas therefore wrap the division in **`INT()`** (Loxone's documented
"integer function (removes decimals)"), so the setpoint **truncates**: 7.4 kW →
10 A, 11 kW → 15 A, 22 kW → 31 A. Loxone never asks the charger for more power
than the Wallbox block asked for. The one deliberate exception is the bottom of
the range: a request below 6 A still lands on the 6 A floor, because writing 0
would withdraw the charging release altogether.

### Single-phase (`mono`)

On a **single-phase supply**, swap the two blocks in Loxone Config: disconnect
`3 fase` from `Max Charging Rate I`, then connect `mono`'s output to it instead.
`mono` already ships with the clamp, the truncation **and** a divide-by-zero
guard:

```
IF(I2>100;MIN(MAX(INT((I1*1000)/I2);6);80);6)
```

`I2` is the **measured** *Voltage of phase L1* input, not a nominal 230 V — and
a measured input reads **0** whenever the charger is unreachable, the poll has
not landed yet, or the contactor is open. Dividing by that yields 0 A, and 0 is
precisely the value that withdraws the charging release. The `IF` guard means
*"only compute a setpoint when we have a plausible mains reading"*: **100 V** is
below every real single-phase supply (230 V nominal, 207–253 V at ±10 %) and
above every partial or absent reading, so anything at or under it falls back to
the 6 A minimum instead of dividing.

If you would rather not depend on a live reading at all, use the nominal voltage
and drop the guard with it: `MIN(MAX(INT((I1*1000)/230);6);80)`, leaving I2
unconnected.

> Loxone formulas use a **comma** as the decimal separator (`1,732`) and a
> **semicolon** as the argument separator.

## Multiple charging points

`Veton.Loxone` covers **one charging point** (connector 1). For a two-point
cabinet there is a ready-made variant: **`Veton-2CP.Loxone`**.

Register addresses are `connector × 1000 + register`:

| Charging point | Register base | e.g. max current |
|---|---|---|
| 1 | `1xxx` | `1301` |
| 2 | `2xxx` | `2301` |
| 3 | `3xxx` | `3301` |

### `Veton-2CP.Loxone` (two charging points)

One CHARX controller is a **single Modbus TCP endpoint**, so both charging
points live in **one** Modbus device (`Veton - Charger`) — CP1 on `1xxx`, CP2 on
`2xxx` — rather than two devices sharing an address. Everything downstream is
mirrored per point:

| Per charging point | CP1 | CP2 |
|---|---|---|
| Modbus sensors / actuators | 12 + 3 @ `1xxx` | 12 + 3 @ `2xxx` |
| Wallbox block | `Wallbox CP1` | `Wallbox CP2` |
| Vc / Cac formulas | `CP1 Vehicle connected` / `CP1 Charging active` | `CP2 …` |
| Error status block | `CP1 Error` | `CP2 Error` |
| Vehicle-status tracker | `CP1 Vehicle status` | `CP2 Vehicle status` |

Set the Modbus server **Address** once — both points share it.

For the charger I/O alone, `MB_Veton.xml` covers connector 1 and
**`MB_Veton_CP2.xml`** covers connector 2. For a third point and beyond, copy
either template and add `1000` to every `ModbusAddress`.

## Modbus device template (`MB_Veton.xml`)

For just the charger I/O (no full project), use the Modbus device template:

1. In Loxone Config, select the **Modbus Extension** (or Modbus TCP) in the
   periphery tree → ribbon **Device templates → Import template** → pick
   `MB_Veton.xml`. *(Or drop it straight into Loxone Config's template folder —
   `C:\ProgramData\Loxone\Loxone Config <ver>\Templates\Comm\` on Config 17,
   `…\LoxoneConfig <ver>\ENG\Comm\` on older builds. The filename **must** keep
   the `MB_` prefix.)*
2. Add the device, set the charger's **IP** (port `502`), then wire the inputs/
   outputs to a Wallbox block.

The template targets **charging point 1** (registers `1xxx`); for more points,
offset every address by `connector × 1000` (see above).

This is also the file to submit to the official **[Loxone Library](https://library.loxone.com/)**
(curated by Loxone).

### After importing the template (required)

A Loxone Modbus **device template** has no way to express an input's validity
range — `MinVal`/`MaxVal` are not template attributes (Loxone's own shipped
templates use none). Three inputs therefore need their range widened **by hand**,
once, in Loxone Config after the device is added. The full `.Loxone` projects
already ship with all three applied; only template users have to do this.

| Input | Set | Why |
|---|---|---|
| **Counter Active Energy** (X250) | Maximum value `10000` → **`100000000`** | It is a **lifetime** kWh counter. A charger past 10 000 kWh exceeds the default ceiling and the input stops reporting — a charger on the bench already reads 12 182 kWh, so the old ceiling breaks real units today. |
| **Vehicle status** (X299) | Minimum **`0`**, Maximum **`65535`** | The value is two ASCII characters packed into one 16-bit register, i.e. ≈ 16 000–19 000 (`A1` = 16689 … `IN` = 18766) — out of range under the default limits, so `Vc`/`Cac` never go true. |
| **Error Code** (X293) | Minimum **`0`**, Maximum **`4294967295`** | X293/X294 is a **32-bit error bitfield**, not a fault number: bit *N* has the mask `2**(N-1)`. So bit 14 alone is already 8192, bit 15 is 16384 — past the stock ±10000 ceiling — and a bit 22 fault is **2 097 152**, 200× over it. Under the default limits the input silently stops showing real faults: every fault from bit 15 up is out of range, so the fault display reads clean while the charger is in error. |

In Loxone Config: select the input in the periphery tree → **Properties** →
*Minimum value* / *Maximum value*. Redo this after re-importing the template.

## Notes

- Ships with the Modbus server **IP blank** and the document **APPKEY cleared** —
  set your own on import.
- **Nothing here writes charging release.** The projects and templates only write
  `X301` (current cap) and `X306`/`X307` (watchdog); release stays with OCPP.
  See [Why X300 / X303 are not written](#why-x300--x303-are-not-written).
- Template users: do the three manual range fixes above, otherwise the energy
  counter, the vehicle-status derived `Vc`/`Cac` signals and the error-code
  display will not work.
- The `X301` setpoint **truncates** rather than rounds, so Loxone never asks the
  charger for more power than the Wallbox block budgeted. See
  [The setpoint truncates, it never rounds up](#the-setpoint-truncates-it-never-rounds-up).

## License

[MIT](LICENSE) — free to use and modify.
