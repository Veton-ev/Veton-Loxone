#!/usr/bin/env python3
"""Idempotent fix pass for the Veton Loxone Config project files.

Usage:  tools/fix-loxone-project.py <project.Loxone> [...]
        tools/fix-loxone-project.py --check <project.Loxone>   (verify only)

The .Loxone project file is generated XML: UTF-8 *with BOM*, CRLF line endings,
tab indented, one XML element per line (except multi-line text attributes inside
<StateText>).  As shipped it used to carry an attribute whose name starts with a
digit (12hTF="true"), which a strict XML parser rejects; F10 removes it, so the
file now validates with ordinary tooling.  Loxone Config writes it back on save,
which is one of the reasons this script must be re-run after every save.

Everything here matches on stable attribute patterns (ModbusAddress=, Title=,
Ref=, Co K=) -- never on line numbers or byte offsets -- so the script survives
Loxone Config re-saving the file on Windows.  Every fix detects whether it is
already applied and skips, so re-running is a no-op (byte identical output).

Fixes
-----
F1  blank the document-level password salt (public repo)
F2  Active Power (X244) correction mW->kW instead of mW->W (Wallbox wants kW)
F3  Counter Active Energy (X250) MinVal/MaxVal ceiling raised off 10 MWh
F4  drop the X300 "Charging Release" and X303 "Locking" writes entirely
F5  clamp the X301 setpoint to 6..80 A and stop 'mono' + '3 fase' both driving it
F6  add the CHARX watchdog: X306 fallback current + X307 watchdog timer
F7  validity ranges: every ModbusASensor must be able to show every value its
    register can physically carry (X293/X294 is a 32-bit bitfield, not an ordinal)
F8  X301 setpoint truncates (INT) instead of rounding up past the asked budget
F9  the 'mono' single-phase formula guards its divide-by-zero and gets F5+F8
F10 remove the 12hTF document attribute so the file is well-formed XML
F11 pause: X301 = 0 when the Wallbox block's target power (outLimit) is 0, so a
    paused / load-shed / not-allowed car actually stops instead of idling at the
    6 A floor (~4 kW); any non-zero target keeps the 6..80 A clamp + truncation
F12 X301 RepeatRate 3600 -> 60 s: the setpoint (and a 0 A pause) is re-asserted
    at the watchdog cadence, so a CHARX-side revert of an external X301 write
    is undone within a minute instead of an hour
F13 last RFID card UID (X275, String) -> Wallbox 'user' input, so a charging
    session is attributed to the card holder; read-only, no new Modbus write
"""

from __future__ import annotations

import html
import re
import sys
import xml.etree.ElementTree as ET

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

# Never hardcode the salt itself -- this is a PUBLIC repo, and a literal here
# would re-publish the very value the fix exists to remove.  Match any non-empty
# S= on an <HP> element instead; that also survives Loxone Config re-embedding a
# different one on save.
HP_SALT_RE = re.compile(r'(<HP\b[^>]*?\sS=")([^"]+)(")')

# X244 active power: milliwatts on the wire, kilowatts at the Wallbox block.
POWER_SRC_HIGH = "1000000"
POWER_DST_HIGH = "1"

# X250 lifetime active-energy counter, scaled to kWh by the sensor correction.
ENERGY_MIN = "0"
ENERGY_MAX = "100000000"

# X301 range per Veton-EMS-Integration/docs/modbus.md ("never write 0") -- never
# write 0 EXCEPT as the deliberate pause when the Wallbox block's target power
# is 0 (F11).  On a CHARX, X301 = 0 withdraws the charging release (the car
# stops); writing >= 6 again resumes.  That is the pause primitive Veton's own
# agent uses, and it is the only way Loxone's "pause" reaches the car: the
# Wallbox block signals pause / load-shed / not-allowed by driving outLimit to
# 0 kW, and a plain 6 A floor would keep the car charging at ~4 kW.
X301_MIN, X301_MAX = 6, 80
# F12 -- re-send cadence of the X301 setpoint (was 3600 s).  Same as X307's.
X301_REPEAT = "60"

# The '3 fase' formula goes through three states: as authored, F5-clamped, and
# F8-truncated.  INT() is documented on Loxone's own Formula page ("Integer
# function (removes decimals)") alongside ABS/SQRT/MIN/MAX/IF, so it is safe to
# use; without it the 16-bit register write ROUNDS, and 7.4 kW -> 10.68 A -> 11 A
# asks the charger for ~7.62 kW, more than the Wallbox block budgeted.
FORMULA_3F_RAW = "(I1*1000)/(400*1,732)"
FORMULA_3F_CLAMPED = "MIN(MAX((I1*1000)/(400*1,732);%d);%d)" % (X301_MIN, X301_MAX)
FORMULA_3F_TRUNC = "MIN(MAX(INT((I1*1000)/(400*1,732));%d);%d)" % (X301_MIN, X301_MAX)
# F11 -- the final form: 0 kW target -> 0 A (pause), anything else -> the F5/F8
# clamp.  The Wallbox block's minimum-power parameter is 4.16 kW, so every
# non-zero outLimit already lands at >= 6 A; the IF only separates "paused"
# from "charging".  '>' is XML-escaped exactly like the existing
# IF(I1&gt;16944;...) formulas in the file.
FORMULA_3F_PAUSE = "IF(I1&gt;0;%s;0)" % FORMULA_3F_TRUNC

# The 'mono' single-phase formula divides by the *measured* L1 voltage, which
# reads 0 whenever the charger is unreachable or the contactor is open.  100 V is
# the "we have a plausible mains reading" floor: every real supply mono is used
# on (230 V nominal, 207-253 V at +-10 %) clears it by a wide margin, and no
# partial/absent reading does.  Below it the formula falls back to the IEC 61851
# minimum 6 A instead of dividing.  '>' lives in an XML attribute, so it is
# escaped exactly like the existing IF(I1&gt;16944;...) formulas in the file.
MONO_V_FLOOR = 100
FORMULA_MONO_RAW = "(I1*1000)/I2"
# Belt and braces: the divisor is ALSO floored with MAX(I2;100), so the
# expression cannot divide by zero even if Loxone evaluates both IF branches
# eagerly.  Loxone's IF laziness is undocumented and untestable without a
# Miniserver, and the consequence of getting it wrong is a formula error on a
# current setpoint -- so the formula is made safe under either semantics rather
# than relying on the guard alone.
FORMULA_MONO_PREV = "IF(I2&gt;%d;MIN(MAX(INT((I1*1000)/I2);%d);%d);%d)" % (
    MONO_V_FLOOR, X301_MIN, X301_MAX, X301_MIN
)
FORMULA_MONO_NEW = "IF(I2&gt;%d;MIN(MAX(INT((I1*1000)/MAX(I2;%d));%d);%d);%d)" % (
    MONO_V_FLOOR, MONO_V_FLOOR, X301_MIN, X301_MAX, X301_MIN
)
# F11 -- pause wraps the guarded form: 0 kW -> 0 A regardless of the voltage.
FORMULA_MONO_PAUSE = "IF(I1&gt;0;%s;0)" % FORMULA_MONO_NEW

# F10 -- 12hTF="true" is a 12-hour-clock display setting, and its name starts
# with a digit, which XML forbids.  It makes the whole project unreadable to every
# strict parser (CI's xmllint, gitleaks, lox-cli, ElementTree) for no benefit --
# and a 12-hour clock is wrong on a Ctry="BE" project anyway.  Removing it is what
# lets the file be validated by ordinary tooling.
TF12_RE = re.compile(r'\s12hTF="[^"]*"')

# F7 -- physically possible RAW register span per CHARX register, keyed by the
# register number without the charging-point offset.  Source:
# charx-doctor/references/modbus-register-map.yaml (Phoenix UM EN CHARX SEC
# 109999_en_08 appendix B3).  Run through the input's own two-point correction
# this becomes the engineering span MinVal/MaxVal has to accommodate.
REGISTER_SPAN = {
    120: (0, 5, "release-mode enum 0..5 (Dashboard/Whitelist/External/Permanent/OCPP/Modbus)"),
    232: (0, 500_000, "voltage L1 [mV]; 500 V is above any 400 V TN phase voltage"),
    234: (0, 500_000, "voltage L2 [mV]; 500 V is above any 400 V TN phase voltage"),
    236: (0, 500_000, "voltage L3 [mV]; 500 V is above any 400 V TN phase voltage"),
    238: (0, 100_000, "current L1 [mA]; X301 caps at 80 A, 100 A leaves overcurrent headroom"),
    240: (0, 100_000, "current L2 [mA]; X301 caps at 80 A, 100 A leaves overcurrent headroom"),
    242: (0, 100_000, "current L3 [mA]; X301 caps at 80 A, 100 A leaves overcurrent headroom"),
    244: (0, 3 * 80 * 253 * 1000, "active power [mW]; worst case 3 x 80 A x 253 V"),
    250: (0, 10**11, "active-energy counter [Wh]; 1e11 Wh = 60.7 kW flat out for ~190 years"),
    264: (0, 100, "SOC [%]"),
    # X275 is read as a Loxone String (ModbusDataType 101, F13): the value is
    # text, so MinVal/MaxVal do not apply.  The STRING marker keeps the F7 audit
    # from widening it AND from reporting it as NOT AUDITED.
    275: ("string", "string", "last RFID card UID: 10 registers of big-endian ASCII, NUL-padded, "
                              "e.g. 14 hex characters for a 7-byte card, 8 for a 4-byte card"),
    293: (0, 0xFFFFFFFF, "32-bit error BITFIELD, MSB X293 / LSB X294 -- bit N is 2**(N-1), "
                         "so a lone bit 22 fault is 2097152 and any bit from 14 up "
                         "already exceeds a 10000 ceiling"),
    299: (0, 0xFFFF, "vehicle status: two ASCII characters packed into one 16-bit register"),
}

# X306 / X307 watchdog.  RepeatRate re-sends the output value every N seconds
# unconditionally -- Loxone Config 17's own help text for the Modbus output
# repeat property reads "Interval in seconds, at which the output value is sent
# again"; it does NOT condition on the value being > 0 (that condition belongs
# to virtual outputs).  So a 0 A pause on X301 is re-sent too (F12).  300 s
# timer with a 60 s re-send leaves five missed sends of margin, and 6 A is the
# IEC 61851 minimum so a dead Miniserver slows the car instead of stopping it.
WATCHDOG = [
    # (register, title, constant, RepeatRate [s], display unit)
    (306, "Watchdog fallback current", "6", "3600", "A"),
    (307, "Watchdog timer", "300", "60", "s"),
]
# internal names for the minted Modbus actuators; CP1 -> AMQ60/61, CP2 -> AMQ62/63
INAME_BASE = 60

# F13 -- last RFID card UID.  CHARX register X275 (per charging point: 1275,
# 2275, ... like the existing 1232/1299 inputs): FC03 holding registers, 10
# words of big-endian ASCII, NUL-padded, holding the UID of the last card
# presented -- e.g. 14 hex characters for a 7-byte card, 8 for a 4-byte card.
# Measured byte-identical against the charger's own RFID event stream on a
# production fleet (firmware 1.7.3, release mode OCPP); populated in every
# release mode, so no mode gate.  It is STICKY: it keeps the previous card
# until the next tap.  X308 ("reset last RFID") was measured INERT on that
# firmware (write acknowledged, X275 unchanged), so no reset write is added
# and this fix adds NO Modbus write at all.
#
# Loxone Config's Modbus analogue data-type list (Config 17's own resource
# strings) is, in order: 16-bit Unsigned, 16-bit Signed, 32-bit Unsigned,
# 32-bit Signed, 32-bit Float, String, 64-bit Unsigned, 64-bit Signed, 64-bit
# Double, and the XML attribute is ModbusDataType = 96 + index: the existing
# 98 = 32-bit unsigned, 102 = 64-bit unsigned, so String = 101.  HOW MANY
# registers Config reads for a String is not known -- Config's attribute
# vocabulary has no length attribute (only ModbusAddress, ModbusCmd,
# ModbusDataType, ModbusPollingCycle, ModbusCoilQuantity, ModbusID), so none
# is invented here.  TO CONFIRM in Loxone Config: the input should show the
# full 14-character UID of a 7-byte card; fewer characters means Config reads
# fewer registers.
RFID_REG = 275
RFID_TITLE = "Last RFID card"
RFID_DATATYPE = "101"          # String
RFID_DESC = ("Last RFID card UID as ASCII text (CHARX X%d: 10 holding registers, big-endian, "
             "NUL-padded; 14 hex characters for a 7-byte card). Sticky: keeps the previous card "
             "until the next tap. TO CONFIRM in Loxone Config: how many registers the String "
             "data type reads - the value should show the full 14 characters; fewer means "
             "fewer registers are read." % RFID_REG)
# mint() sequence numbers used by F13 (F6 uses 0x00..0x1B per charging point)
RFID_SEQ_BASE = 0x20

# UUID prefix reserved for objects minted by this script.
MINT_PREFIX = "1f77ec02-0300-"
OBJ_SUFFIX = "ffff721cca2b7737"  # 4th group used by page/IO objects

NL = "\r\n"
BOM = "﻿"


# --------------------------------------------------------------------------
# line / block helpers
# --------------------------------------------------------------------------


def read_lines(path: str) -> list[str]:
    raw = open(path, "rb").read()
    if not raw.startswith(b"\xef\xbb\xbf"):
        raise SystemExit("%s: missing UTF-8 BOM" % path)
    text = raw.decode("utf-8-sig")
    if text.count("\r\n") != text.count("\n") or text.count("\r") != text.count("\n"):
        raise SystemExit("%s: mixed line endings; refusing to touch it" % path)
    return text.split(NL)


def write_lines(path: str, lines: list[str]) -> bytes:
    data = (BOM + NL.join(lines)).encode("utf-8")
    open(path, "wb").write(data)
    return data


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip("\t"))


def block_end(lines: list[str], start: int) -> int:
    """Index (inclusive) of the last line of the element opened at line `start`.

    Works for <C> and <Co> alike; the elements this script touches never carry a
    multi-line attribute value (only <StateText> does), so matching the closing
    tag at the same indentation is safe.
    """
    if lines[start].rstrip().endswith("/>"):
        return start
    tag = re.match(r"\t*<(\w+)", lines[start]).group(1)
    closer = "\t" * indent_of(lines[start]) + "</%s>" % tag
    for i in range(start + 1, len(lines)):
        if lines[i] == closer:
            return i
    raise SystemExit("unterminated element at line %d: %s" % (start + 1, lines[start][:80]))


def find_line(lines: list[str], *needles: str, required: bool = True) -> int:
    hits = [i for i, ln in enumerate(lines) if all(n in ln for n in needles)]
    if len(hits) > 1:
        raise SystemExit("ambiguous match for %r (%d hits)" % (needles, len(hits)))
    if not hits:
        if required:
            raise SystemExit("no match for %r" % (needles,))
        return -1
    return hits[0]


def attr(line: str, name: str) -> str | None:
    m = re.search(r'\b%s="([^"]*)"' % re.escape(name), line)
    return m.group(1) if m else None


def set_attr(line: str, name: str, value: str) -> str:
    pat = r'(\b%s=")[^"]*(")' % re.escape(name)
    if not re.search(pat, line):
        raise SystemExit("attribute %s not present in: %s" % (name, line[:120]))
    return re.sub(pat, lambda m: m.group(1) + value + m.group(2), line, count=1)


def drop_attr(line: str, name: str) -> str:
    return re.sub(r'\s+\b%s="[^"]*"' % re.escape(name), "", line, count=1)


def connector_uuid(lines: list[str], start: int, end: int, key: str) -> str:
    for i in range(start, end + 1):
        if '<Co K="%s"' % key in lines[i]:
            return attr(lines[i], "U")
    raise SystemExit("connector %s not found in block at line %d" % (key, start + 1))


# --------------------------------------------------------------------------
# charging points present in this file
# --------------------------------------------------------------------------


def charging_points(lines: list[str]) -> list[tuple[int, str]]:
    """[(connector number, object-title prefix)] -- CP2 objects are 'CP2 '-titled."""
    cps = []
    for cp in range(1, 10):
        i = find_line(lines, 'Type="ModbusAActor"', 'ModbusAddress="%d301"' % cp, required=False)
        if i < 0:
            continue
        title = attr(lines[i], "Title") or ""
        base = "Max Charging Rate I"
        assert title.endswith(base), "unexpected X%d301 actuator title %r" % (cp, title)
        cps.append((cp, title[: -len(base)]))
    if not cps:
        raise SystemExit("no charging point found (no ModbusAddress=X301)")
    return cps


# --------------------------------------------------------------------------
# F1 -- blank the document salt
# --------------------------------------------------------------------------


def fix_salt(lines: list[str], log: list[str]) -> None:
    hits = [i for i, ln in enumerate(lines) if HP_SALT_RE.search(ln)]
    if not hits:
        log.append("F1  salt: already blank")
        return
    for i in hits:
        lines[i] = HP_SALT_RE.sub(r'\1\3', lines[i])
    log.append("F1  salt: blanked %d <HP> element(s)" % len(hits))


# --------------------------------------------------------------------------
# F2 -- Active Power correction mW -> kW
# --------------------------------------------------------------------------


def fix_12htf(lines: list[str], log: list[str]) -> None:
    hits = [i for i, ln in enumerate(lines) if TF12_RE.search(ln)]
    if not hits:
        log.append("F10 12hTF: already removed")
        return
    for i in hits:
        lines[i] = TF12_RE.sub("", lines[i])
    log.append("F10 12hTF: removed %d occurrence(s); the file is now well-formed XML"
               % len(hits))


def linear_map(line: str) -> tuple[float, float]:
    """(slope, intercept) of the Loxone two-point sensor correction."""
    sl = float(attr(line, "SourceValLow") or 0)
    dl = float(attr(line, "DestValLow") or 0)
    sh = float(attr(line, "SourceValHigh"))
    dh = float(attr(line, "DestValHigh"))
    slope = (dh - dl) / (sh - sl)
    return slope, dl - slope * sl


def fix_active_power(lines: list[str], cp: int, log: list[str]) -> None:
    i = find_line(lines, 'Type="ModbusASensor"', 'ModbusAddress="%d244"' % cp)
    end = block_end(lines, i)
    before = linear_map(lines[i])

    if attr(lines[i], "SourceValHigh") == POWER_SRC_HIGH and attr(lines[i], "SourceValLow") is None:
        log.append("F2  X%d244 active power: already mW->kW" % cp)
    else:
        assert abs(before[0] - 0.001) < 1e-12 and abs(before[1]) < 1e-12, (
            "X%d244 unexpected starting correction %r" % (cp, before)
        )
        ln = lines[i]
        ln = drop_attr(ln, "SourceValLow")
        ln = drop_attr(ln, "DestValLow")
        ln = set_attr(ln, "SourceValHigh", POWER_SRC_HIGH)
        ln = set_attr(ln, "DestValHigh", POWER_DST_HIGH)
        lines[i] = ln
        log.append(
            "F2  X%d244 active power: correction %.6g x + %.6g -> %.6g x + %.6g (mW->kW)"
            % (cp, before[0], before[1], *linear_map(ln))
        )

    # arithmetic check on whatever is in the file now
    slope, icept = linear_map(lines[i])
    assert abs(slope - 1e-6) < 1e-18, "X%d244 slope is %r, expected 1e-6" % (cp, slope)
    assert abs(icept) < 1e-12, "X%d244 intercept is %r, expected 0" % (cp, icept)
    assert abs(11_000_000 * slope - 11.0) < 1e-9, "11 kW must map to 11"

    # display unit W -> kW
    for j in range(i, end + 1):
        if "<Display" in lines[j] and "&lt;v.3&gt; W" in lines[j]:
            lines[j] = lines[j].replace("&lt;v.3&gt; W", "&lt;v.3&gt; kW")
            log.append("F2  X%d244 display unit: W -> kW" % cp)
            break
    else:
        assert any(
            "&lt;v.3&gt; kW" in lines[j] for j in range(i, end + 1)
        ), "X%d244 display unit is neither W nor kW" % cp


# --------------------------------------------------------------------------
# F3 -- energy counter ceiling
# --------------------------------------------------------------------------


def fix_energy_range(lines: list[str], cp: int, log: list[str]) -> None:
    i = find_line(lines, 'Type="ModbusASensor"', 'ModbusAddress="%d250"' % cp)
    if attr(lines[i], "MinVal") == ENERGY_MIN and attr(lines[i], "MaxVal") == ENERGY_MAX:
        log.append("F3  X%d250 energy counter: range already %s..%s" % (cp, ENERGY_MIN, ENERGY_MAX))
    else:
        old = (attr(lines[i], "MinVal"), attr(lines[i], "MaxVal"))
        lines[i] = set_attr(set_attr(lines[i], "MinVal", ENERGY_MIN), "MaxVal", ENERGY_MAX)
        log.append(
            "F3  X%d250 energy counter: MinVal/MaxVal %s/%s -> %s/%s"
            % (cp, old[0], old[1], ENERGY_MIN, ENERGY_MAX)
        )

    # does the post-F2 active-power sensor need the same widening?
    p = find_line(lines, 'Type="ModbusASensor"', 'ModbusAddress="%d244"' % cp)
    slope, _ = linear_map(lines[p])
    pmax = float(attr(lines[p], "MaxVal"))
    # worst case the CHARX can report on one CP: 3 x 80 A x 253 V ~ 60.7 kW
    worst = 3 * 80 * 253 * 1000 * slope
    log.append(
        "F3  X%d244 active power: post-F2 worst case %.3f kW vs MaxVal %g -> %s"
        % (cp, worst, pmax, "no widening needed" if worst < pmax else "WIDENING NEEDED")
    )
    assert worst < pmax, "X%d244 range too narrow after F2" % cp


# --------------------------------------------------------------------------
# F4 -- drop the X300 / X303 writes
# --------------------------------------------------------------------------


def fix_drop_coil_writes(lines: list[str], cp: int, prefix: str, log: list[str]) -> None:
    for reg, name in ((300, "Charging Release"), (303, "Locking")):
        i = find_line(lines, 'Type="ModbusActor"', 'ModbusAddress="%d%d"' % (cp, reg), required=False)
        if i < 0:
            log.append("F4  X%d%03d %s: already removed" % (cp, reg, name))
            continue
        actor_u = attr(lines[i], "U")
        end = block_end(lines, i)
        del lines[i : end + 1]
        log.append("F4  X%d%03d %s: removed ModbusActor (ModbusCmd=5, write single coil)" % (cp, reg, name))

        # and the OutputRef that points at it, if any
        j = find_line(lines, 'Type="OutputRef"', 'Ref="%s"' % actor_u, required=False)
        if j >= 0:
            title = attr(lines[j], "Title")
            jend = block_end(lines, j)
            del lines[j : jend + 1]
            log.append('F4  X%d%03d: removed OutputRef "%s" on the Program page' % (cp, reg, title))

    # the Wallbox 'allow' *input* is never wired in these projects; outAllow fed
    # the OutputRef we just deleted, so nothing dangles on the Wallbox side.
    w = find_line(lines, 'Type="Wallbox"', 'Title="%s"' % (prefix + "Wallbox" if prefix else "Wallbox"),
                  required=False)
    if w < 0:  # 2-CP file titles them "Wallbox CP1" / "Wallbox CP2"
        w = find_line(lines, 'Type="Wallbox"', 'Title="Wallbox CP%d"' % cp, required=False)
    if w >= 0:
        wend = block_end(lines, w)
        for k in range(w, wend + 1):
            if '<Co K="allow"' in lines[k]:
                assert lines[k].rstrip().endswith("/>"), (
                    "Wallbox 'allow' has a wired <In>; F4 assumed it was empty"
                )
                log.append("F4  Wallbox CP%d: 'allow' input confirmed unwired, 'outAllow' left unconnected" % cp)
                break


# --------------------------------------------------------------------------
# F5 -- clamp X301 and stop two formulas fighting over it
# --------------------------------------------------------------------------


def fix_setpoint(lines: list[str], cp: int, prefix: str, log: list[str]) -> None:
    mono = find_line(lines, 'Type="Formula"', 'Title="%smono"' % prefix)
    mono_aq = connector_uuid(lines, mono, block_end(lines, mono), "AQ")

    actor = find_line(lines, 'Type="ModbusAActor"', 'ModbusAddress="%d301"' % cp)
    ref = find_line(lines, 'Type="OutputRef"', 'Ref="%s"' % attr(lines[actor], "U"))
    ref_end = block_end(lines, ref)

    ai = next(i for i in range(ref, ref_end + 1) if '<Co K="AI"' in lines[i])
    ai_end = block_end(lines, ai)
    ins = [i for i in range(ai, ai_end + 1) if "<In Input=" in lines[i]]
    doomed = [i for i in ins if attr(lines[i], "Input") == mono_aq]
    if doomed:
        for i in reversed(doomed):
            del lines[i]
        ai_end = block_end(lines, ai)
        remaining = sum(1 for i in range(ai, ai_end + 1) if "<In Input=" in lines[i])
        lines[ai] = set_attr(lines[ai], "Nc", str(remaining))
        log.append(
            'F5  X%d301 setpoint: dropped the "%smono" link from OutputRef "%sMax Charging Rate I", Nc %d -> %d'
            % (cp, prefix, prefix, len(ins), remaining)
        )
    else:
        log.append("F5  X%d301 setpoint: 'mono' link already removed (Nc=%s)" % (cp, attr(lines[ai], "Nc")))

    tri = find_line(lines, 'Type="Formula"', 'Title="%s3 fase"' % prefix)
    cur = attr(lines[tri], "Formula")
    if cur in (FORMULA_3F_CLAMPED, FORMULA_3F_TRUNC, FORMULA_3F_PAUSE):
        log.append("F5  X%d301 clamp: '%s3 fase' formula already clamped" % (cp, prefix))
    else:
        assert cur == FORMULA_3F_RAW, "unexpected '%s3 fase' formula: %r" % (prefix, cur)
        lines[tri] = set_attr(lines[tri], "Formula", FORMULA_3F_CLAMPED)
        log.append("F5  X%d301 clamp: '%s3 fase' %s -> %s" % (cp, prefix, cur, FORMULA_3F_CLAMPED))

    # the clamp must actually keep X301 inside the documented 6..80 A window
    for kw in (0.0, 0.5, 4.16, 11.0, 22.0, 100.0):
        raw = (kw * 1000) / (400 * 1.732)
        assert X301_MIN <= min(max(raw, X301_MIN), X301_MAX) <= X301_MAX


# --------------------------------------------------------------------------
# F6 -- CHARX watchdog (X306 fallback current + X307 timer)
# --------------------------------------------------------------------------


def mint(file_text: str, cp: int, seq: int, suffix: str) -> str:
    u = "%s%04x-%s" % (MINT_PREFIX, cp * 0x100 + seq, suffix)
    assert u not in file_text, "minted uuid %s already occurs in the file" % u
    return u


def fix_watchdog(lines: list[str], cp: int, prefix: str, log: list[str]) -> None:
    if find_line(lines, 'ModbusAddress="%d306"' % cp, required=False) >= 0:
        log.append("F6  X%d306/X%d307 watchdog: already present" % (cp, cp))
        return

    blob = NL.join(lines)

    # templates to clone
    src_actor = find_line(lines, 'Type="ModbusAActor"', 'ModbusAddress="%d301"' % cp)
    src_actor_end = block_end(lines, src_actor)
    actor_i_suffix = connector_uuid(lines, src_actor, src_actor_end, "I").split("-", 3)[3]
    io_line = next(l for l in lines[src_actor : src_actor_end + 1] if "<IoData" in l).strip()

    src_ref = find_line(lines, 'Type="OutputRef"', 'Ref="%s"' % attr(lines[src_actor], "U"))
    src_ref_end = block_end(lines, src_ref)
    ref_ai_suffix = connector_uuid(lines, src_ref, src_ref_end, "AI").split("-", 3)[3]
    ref_aq_suffix = connector_uuid(lines, src_ref, src_ref_end, "AQ").split("-", 3)[3]
    ref_py = int(attr(lines[src_ref], "Py"))

    src_fml = find_line(lines, 'Type="Formula"', 'Title="%smono"' % prefix)
    src_fml_end = block_end(lines, src_fml)
    fml_suffix = {
        k: connector_uuid(lines, src_fml, src_fml_end, k).split("-", 3)[3]
        for k in ("Input1", "Input2", "Input3", "Input4", "AQ", "TQ")
    }

    # CP1's Program-page band starts at y 1824 and the Max Charging Rate I
    # OutputRef sits at y 3648; CP2's band is 10000 higher.  Free space is to
    # the right of x 10000 (nothing else on the page reaches past x 9888).
    yoff = ref_py - 3648
    slots = {
        306: dict(fml=(10848, 4320 + yoff, 12192, 5016 + yoff),
                  ref=(12480, 4320 + yoff, 14592, 4512 + yoff)),
        307: dict(fml=(10848, 5280 + yoff, 12192, 5976 + yoff),
                  ref=(12480, 5280 + yoff, 14592, 5472 + yoff)),
    }

    actor_block: list[str] = []
    page_block: list[str] = []
    for n, (reg, title, const, repeat, unit) in enumerate(WATCHDOG):
        iname = "AMQ%d" % (INAME_BASE + (cp - 1) * len(WATCHDOG) + n)
        assert find_line(lines, 'IName="%s"' % iname, required=False) < 0, "IName %s already used" % iname
        base = n * 0x10
        fml_u = mint(blob, cp, base + 0x00, OBJ_SUFFIX)
        fml_co = {k: mint(blob, cp, base + 0x01 + i, fml_suffix[k])
                  for i, k in enumerate(("Input1", "Input2", "Input3", "Input4", "AQ", "TQ"))}
        ref_u = mint(blob, cp, base + 0x07, OBJ_SUFFIX)
        ref_ai = mint(blob, cp, base + 0x08, ref_ai_suffix)
        ref_aq = mint(blob, cp, base + 0x09, ref_aq_suffix)
        act_u = mint(blob, cp, base + 0x0A, OBJ_SUFFIX)
        act_i = mint(blob, cp, base + 0x0B, actor_i_suffix)
        full = prefix + title

        t = "\t" * indent_of(lines[src_actor])
        actor_block += [
            '%s<C Type="ModbusAActor" Desc="CHARX watchdog register X%d - re-sent every %s s by RepeatRate" '
            'IName="%s" V="172" U="%s" Title="%s" Nio="1" WF="16400" Channel="1" SourceValHigh="100" '
            'DestValHigh="100" DimmType="3" ModbusAddress="%d%d" ModbusCmd="6" ModbusCoilQuantity="1" '
            'RepeatRate="%s">' % (t, reg, repeat, iname, act_u, full, cp, reg, repeat),
            '%s\t<Co K="I" Nc="1" U="%s">' % (t, act_i),
            '%s\t\t<In Input="%s"/>' % (t, ref_aq),
            "%s\t</Co>" % t,
            "%s\t%s" % (t, io_line),
            '%s\t<Display Unit="&lt;v&gt; %s" StateOnly="true"/>' % (t, unit),
            "%s</C>" % t,
        ]

        p = "\t" * indent_of(lines[src_ref])
        fx1, fy1, fx2, fy2 = slots[reg]["fml"]
        rx1, ry1, rx2, ry2 = slots[reg]["ref"]
        page_block += [
            '%s<C Type="Formula" V="172" U="%s" Title="%s" Px="%d" Py="%d" Px2="%d" Py2="%d" '
            'Cl="105,195,80" Nio="6" Formula="%s">' % (p, fml_u, full, fx1, fy1, fx2, fy2, const),
        ]
        for k in ("Input1", "Input2", "Input3", "Input4", "AQ", "TQ"):
            page_block.append('%s\t<Co K="%s" U="%s"/>' % (p, k, fml_co[k]))
        page_block += [
            "%s</C>" % p,
            '%s<C Type="OutputRef" V="172" U="%s" Title="%s" Px="%d" Py="%d" Px2="%d" Py2="%d" '
            'Cl="238,238,238" Nio="2" Ref="%s" WF="16384" Analog="true" LinkRefType="156">'
            % (p, ref_u, full, rx1, ry1, rx2, ry2, act_u),
            '%s\t<Co K="AI" Nc="1" U="%s">' % (p, ref_ai),
            '%s\t\t<In Input="%s" CF="true"/>' % (p, fml_co["AQ"]),
            "%s\t</Co>" % p,
            '%s\t<Co K="AQ" U="%s"/>' % (p, ref_aq),
            "%s</C>" % p,
        ]

    lines[src_actor_end + 1 : src_actor_end + 1] = actor_block
    src_ref = find_line(lines, 'Type="OutputRef"', 'Ref="%s"' % attr(lines[src_actor], "U"))
    src_ref_end = block_end(lines, src_ref)
    lines[src_ref_end + 1 : src_ref_end + 1] = page_block

    log.append(
        "F6  X%d306/X%d307 watchdog: added 2 ModbusAActor + 2 constant Formula + 2 OutputRef "
        "(fallback %s A @ RepeatRate 3600 s, timer %s s @ RepeatRate 60 s)"
        % (cp, cp, WATCHDOG[0][2], WATCHDOG[1][2])
    )


# --------------------------------------------------------------------------
# F7 -- validity ranges on every analogue input
# --------------------------------------------------------------------------


def fmt_val(x: float) -> str:
    """Loxone stores MinVal/MaxVal as plain decimal strings; keep them integral."""
    r = round(x)
    return str(int(r)) if abs(x - r) < 1e-6 else repr(x)


def sensor_rows(lines: list[str], cp: int | None = None) -> list[tuple[int, int, str]]:
    """[(line index, register number without the CP offset, title)] for ModbusASensors."""
    rows = []
    for i, ln in enumerate(lines):
        if 'Type="ModbusASensor"' not in ln:
            continue
        a = attr(ln, "ModbusAddress")
        if a is None:
            continue
        n = int(a)
        if cp is not None and n // 1000 != cp:
            continue
        rows.append((i, n % 1000, attr(ln, "Title") or ""))
    return rows


def is_string_register(reg: int) -> bool:
    """True for registers read as Loxone String text (F13's X275): audited as
    "known, not numeric" -- never widened, never reported NOT AUDITED."""
    span = REGISTER_SPAN.get(reg)
    return span is not None and span[0] == "string"


def needed_range(line: str, reg: int) -> tuple[float, float, str] | None:
    """Engineering span this input must be able to show, or None if unknown."""
    span = REGISTER_SPAN.get(reg)
    if span is None or is_string_register(reg):
        return None
    raw_lo, raw_hi, why = span
    slope, icept = linear_map(line)
    a, b = slope * raw_lo + icept, slope * raw_hi + icept
    return min(a, b), max(a, b), why


def range_gap(line: str, reg: int) -> tuple[float, float] | None:
    """(needed lo, needed hi) when MinVal/MaxVal cannot accommodate it, else None."""
    need = needed_range(line, reg)
    if need is None:
        return None
    lo, hi, _ = need
    cur_lo = float(attr(line, "MinVal")) if attr(line, "MinVal") not in (None, "") else float("-inf")
    cur_hi = float(attr(line, "MaxVal")) if attr(line, "MaxVal") not in (None, "") else float("inf")
    tol = 1e-9 * max(1.0, abs(lo), abs(hi))
    if cur_lo <= lo + tol and hi - tol <= cur_hi:
        return None
    return lo, hi


def fix_sensor_ranges(lines: list[str], cp: int, log: list[str]) -> None:
    """Audit EVERY analogue input, widen the ones that cannot show their register.

    Only inputs whose current MinVal/MaxVal *cannot* carry the physically
    possible value are touched, and those are set to the physical span itself
    (the treatment X250 and X299 already got) -- nothing is ever narrowed.
    """
    unknown, fixed, ok, strings = [], [], 0, []
    for i, reg, title in sensor_rows(lines, cp):
        if is_string_register(reg):
            strings.append("X%d%d %r" % (cp, reg, title))
            continue
        need = needed_range(lines[i], reg)
        if need is None:
            unknown.append("X%d%d %r" % (cp, reg, title))
            continue
        lo, hi, why = need
        gap = range_gap(lines[i], reg)
        if gap is None:
            ok += 1
            continue
        old = (attr(lines[i], "MinVal"), attr(lines[i], "MaxVal"))
        lines[i] = set_attr(set_attr(lines[i], "MinVal", fmt_val(lo)), "MaxVal", fmt_val(hi))
        fixed.append("X%d%d %r: MinVal/MaxVal %s/%s -> %s/%s -- %s"
                     % (cp, reg, title, old[0], old[1], fmt_val(lo), fmt_val(hi), why))
        assert range_gap(lines[i], reg) is None, "X%d%d still cannot show its register" % (cp, reg)

    if fixed:
        for f in fixed:
            log.append("F7  " + f)
    log.append("F7  CP%d validity ranges: %d input(s) widened, %d already fine%s%s"
               % (cp, len(fixed), ok,
                  ", %d String input(s) (text, no numeric range): %s" % (len(strings), strings)
                  if strings else "",
                  ", %d NOT AUDITED (register semantics unknown): %s" % (len(unknown), unknown)
                  if unknown else ""))


# --------------------------------------------------------------------------
# F8 -- X301 truncates instead of rounding past the requested budget
# --------------------------------------------------------------------------


def kw_of(amps: float) -> float:
    return amps * 400 * 1.732 / 1000


def fix_setpoint_truncate(lines: list[str], cp: int, prefix: str, log: list[str]) -> None:
    tri = find_line(lines, 'Type="Formula"', 'Title="%s3 fase"' % prefix)
    cur = attr(lines[tri], "Formula")
    if cur in (FORMULA_3F_TRUNC, FORMULA_3F_PAUSE):
        log.append("F8  X%d301 truncation: '%s3 fase' already truncates (INT)" % (cp, prefix))
    else:
        assert cur == FORMULA_3F_CLAMPED, "unexpected '%s3 fase' formula: %r" % (prefix, cur)
        lines[tri] = set_attr(lines[tri], "Formula", FORMULA_3F_TRUNC)
        log.append("F8  X%d301 truncation: '%s3 fase' %s -> %s" % (cp, prefix, cur, FORMULA_3F_TRUNC))

    # the setpoint may never ask the charger for more power than the Wallbox
    # block budgeted -- except at the 6 A floor, which exists because writing 0
    # withdraws the charging release outright.
    for kw in (0.0, 1.0, 4.16, 7.4, 11.0, 22.0):
        raw = (kw * 1000) / (400 * 1.732)
        out = min(max(int(raw), X301_MIN), X301_MAX)
        assert X301_MIN <= out <= X301_MAX, "X%d301 %.2f kW -> %d A outside 6..80" % (cp, kw, out)
        assert out == X301_MIN or kw_of(out) <= kw + 1e-9, (
            "X%d301 %.2f kW -> %d A = %.3f kW, more than asked" % (cp, kw, out, kw_of(out))
        )


# --------------------------------------------------------------------------
# F9 -- 'mono' guards its divide-by-zero (and gets the F5 clamp + F8 truncation)
# --------------------------------------------------------------------------


def fix_mono_guard(lines: list[str], cp: int, prefix: str, log: list[str]) -> None:
    mono = find_line(lines, 'Type="Formula"', 'Title="%smono"' % prefix)
    cur = attr(lines[mono], "Formula")
    if cur in (FORMULA_MONO_NEW, FORMULA_MONO_PAUSE):
        log.append("F9  '%smono': already guarded/clamped/truncated" % prefix)
    else:
        assert cur in (FORMULA_MONO_RAW, FORMULA_MONO_PREV), \
            "unexpected '%smono' formula: %r" % (prefix, cur)
        lines[mono] = set_attr(lines[mono], "Formula", FORMULA_MONO_NEW)
        log.append("F9  '%smono': %s -> %s (I2 = measured Voltage L1; it reads 0 whenever "
                   "the charger is unreachable or the contactor is open)"
                   % (prefix, cur, FORMULA_MONO_NEW))

    # 'mono' is shipped unwired (F5); README tells single-phase installers to swap
    # to it, so it has to be safe before they do.
    for volts, kw in ((0.0, 7.4), (0.0, 0.0), (95.0, 7.4), (230.0, 0.0), (230.0, 3.7),
                      (230.0, 7.4), (253.0, 22.0)):
        if volts > MONO_V_FLOOR:
            out = min(max(int((kw * 1000) / volts), X301_MIN), X301_MAX)
            assert out == X301_MIN or out * volts / 1000 <= kw + 1e-9, (
                "'%smono' %.0f V / %.2f kW -> %d A asks for more than budgeted" % (prefix, volts, kw, out)
            )
        else:
            out = X301_MIN  # no division at all
        assert X301_MIN <= out <= X301_MAX, (
            "'%smono' %.0f V / %.2f kW -> %d A outside 6..80" % (prefix, volts, kw, out)
        )


# --------------------------------------------------------------------------
# F11 -- pause: target power 0 -> X301 = 0 (release withdrawn), else the clamp
# --------------------------------------------------------------------------


def model_3f(kw: float) -> int:
    """Python model of FORMULA_3F_PAUSE."""
    if not kw > 0:
        return 0
    return min(max(int((kw * 1000) / (400 * 1.732)), X301_MIN), X301_MAX)


def model_mono(kw: float, volts: float) -> int:
    """Python model of FORMULA_MONO_PAUSE."""
    if not kw > 0:
        return 0
    if volts > MONO_V_FLOOR:
        return min(max(int((kw * 1000) / max(volts, MONO_V_FLOOR)), X301_MIN), X301_MAX)
    return X301_MIN


# Loxone formula text -> value, so the self-check runs the formula AS STORED,
# not only the Python model of it.  Otherwise a typo in FORMULA_3F_PAUSE /
# FORMULA_MONO_PAUSE would pass the model check and ship.  Dependency-free:
# the stored text is normalised (XML-unescape, `1,732` -> `1.732`, `;` -> `,`,
# IF/MIN/MAX/INT -> Python callables) and eval()ed in an empty-builtins
# namespace, after a whitelist proves nothing but those tokens is in it.
_FORMULA_IDENTS = {"IF", "MIN", "MAX", "INT", "ABS"}
_FORMULA_ALLOWED_RE = re.compile(r"^[\sA-Za-z0-9_.,;()+\-*/<>=!]*$")
_FORMULA_NUM_RE = re.compile(r"(?<![A-Za-z_0-9])(\d+),(\d+)")


def eval_stored_formula(text: str, inputs: dict[str, float]) -> float:
    """Evaluate a Loxone `Formula=` attribute value with the given I1..In.

    Both IF branches are evaluated EAGERLY (Python call semantics), which is
    the stricter of the two undocumented Loxone semantics -- a formula that
    survives it survives the lazy one too."""
    src = html.unescape(text)
    if not _FORMULA_ALLOWED_RE.match(src):
        raise AssertionError("formula has a character outside the whitelist: %r" % text)
    src = _FORMULA_NUM_RE.sub(r"\1.\2", src)     # comma decimal -> dot
    src = src.replace(";", ",")                   # argument separator
    src = src.replace("<>", "!=")
    idents = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", src))
    unknown = {i for i in idents if i.upper() not in _FORMULA_IDENTS and not re.fullmatch(r"I\d+", i.upper())}
    if unknown:
        raise AssertionError("formula uses identifier(s) outside the whitelist %s: %r" % (sorted(unknown), text))
    ns = {
        "__builtins__": {},
        "IF": lambda c, a, b: a if c else b,
        "MIN": min, "MAX": max, "INT": int, "ABS": abs,
    }
    ns.update({k.upper(): float(v) for k, v in inputs.items()})
    return float(eval(src, ns, {}))   # noqa: S307 -- whitelisted tokens only


def selfcheck_stored_formulas(lines: list[str], cp: int, prefix: str) -> None:
    """Run the formula TEXT now stored in the file over the same sweep as the
    Python model and require identical outputs.  Fails loudly on any drift."""
    i = find_line(lines, 'Type="Formula"', 'Title="%s3 fase"' % prefix)
    text_3f = attr(lines[i], "Formula")
    assert text_3f == FORMULA_3F_PAUSE, "stored '%s3 fase' text is not FORMULA_3F_PAUSE: %r" % (prefix, text_3f)
    for kw in (0.0, -1.0, 0.5, 1.0, 4.16, 7.4, 11.0, 22.0, 60.0):
        got = eval_stored_formula(text_3f, {"I1": kw})
        want = model_3f(kw)
        assert got == want, (
            "X%d301 '%s3 fase' STORED TEXT %r at %.2f kW -> %r, Python model says %r"
            % (cp, prefix, text_3f, kw, got, want)
        )
    i = find_line(lines, 'Type="Formula"', 'Title="%smono"' % prefix)
    text_mono = attr(lines[i], "Formula")
    assert text_mono == FORMULA_MONO_PAUSE, "stored '%smono' text is not FORMULA_MONO_PAUSE: %r" % (prefix, text_mono)
    for volts in (0.0, 95.0, 100.0, 101.0, 230.0, 253.0):
        for kw in (0.0, -1.0, 1.0, 3.7, 4.16, 7.4, 11.0, 22.0, 60.0):
            got = eval_stored_formula(text_mono, {"I1": kw, "I2": volts})
            want = model_mono(kw, volts)
            assert got == want, (
                "'%smono' STORED TEXT %r at %.0f V / %.2f kW -> %r, Python model says %r"
                % (prefix, text_mono, volts, kw, got, want)
            )


def fix_pause(lines: list[str], cp: int, prefix: str, log: list[str]) -> None:
    """Make Loxone's "pause" actually pause the car.

    The Wallbox block drives outLimit (Tp) to 0 kW whenever charging is paused,
    load-shed or not allowed.  The F5/F8 clamp turned that 0 into the 6 A floor,
    so a "paused" car kept charging at ~4 kW.  Now 0 kW -> X301 = 0, which on a
    CHARX withdraws the charging release; any non-zero target (>= the block's
    4.16 kW minimum-power parameter) keeps the 6..80 A clamp + truncation.
    """
    for title, prev, new in (("3 fase", FORMULA_3F_TRUNC, FORMULA_3F_PAUSE),
                             ("mono", FORMULA_MONO_NEW, FORMULA_MONO_PAUSE)):
        i = find_line(lines, 'Type="Formula"', 'Title="%s%s"' % (prefix, title))
        cur = attr(lines[i], "Formula")
        if cur == new:
            log.append("F11 X%d301 pause: '%s%s' already pauses on 0 kW" % (cp, prefix, title))
            continue
        assert cur == prev, "unexpected '%s%s' formula: %r" % (prefix, title, cur)
        lines[i] = set_attr(lines[i], "Formula", new)
        log.append("F11 X%d301 pause: '%s%s' %s -> %s" % (cp, prefix, title, cur, new))

    # self-check the STORED TEXT against the Python model over the same sweep
    # (a typo in FORMULA_*_PAUSE cannot pass on the model alone)
    selfcheck_stored_formulas(lines, cp, prefix)
    log.append("F11 X%d301 pause: stored '%s3 fase'/'%smono' text evaluates identically to the model"
               % (cp, prefix, prefix))

    # self-check the model: 0 kW is the ONLY way to get 0 A; every non-zero
    # target stays in 6..80 and never asks for more power than budgeted, except
    # at the 6 A floor.
    for kw in (0.0, 1.0, 4.16, 7.4, 11.0, 22.0):
        out = model_3f(kw)
        if kw == 0.0:
            assert out == 0, "X%d301 '3 fase' 0 kW -> %d A, expected 0 (pause)" % (cp, out)
            continue
        assert X301_MIN <= out <= X301_MAX, "X%d301 '3 fase' %.2f kW -> %d A outside 6..80" % (cp, kw, out)
        assert out == X301_MIN or kw_of(out) <= kw + 1e-9, (
            "X%d301 '3 fase' %.2f kW -> %d A = %.3f kW, more than asked" % (cp, kw, out, kw_of(out))
        )
    for volts in (0.0, 95.0, 230.0, 253.0):
        assert model_mono(0.0, volts) == 0, (
            "'%smono' %.0f V / 0 kW -> %d A, expected 0 (pause) regardless of voltage"
            % (prefix, volts, model_mono(0.0, volts))
        )
    for volts, kw in ((0.0, 7.4), (95.0, 7.4), (230.0, 3.7), (230.0, 7.4), (253.0, 22.0),
                      (230.0, 1.0), (230.0, 4.16), (230.0, 11.0)):
        out = model_mono(kw, volts)
        assert X301_MIN <= out <= X301_MAX, (
            "'%smono' %.0f V / %.2f kW -> %d A outside 6..80" % (prefix, volts, kw, out)
        )
        if volts <= MONO_V_FLOOR:
            assert out == X301_MIN, "'%smono' %.0f V / %.2f kW -> %d A, expected the 6 A guard" % (
                prefix, volts, kw, out)
        else:
            assert out == X301_MIN or out * volts / 1000 <= kw + 1e-9, (
                "'%smono' %.0f V / %.2f kW -> %d A asks for more than budgeted" % (prefix, volts, kw, out)
            )


# --------------------------------------------------------------------------
# F12 -- X301 re-sent every 60 s
# --------------------------------------------------------------------------

X301_DESC = ("Set the max amount of amps that the car is able to use; 0 = pause "
             "(charging release withdrawn); re-sent every %s s by RepeatRate" % X301_REPEAT)


def fix_x301_repeat(lines: list[str], cp: int, log: list[str]) -> None:
    """RepeatRate 3600 -> 60 on the X301 actuator, and say so in its Desc.

    Defence against the CHARX firmware issue where an external X301 write can
    revert to 0 shortly after landing, and it keeps both a 0 A pause and a
    setpoint asserted at the same cadence as the X307 watchdog feed.
    """
    i = find_line(lines, 'Type="ModbusAActor"', 'ModbusAddress="%d301"' % cp)
    old_rate, old_desc = attr(lines[i], "RepeatRate"), attr(lines[i], "Desc")
    if old_rate == X301_REPEAT and old_desc == X301_DESC:
        log.append("F12 X%d301 RepeatRate: already %s s" % (cp, X301_REPEAT))
        return
    ln = set_attr(lines[i], "RepeatRate", X301_REPEAT)
    ln = set_attr(ln, "Desc", X301_DESC)
    lines[i] = ln
    log.append("F12 X%d301 RepeatRate: %s -> %s s (Desc updated: %r)" % (cp, old_rate, X301_REPEAT, X301_DESC))


# --------------------------------------------------------------------------
# F13 -- last RFID card UID (X275) -> Wallbox 'user' input
# --------------------------------------------------------------------------

RECT_RE = re.compile(
    r'<C Type="([^"]+)"[^>]*?U="([^"]+)"[^>]*?Px="(-?\d+)" Py="(-?\d+)" Px2="(-?\d+)" Py2="(-?\d+)"'
)


def page_rects(text: str) -> list[tuple[str, str, int, int, int, int]]:
    """[(type, uuid, x1, y1, x2, y2)] of every placed page object."""
    return [
        (m.group(1), m.group(2), int(m.group(3)), int(m.group(4)), int(m.group(5)), int(m.group(6)))
        for m in RECT_RE.finditer(text)
    ]


def rects_overlap(a, b) -> bool:
    return a[2] < b[4] and b[2] < a[4] and a[3] < b[5] and b[3] < a[5]


def next_iname(lines: list[str], kind: str = "AMQ") -> str:
    """The lowest unused internal name above every existing <kind><n>."""
    used = [int(m) for ln in lines for m in re.findall(r'\bIName="%s(\d+)"' % kind, ln)]
    return "%s%d" % (kind, (max(used) if used else 0) + 1)


def input_ref_for(lines: list[str], sensor_u: str) -> int:
    """Line of the InputRef page object that references the given sensor."""
    return find_line(lines, 'Type="InputRef"', 'Ref="%s"' % sensor_u)


def wallbox_for_cp(lines: list[str], cp: int) -> tuple[int, int]:
    """(start, end) of the Wallbox block whose 'power' input is fed by this
    charging point's Active Power (X244) InputRef -- the wiring is the
    identity, not the title, so it works for 'Wallbox' and 'Wallbox CPn' alike."""
    s = find_line(lines, 'Type="ModbusASensor"', 'ModbusAddress="%d244"' % cp)
    r = input_ref_for(lines, attr(lines[s], "U"))
    aq = connector_uuid(lines, r, block_end(lines, r), "AQ")
    for w in (i for i, ln in enumerate(lines) if 'Type="Wallbox"' in ln):
        wend = block_end(lines, w)
        p = next((k for k in range(w, wend + 1) if '<Co K="power"' in lines[k]), None)
        if p is None:
            continue
        pend = block_end(lines, p)
        if any(attr(lines[k], "Input") == aq for k in range(p, pend + 1) if "<In Input=" in lines[k]):
            return w, wend
    raise SystemExit("no Wallbox block is fed by CP%d's Active Power InputRef" % cp)


def rfid_wiring(lines: list[str], cp: int) -> tuple[int, int, str] | None:
    """(sensor line, InputRef line, Wallbox 'user' <In Input> uuid or '') once
    the X275 sensor exists; None when F13 has not been applied at all."""
    s = find_line(lines, 'Type="ModbusASensor"', 'ModbusAddress="%d%d"' % (cp, RFID_REG), required=False)
    if s < 0:
        return None
    r = find_line(lines, 'Type="InputRef"', 'Ref="%s"' % attr(lines[s], "U"), required=False)
    w, wend = wallbox_for_cp(lines, cp)
    u = next(k for k in range(w, wend + 1) if '<Co K="user"' in lines[k])
    uend = block_end(lines, u)
    fed = [attr(lines[k], "Input") for k in range(u, uend + 1) if "<In Input=" in lines[k]]
    return s, r, fed[0] if fed else ""


def fix_rfid_uid(lines: list[str], cp: int, prefix: str, log: list[str]) -> None:
    """Read X275 as a Loxone String and wire it to the Wallbox block's Uid input.

    Mirrors the existing Active Power path exactly: ModbusASensor under the
    Modbus device -> InputRef on the Wallbox page -> the InputRef's AQ feeds the
    Wallbox connector.  Read-only: FC03, no new write (X308 is inert, see the
    RFID_* comment).  The sensor's numeric attributes (MinVal/MaxVal, scaling)
    are cloned from the X299 Vehicle-status input for shape only; a String
    value has no numeric range, and F7 treats X275 accordingly.
    """
    state = rfid_wiring(lines, cp)
    if state is not None:
        s, r, fed = state
        assert r >= 0 and fed, (
            "X%d%d exists but is only partly wired (InputRef %s, Wallbox user %r); "
            "F13 will not repair a half-applied fix" % (cp, RFID_REG, r >= 0, fed)
        )
        assert attr(lines[s], "ModbusDataType") == RFID_DATATYPE and attr(lines[s], "ModbusCmd") == "3", (
            "X%d%d present but not String/FC03" % (cp, RFID_REG)
        )
        log.append("F13 X%d%d last RFID card: already present and wired to the Wallbox 'user' input"
                   % (cp, RFID_REG))
        return

    blob = NL.join(lines)

    # -- sensor: clone the shape of the X299 Vehicle-status input -------------
    src = find_line(lines, 'Type="ModbusASensor"', 'ModbusAddress="%d299"' % cp)
    src_end = block_end(lines, src)
    q_suffix = connector_uuid(lines, src, src_end, "Q").split("-", 3)[3]
    qe_suffix = connector_uuid(lines, src, src_end, "Qe").split("-", 3)[3]
    io_line = next(l for l in lines[src : src_end + 1] if "<IoData" in l).strip()
    iname = next_iname(lines)
    assert find_line(lines, 'IName="%s"' % iname, required=False) < 0, "IName %s already used" % iname
    sens_u = mint(blob, cp, RFID_SEQ_BASE + 0x00, OBJ_SUFFIX)
    sens_q = mint(blob, cp, RFID_SEQ_BASE + 0x01, q_suffix)
    sens_qe = mint(blob, cp, RFID_SEQ_BASE + 0x02, qe_suffix)
    full = prefix + RFID_TITLE
    t = "\t" * indent_of(lines[src])
    sensor_block = [
        '%s<C Type="ModbusASensor" Desc="%s" IName="%s" V="172" U="%s" Title="%s" Nio="2" WF="16400" '
        'ValOT="1" Analog="true" ModbusAddress="%d%d" ModbusDataType="%s" ModbusCmd="3" '
        'ModbusPollingCycle="5" SourceValHigh="%s" DestValHigh="%s" MinVal="%s" MaxVal="%s" '
        'MinChange="%s" MinTime="%s" MaxTime="%s">'
        % (t, RFID_DESC, iname, sens_u, full, cp, RFID_REG, RFID_DATATYPE,
           attr(lines[src], "SourceValHigh"), attr(lines[src], "DestValHigh"),
           attr(lines[src], "MinVal"), attr(lines[src], "MaxVal"),
           attr(lines[src], "MinChange"), attr(lines[src], "MinTime"), attr(lines[src], "MaxTime")),
        '%s\t<Co K="Q" U="%s"/>' % (t, sens_q),
        '%s\t<Co K="Qe" U="%s"/>' % (t, sens_qe),
        "%s\t%s" % (t, io_line),
        '%s\t<Display Type="2" Unit="&lt;v&gt;" StateOnly="true"/>' % t,   # text: no unit, no decimals
        "%s</C>" % t,
    ]
    assert attr(sensor_block[0], "SourceValHigh") == attr(sensor_block[0], "DestValHigh"), "identity scaling"

    # -- InputRef on the Wallbox page: same column as Active Power / Energy ----
    src_ref = input_ref_for(lines, attr(lines[src], "U"))            # attribute shape from X299's
    src_ref_end = block_end(lines, src_ref)
    ref_suffix = {k: connector_uuid(lines, src_ref, src_ref_end, k).split("-", 3)[3]
                  for k in ("AI", "I", "AQ", "Q")}
    e = find_line(lines, 'Type="ModbusASensor"', 'ModbusAddress="%d250"' % cp)
    col_ref = input_ref_for(lines, attr(lines[e], "U"))             # geometry: right below Energy
    x1, x2 = int(attr(lines[col_ref], "Px")), int(attr(lines[col_ref], "Px2"))
    y1 = int(attr(lines[col_ref], "Py2"))
    y2 = y1 + int(attr(lines[col_ref], "Py2")) - int(attr(lines[col_ref], "Py"))
    ref_u = mint(blob, cp, RFID_SEQ_BASE + 0x03, OBJ_SUFFIX)
    ref_co = {k: mint(blob, cp, RFID_SEQ_BASE + 0x04 + i, ref_suffix[k])
              for i, k in enumerate(("AI", "I", "AQ", "Q"))}
    new_rect = ("InputRef", ref_u, x1, y1, x2, y2)
    clash = [r for r in page_rects(blob) if rects_overlap(r, new_rect)]
    assert not clash, "F13 InputRef slot %s overlaps %s" % (new_rect[2:], clash[:2])
    p = "\t" * indent_of(lines[src_ref])
    head = lines[src_ref].strip()
    for name, val in (("U", ref_u), ("Title", full), ("Px", str(x1)), ("Py", str(y1)),
                      ("Px2", str(x2)), ("Py2", str(y2)), ("Ref", sens_u)):
        head = set_attr(head, name, val)
    ref_block = [
        p + head,
        '%s\t<Co K="AI" Nc="1" U="%s">' % (p, ref_co["AI"]),
        '%s\t\t<In Input="%s"/>' % (p, sens_q),
        "%s\t</Co>" % p,
        '%s\t<Co K="I" Nc="1" U="%s">' % (p, ref_co["I"]),
        '%s\t\t<In Input="%s"/>' % (p, sens_qe),
        "%s\t</Co>" % p,
        '%s\t<Co K="AQ" U="%s"/>' % (p, ref_co["AQ"]),
        '%s\t<Co K="Q" U="%s"/>' % (p, ref_co["Q"]),
        "%s</C>" % p,
    ]

    # -- insert (sensor first; the page lives further down, so re-find) -------
    lines[src_end + 1 : src_end + 1] = sensor_block
    src_ref = input_ref_for(lines, attr(lines[src], "U"))
    src_ref_end = block_end(lines, src_ref)
    lines[src_ref_end + 1 : src_ref_end + 1] = ref_block

    # -- Wallbox 'user' (Uid) input <- the InputRef's AQ ----------------------
    w, wend = wallbox_for_cp(lines, cp)
    u = next(k for k in range(w, wend + 1) if '<Co K="user"' in lines[k])
    assert lines[u].rstrip().endswith("/>"), "Wallbox CP%d 'user' input is already wired: %s" % (cp, lines[u].strip())
    user_u = attr(lines[u], "U")
    wt = "\t" * indent_of(lines[u])
    lines[u : u + 1] = [
        '%s<Co K="user" Nc="1" U="%s">' % (wt, user_u),
        '%s\t<In Input="%s"/>' % (wt, ref_co["AQ"]),
        "%s</Co>" % wt,
    ]
    wtitle = attr(lines[w], "Title")

    log.append(
        "F13 X%d%d last RFID card: added ModbusASensor %s (String, FC03, poll 5 s) + InputRef at "
        "(%d,%d)-(%d,%d) and wired its AQ to the '%s' block's 'user' (Uid) input -- no new write; "
        "String register count to be confirmed in Loxone Config"
        % (cp, RFID_REG, iname, x1, y1, x2, y2, wtitle)
    )


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------


def verify(path: str, data: bytes) -> list[str]:
    out = []
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        out.append("  [%s] %s" % ("OK " if cond else "FAIL", msg))

    chk(data.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM present")
    body = data[3:]
    lone_lf = len(re.findall(rb"(?<!\r)\n", body))
    lone_cr = len(re.findall(rb"\r(?!\n)", body))
    chk(lone_lf == 0, "lone LF: %d" % lone_lf)
    chk(lone_cr == 0, "lone CR: %d" % lone_cr)

    text = data.decode("utf-8-sig")
    us = re.findall(r'\bU="([^"]*)"', text)
    chk(len(us) == len(set(us)), "U= unique: %d values, %d distinct" % (len(us), len(set(us))))

    uset = set(us)
    dang = {}
    for label, vals in (
        ("<In Input>", re.findall(r'<In Input="([^"]*)"', text)),
        ("Ref=", re.findall(r'\bRef="([^"]*)"', text)),
        ("RefTracker=", re.findall(r'\bRefTracker="([^"]*)"', text)),
    ):
        miss = [v for v in vals if v not in uset]
        dang[label] = (len(vals), miss)
    for label, (total, miss) in dang.items():
        chk(not miss, "%s: %d total, %d dangling %s" % (label, total, len(miss), miss[:3] if miss else ""))

    opens = len(re.findall(r"<C\b", text))
    closes = text.count("</C>")
    selfclose = sum(1 for ln in text.split(NL) if ln.lstrip().startswith("<C ") and ln.rstrip().endswith("/>"))
    chk(opens - selfclose == closes, "<C opens %d (%d self-closing) == </C> closes %d" % (opens, selfclose, closes))

    try:
        ET.fromstring(text.replace("12hTF=", "x12hTF="))
        chk(True, "well-formed XML (12hTF workaround)")
    except ET.ParseError as e:
        chk(False, "XML parse error: %s" % e)

    chk(not HP_SALT_RE.search(text), "every <HP> salt is blank")
    chk(not TF12_RE.search(text), 'no 12hTF attribute (file is well-formed XML)')
    for name in ("APPKEY", "APPID", "Serial"):
        vals = set(re.findall(r'\b%s="([^"]*)"' % name, text))
        chk(vals <= {""}, '%s all empty (values %s)' % (name, sorted(vals)))
    srv = re.findall(r'<C Type="ModbusServer"[^>]*\bAddress="([^"]*)"', text)
    chk(set(srv) <= {""}, "ModbusServer Address empty (%d server(s))" % len(srv))
    hp = re.findall(r"<HP [^>]*/>", text)
    chk(all('S=""' in h for h in hp), "all %d <HP> salts blank" % len(hp))

    vlines = text.split(NL)
    gaps = [
        "X%s %r needs [%s .. %s], has [%s .. %s]"
        % (attr(vlines[i], "ModbusAddress"), title,
           fmt_val(range_gap(vlines[i], reg)[0]), fmt_val(range_gap(vlines[i], reg)[1]),
           attr(vlines[i], "MinVal"), attr(vlines[i], "MaxVal"))
        for i, reg, title in sensor_rows(vlines)
        if range_gap(vlines[i], reg) is not None
    ]
    unaudited = [t for i, reg, t in sensor_rows(vlines) if reg not in REGISTER_SPAN]
    chk(not gaps, "every ModbusASensor range accommodates its register: %d sensor(s), "
                  "%d too narrow %s%s"
                  % (len(sensor_rows(vlines)), len(gaps), gaps[:3],
                     ", %d unaudited %s" % (len(unaudited), unaudited) if unaudited else ""))

    tri = re.findall(r'Type="Formula"[^>]*Title="[^"]*3 fase"[^>]*Formula="([^"]*)"', text)
    chk(bool(tri) and set(tri) == {FORMULA_3F_PAUSE},
        "'3 fase' setpoint pauses on 0 kW + clamped + truncated on all %d charging point(s): %s"
        % (len(tri), set(tri)))
    mono = re.findall(r'Type="Formula"[^>]*Title="[^"]*mono"[^>]*Formula="([^"]*)"', text)
    chk(bool(mono) and set(mono) == {FORMULA_MONO_PAUSE},
        "'mono' pauses on 0 kW + guarded + clamped + truncated on all %d charging point(s): %s"
        % (len(mono), set(mono)))
    # Evaluate the stored formula TEXT (not the Python model) over the F11
    # sweep, so `--check` also catches a typo'd FORMULA_*_PAUSE constant.
    drift = []
    for txt in set(tri):
        for kw in (0.0, -1.0, 0.5, 1.0, 4.16, 7.4, 11.0, 22.0, 60.0):
            try:
                got = eval_stored_formula(txt, {"I1": kw})
            except Exception as e:   # noqa: BLE001 -- report, do not hide
                drift.append("'3 fase' %.2f kW: %s" % (kw, e))
                continue
            if got != model_3f(kw):
                drift.append("'3 fase' %.2f kW -> %r, model %r" % (kw, got, model_3f(kw)))
    for txt in set(mono):
        for volts in (0.0, 95.0, 100.0, 101.0, 230.0, 253.0):
            for kw in (0.0, -1.0, 1.0, 3.7, 4.16, 7.4, 11.0, 22.0, 60.0):
                try:
                    got = eval_stored_formula(txt, {"I1": kw, "I2": volts})
                except Exception as e:   # noqa: BLE001
                    drift.append("'mono' %.0f V / %.2f kW: %s" % (volts, kw, e))
                    continue
                if got != model_mono(kw, volts):
                    drift.append("'mono' %.0f V / %.2f kW -> %r, model %r" % (volts, kw, got, model_mono(kw, volts)))
    chk(bool(tri) and bool(mono) and not drift,
        "stored '3 fase'/'mono' formula TEXT evaluates identically to the Python model "
        "over the F11 sweep (%d '3 fase' + %d 'mono' text(s)): %s"
        % (len(set(tri)), len(set(mono)), drift[:3] if drift else "no drift"))
    x301 = [ln for ln in vlines if 'Type="ModbusAActor"' in ln
            and re.search(r'\bModbusAddress="\d301"', ln)]
    rates = [attr(ln, "RepeatRate") for ln in x301]
    chk(bool(x301) and set(rates) == {X301_REPEAT},
        "X301 actuator RepeatRate=%s on all %d charging point(s): %s" % (X301_REPEAT, len(x301), rates))

    addrs = sorted(int(a) for a in re.findall(r'\bModbusAddress="(\d+)"', text))
    out.append("  ModbusAddress values: %s" % addrs)
    for gone in (1300, 1303, 2300, 2303):
        chk(gone not in addrs, "X%d absent" % gone)
    ncp = len({a // 1000 for a in addrs})
    for cp in sorted({a // 1000 for a in addrs}):
        chk(cp * 1000 + 306 in addrs and cp * 1000 + 307 in addrs, "X%d306/X%d307 present" % (cp, cp))
    out.append("  charging points detected: %d" % ncp)

    rects = page_rects(text)
    chk(len(rects) == text.count(' Px="'), "all %d page rectangles parsed" % len(rects))
    overlaps = []
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            a, b = rects[i], rects[j]
            if rects_overlap(a, b):
                overlaps.append((a[0], a[1], b[0], b[1]))
    chk(not overlaps, "page-rectangle overlaps: %d %s" % (len(overlaps), overlaps[:3]))

    # F13 -- every charging point reads X275 as a String and its Wallbox block's
    # 'user' (Uid) input is fed by exactly that sensor's InputRef, and by nothing
    # else; the Wallbox is identified by its 'power' wiring, so a CP1 sensor
    # feeding the CP2 block would be caught too.
    cps = sorted({a // 1000 for a in addrs})
    rfid = []
    for cp in cps:
        try:
            s = find_line(vlines, 'Type="ModbusASensor"', 'ModbusAddress="%d%d"' % (cp, RFID_REG))
            typed = attr(vlines[s], "ModbusDataType") == RFID_DATATYPE and attr(vlines[s], "ModbusCmd") == "3"
            r = find_line(vlines, 'Type="InputRef"', 'Ref="%s"' % attr(vlines[s], "U"))
            aq = connector_uuid(vlines, r, block_end(vlines, r), "AQ")
            w, wend = wallbox_for_cp(vlines, cp)
            u = next(k for k in range(w, wend + 1) if '<Co K="user"' in vlines[k])
            uend = block_end(vlines, u)
            fed = [attr(vlines[k], "Input") for k in range(u, uend + 1) if "<In Input=" in vlines[k]]
            wired = fed == [aq] and attr(vlines[u], "Nc") == "1"
            rfid.append((cp, typed, wired, attr(vlines[w], "Title")))
        except SystemExit as e:
            rfid.append((cp, False, False, str(e)))
    chk(bool(rfid) and all(t and wd for _, t, wd, _ in rfid),
        "F13 X275 last RFID card: String/FC03 sensor + InputRef feeding the Wallbox 'user' (Uid) "
        "input on all %d charging point(s): %s"
        % (len(cps), ["CP%d %s: type %s, wired %s" % (cp, ttl, t, wd) for cp, t, wd, ttl in rfid]))
    nwb = len(re.findall(r'<C Type="Wallbox"', text))
    chk(nwb == len(cps), "one Wallbox block per charging point: %d block(s), %d point(s)" % (nwb, len(cps)))
    x275_writes = [ln for ln in vlines if 'Type="ModbusAActor"' in ln
                   and re.search(r'\bModbusAddress="\d(275|308)"', ln)]
    chk(not x275_writes, "no write to X275/X308 (RFID register is read-only, X308 reset is inert): %d"
        % len(x275_writes))

    out.insert(0, "verify %s (%d bytes): %s" % (path, len(data), "PASS" if ok else "FAIL"))
    return out if ok else out + ["  >>> VERIFICATION FAILED <<<"]


# --------------------------------------------------------------------------


def process(path: str, check_only: bool = False) -> bool:
    lines = read_lines(path)
    log: list[str] = []

    if not check_only:
        fix_salt(lines, log)
        fix_12htf(lines, log)
        for cp, prefix in charging_points(lines):
            fix_active_power(lines, cp, log)
            fix_energy_range(lines, cp, log)
            fix_sensor_ranges(lines, cp, log)
            fix_drop_coil_writes(lines, cp, prefix, log)
            fix_setpoint(lines, cp, prefix, log)
            fix_setpoint_truncate(lines, cp, prefix, log)
            fix_mono_guard(lines, cp, prefix, log)
            fix_pause(lines, cp, prefix, log)
            fix_x301_repeat(lines, cp, log)
            fix_watchdog(lines, cp, prefix, log)
            fix_rfid_uid(lines, cp, prefix, log)

    before = open(path, "rb").read()
    data = (BOM + NL.join(lines)).encode("utf-8")
    changed = data != before
    if changed and not check_only:
        open(path, "wb").write(data)

    print("=" * 78)
    print("%s  (%s)" % (path, "rewritten" if changed else "unchanged"))
    print("=" * 78)
    for line in log:
        print("  " + line)
    print()
    report = verify(path, data)
    print("\n".join(report))
    print()
    return not report[-1].startswith("  >>>")


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    paths = [a for a in argv[1:] if not a.startswith("--")]
    if not paths:
        print(__doc__)
        return 2
    return 0 if all(process(p, check_only) for p in paths) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
