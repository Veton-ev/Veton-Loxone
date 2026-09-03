# Veton-Loxone — Claude Code instructions

<!-- Template from veton-handbook/templates/CLAUDE.md.template. Keep every section; write
"none" rather than deleting one. Facts here must be checked against the code, not remembered.
Cross-cutting knowledge (fleet, runbooks, access, suppliers) lives in the handbook — link, don't copy. -->

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
the CHARX register map, inputs for energy/power/V/I/vehicle status/error/release mode/SOC,
outputs for max current X301 plus the CHARX safety watchdog X306 (fallback current 6 A) and
X307 (timer 300 s) — the X300 release and X303 lock writes were dropped 2026-09-03, see
Gotchas — a Wallbox block + visualisation page), `Veton-2CP.Loxone` (the same project for a **two-charging-point** cabinet: one Modbus
device carrying CP1 on `1xxx` and CP2 on `2xxx`, with the Wallbox block, Vc/Cac formulas and
visualisation mirrored per point), `MB_Veton.xml` (a Loxone **Modbus device template**, the
format the official Loxone Library accepts, charging point 1) and `MB_Veton_CP2.xml` (the same
template for charging point 2, `2xxx`). Consumers: Loxone installers/end customers and the
Loxone Library. It is the Loxone counterpart of `Veton-ev/HA-Veton`; the protocol reference
behind both is `Veton-ev/Veton-EMS-Integration`. **Not deployed.**
Handbook: `veton-handbook/10-fleet/`.

## Ownership
Owners: TBD (Brend / Andrii). Escalation: Jens. Handbook page: `veton-handbook/10-fleet/`.

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
```
`tools/fix-loxone-project.py` re-applies the ten hand fixes to the two `.Loxone` projects:
blank the document password salt (F1), Active Power X244 correction mW→kW (F2), Counter
Active Energy X250 `MaxVal` ceiling 10 000 → 100 000 000 (F3), drop the X300/X303 writes
(F4), clamp the X301 setpoint to 6–80 A and unwire `mono` so only `3 fase` drives it (F5),
add the X306/X307 watchdog (F6), audit **every** `ModbusASensor`'s `MinVal`/`MaxVal` against
the physically possible span of its register and widen the ones that cannot show it (F7 —
today that is only **Error Code X293**, a 32-bit *bitfield* where bit N = `2**(N-1)`, so
±10 000 hides every fault from bit 15 up; set to `0`/`4294967295`), wrap the X301 division in
`INT()` so the setpoint **truncates** instead of rounding up past the budget the Wallbox block
asked for (F8 — 7.4 kW was 10.68 A → 11 A ≈ 7.62 kW, now 10 A), and guard the single-phase
`mono` formula's divide-by-zero (F9 —
`IF(I2&gt;100;MIN(MAX(INT((I1*1000)/MAX(I2;100));6);80);6)`, shown exactly as it is stored,
i.e. `>` XML-escaped like the existing `IF(I1&gt;16944;…)`; the divisor is floored as well as
guarded, because Loxone's `IF` laziness is undocumented; I2 is the *measured* L1 voltage and
reads 0 whenever the charger is unreachable, and 0 A on X301 withdraws the charging release),
and remove the `12hTF` document attribute so the file is well-formed XML (F10 — Config writes
it back on every save, so this fix runs every time). `mono` stays unwired, but README tells single-phase
installers to swap to it, so it has to be safe first. It matches on stable attribute patterns
(`ModbusAddress=`, `Title=`, `Formula=`, `Ref=`, `Co K=`) — never on line numbers — preserves
BOM + CRLF, and **is idempotent**: every fix detects "already applied" and skips, so a second
run rewrites nothing (byte-identical). It then self-verifies (BOM, line endings, unique `U=`,
no dangling `<In Input>`/`Ref=`, `<C>`/`</C>` balance, strict XML well-formedness *and* the
absence of `12hTF`, salt and `APPKEY`/`Address`/`Serial` blank, expected `ModbusAddress` set, no overlapping
page rectangles, every sensor range wide enough for its register, and both setpoint formulas
in their final clamped/truncated/guarded form).
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
**Not deployed.** Not in the 2026-08-26 deploy survey
(`veton-handbook/70-runbooks/deploy-paths-survey-2026-08-26.md`). Publishing = merging to
`main` (GitHub) and, separately, a manual submission of `MB_Veton.xml` to the Loxone Library
(curated by Loxone, no automation). Rollback = revert the commit.
Blast radius of a bad change: Loxone customers import wrong register numbers / an unsafe write
into their Miniserver — nothing on the Veton fleet or cloud changes.
Path to `main`: PR with the `ci` check green (secret scan); agents cannot push to `main` directly.

## Talks to (seams)
- **CHARX Modbus register map** — hard-coded **four times** here (`Veton.Loxone` and
  `Veton-2CP.Loxone` device `ModbusAddress="1xxx"`/`"2xxx"` attributes, plus `MB_Veton.xml` and
  `MB_Veton_CP2.xml` `ModbusAddress`), charging point 1 = `1xxx`, point 2 = `2xxx`, others at
  `connector × 1000`. Same map in `Veton-EMS-Integration/docs/modbus.md`, `vetonlm/registers`,
  `savings-collector`, `charx-doctor/references`, `veton-ha`, `veton-evse-agent/internal/charx`.
  Change a register → change **all four files here** and check the other copies.
- **`Veton-ev/Veton-EMS-Integration`** is the documented reference and states the integration
  policy; **`Veton-ev/HA-Veton`** is the sibling integration. Keep the three consistent.
See handbook `00-orientation/seams.md`.

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
  script that exists only at
  `/tmp/claude-1000/-home-jens/13a2750e-a229-472e-be3a-8558fd694949/scratchpad/make2cp.py`
  — a session scratchpad, i.e. throwaway. It is a **known gap**: nothing in the repo can
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
- `docs/images/wallbox-preview.svg` is a **mockup**, not a screenshot (README says so). Do not
  present it as the real Loxone UI.
- `Veton.Loxone` is 130 KB of generated XML with GUIDs — review diffs semantically (which
  `Address`/`Title` changed), not line by line.

## Agent rules
- Work on a branch; open a PR; never push to `main` (to be enforced by `.claude/settings.json`
  + `.claude/hooks/guard.sh` and by branch protection — HANDOVER-PLAN Phase 4/5).
- Never run deploy/ssh-to-production commands; there is nothing to deploy from this repo.
- **Public repo:** never commit secrets, Miniserver APPKEYs, charger IPs, customer names or
  fleet numbers; `gitleaks` pre-commit + CI will fail the commit/PR once Phase 4 lands. No
  secrets belong to this repo; the handbook `60-access/` says where fleet secrets live, never
  the value.
- Write durable lessons to `veton-handbook/lessons/` (one file per lesson), not to
  personal memory.
