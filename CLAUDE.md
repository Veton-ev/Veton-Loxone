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
over **Modbus/TCP `:502`**, surfaced through Loxone's native **Wallbox** block. Two artefacts,
no code: `Veton.Loxone` (a Loxone Config 16.x project file, XML: Modbus server + device with
the CHARX register map, inputs for energy/power/V/I/vehicle status/error/release mode/SOC,
outputs for release X300, max current X301 and lock X303, a Wallbox block + visualisation
page) and `MB_Veton.xml` (a Loxone **Modbus device template**, the format the official Loxone
Library accepts, charging point 1 only). Consumers: Loxone installers/end customers and the
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
# test — there is NO test suite. The only mechanical check (ran green 2026-08-26) covers the
#        template only; Veton.Loxone is NOT parseable by a strict XML parser (see Gotchas):
python3 -c "import xml.etree.ElementTree as ET; ET.parse('MB_Veton.xml')"
# build: none
```
CI: **none** — no `.github/workflows/`. Target state: a `ci.yml` (job id `ci`) running the
`MB_Veton.xml` parse above, a grep that `APPKEY=""` and the Modbus server `Address=""` are
still blank in `Veton.Loxone`, and gitleaks; see `veton-handbook/HANDOVER-PLAN.md` Phase 4.

## Deploy
**Not deployed.** Not in the 2026-08-26 deploy survey
(`veton-handbook/70-runbooks/deploy-paths-survey-2026-08-26.md`). Publishing = merging to
`main` (GitHub) and, separately, a manual submission of `MB_Veton.xml` to the Loxone Library
(curated by Loxone, no automation). Rollback = revert the commit.
Blast radius of a bad change: Loxone customers import wrong register numbers / an unsafe write
into their Miniserver — nothing on the Veton fleet or cloud changes.

## Talks to (seams)
- **CHARX Modbus register map** — hard-coded twice here (`Veton.Loxone` device `Address="1xxx"`
  attributes and `MB_Veton.xml` `ModbusAddress`), charging point 1 = `1xxx`, others at
  `connector × 1000`. Same map in `Veton-EMS-Integration/docs/modbus.md`, `vetonlm/registers`,
  `savings-collector`, `charx-doctor/references`, `veton-ha`, `veton-evse-agent/internal/charx`.
  Change a register → change **both files here** and check the other copies.
- **`Veton-ev/Veton-EMS-Integration`** is the documented reference and states the integration
  policy; **`Veton-ev/HA-Veton`** is the sibling integration. Keep the three consistent.
See handbook `00-orientation/seams.md`.

## Gotchas (this repo only)
- **Two copies of the register map, edited by hand.** `Veton.Loxone` and `MB_Veton.xml` were
  authored separately (commits `73d533f`, `bcda557`); nothing checks they agree. Diff them
  after any register change.
- **Re-saving from Loxone Config re-embeds secrets.** A project saved against a real
  Miniserver carries the document `APPKEY` and the Modbus server IP. The shipped file has
  `APPKEY=""` and `Address=""` on purpose (README "Notes"). Before committing a re-exported
  `Veton.Loxone`, grep both back to empty — this is a public repo.
- **This project writes charging release (X300)**, whereas `Veton-EMS-Integration` (commit
  `e7b3555`) documents that an EMS must leave release to OCPP and only cap X301. This
  inconsistency is real and known; do not silently "fix" either side — raise it with the owner.
- **`Veton.Loxone` is not well-formed XML by the spec** — line 3 carries an attribute named
  `12hTF="true"` (names may not start with a digit). Loxone Config accepts it; `xml.etree`,
  `xmllint` and most XML tooling reject it with "not well-formed (invalid token): line 3,
  column 559". Do not "fix" the attribute (Loxone wrote it) and do not run the file through
  an XML formatter/linter that would rewrite it. Only `MB_Veton.xml` can be parsed mechanically.
- **Both files are UTF-8 with BOM and CRLF line endings** (Windows-authored). `MB_Veton.xml`
  declares `minVersion="16000610"` (Config 16.0.6.10+). Editors that strip the BOM, convert to
  LF, or pretty-print may break the Loxone import; the only real validation is Loxone Config,
  which does not run on this Linux box.
- **Only charging point 1 is covered.** Multi-point setups are a manual duplicate-and-offset
  procedure (README "Multiple charging points"); there is no generator script.
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
