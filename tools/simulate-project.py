#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
simulate-project.py -- a test harness for the Veton Loxone projects.

WHY THIS EXISTS
---------------
`Veton.Loxone` / `Veton-2CP.Loxone` are Loxone Config projects.  Loxone Config
is Windows-only, so on this box nothing can *run* them.  What we CAN do is:

  1. parse the project XML and check that the block graph is coherent
     (every wire resolves, no input driven by two sources, no dangling
     required input, no cycle),
  2. re-implement the small amount of arithmetic Loxone does on the way from
     a Modbus register to the Wallbox block and back out to X301 -- the
     Modbus decode, the two-point value correction, the validity range and
     the `Formula` block language -- and
  3. feed it *real register data* from a CHARX (or the CHARX simulator) and
     check that the engineering values that come out are sane.

WHAT IT DOES NOT PROVE  (read this before trusting a green run)
---------------------------------------------------------------
  * It does NOT prove Loxone Config will open/accept the file.  The projects
    parse as strict XML since 2026-09-03 (the `12hTF` attribute was dropped),
    but only Loxone Config can validate the file.
  * It does NOT prove that a `Formula` block with NO inputs wired actually
    emits its constant.  That is how the X306/X307 watchdog is fed, so it is
    THE key untested assumption of the whole watchdog design.  This harness
    assumes it does (Loxone documents the formula field as an expression that
    is evaluated, and a constant expression has no inputs to wait for), but
    assuming is not measuring.
  * It does NOT prove the `Wallbox` block behaves as documented.  The block is
    closed firmware.  Here it is a stub that records what arrives at its
    inputs and lets you INJECT `outLimit`; the real block's mode logic,
    load-shedding, session handling and the actual kW it asks for are
    entirely outside this harness.
  * It does NOT prove `ModbusDataType="101"` (String) really reads a string,
    nor how many registers Loxone reads for one -- neither project uses a
    string input; the decoder here is a guess with a fixed word count.
  * It does NOT prove Loxone's byte/word order matches ours (big-endian,
    high word first).  No project attribute states it; it matched the real
    charger's data, which is the only evidence we have.
  * It does NOT prove what Loxone does with a value outside MinVal/MaxVal
    (clamp vs. substitute vs. discard).  We report BOTH readings and shout.
  * It does NOT prove `RepeatRate` really re-sends -- the watchdog's whole
    keep-alive story rests on that, and only a Miniserver can show it.

SAFETY
------
This harness NEVER writes to Modbus.  It only ever issues FC03 (read holding
registers).  It is therefore safe to point at the real charger with --host.
`assert_no_writes()` re-checks that promise against the simulator's own log.

USAGE
-----
    tools/simulate-project.py                       # Veton.Loxone vs the sim
    tools/simulate-project.py Veton-2CP.Loxone
    tools/simulate-project.py Veton.Loxone Veton-2CP.Loxone
    tools/simulate-project.py --host <charger-ip> --no-scenarios     # a real charger, read-only (FC03)
    tools/simulate-project.py --no-sim              # static analysis only
    tools/simulate-project.py --outlimit 7.4        # single outLimit instead of the sweep

Exit code 0 = every assertion passed.  Non-zero = at least one failed.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import struct
import os
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO = pathlib.Path(__file__).resolve().parent.parent
# Where the CHARX simulator keeps scenario.txt / writes.log; override with
# --sim-dir or $CHARX_SIM_DIR. No personal paths in a public repo.
DEFAULT_SIM_DIR = pathlib.Path(os.environ.get("CHARX_SIM_DIR", str(REPO / ".sim")))
# No site addresses in a public repo: point --host (or $CHARX_HOST) at a
# simulator or a charger on your own network.
DEFAULT_HOST = os.environ.get("CHARX_HOST", "127.0.0.1")
DEFAULT_SWEEP = [0.0, 1.0, 4.16, 7.4, 11.0, 22.0]

# ---------------------------------------------------------------------------
# 1.  Loxone Modbus data types
# ---------------------------------------------------------------------------
# Extracted from Loxone Config 17's own resource strings: the ModbusDataType
# attribute is 96 + index into this list.  The attribute being ABSENT means
# index 0 (16-bit unsigned) -- Config omits it for the default.
DATATYPES = [
    ("u16", 1),   # 96
    ("s16", 1),   # 97
    ("u32", 2),   # 98
    ("s32", 2),   # 99
    ("f32", 2),   # 100
    ("string", 10),  # 101  <- word count is a GUESS, see module docstring
    ("u64", 4),   # 102
    ("s64", 4),   # 103
    ("f64", 4),   # 104
]


def datatype_of(attr: Optional[str]) -> Tuple[int, str, int]:
    """(code, name, word_count) for a ModbusDataType attribute value."""
    if attr in (None, ""):
        return 96, "u16 (attribute absent -> default)", 1
    code = int(attr)
    idx = code - 96
    if 0 <= idx < len(DATATYPES):
        name, words = DATATYPES[idx]
        return code, name, words
    return code, f"UNKNOWN({code})", 1


def decode(words: Sequence[int], code: int) -> Any:
    """Decode raw registers (big-endian, high word first) per Loxone type."""
    idx = code - 96
    name = DATATYPES[idx][0] if 0 <= idx < len(DATATYPES) else "u16"
    raw = b"".join(struct.pack(">H", w & 0xFFFF) for w in words)
    if name == "u16":
        return struct.unpack(">H", raw[:2])[0]
    if name == "s16":
        return struct.unpack(">h", raw[:2])[0]
    if name == "u32":
        return struct.unpack(">I", raw[:4])[0]
    if name == "s32":
        return struct.unpack(">i", raw[:4])[0]
    if name == "f32":
        return struct.unpack(">f", raw[:4])[0]
    if name == "u64":
        return struct.unpack(">Q", raw[:8])[0]
    if name == "s64":
        return struct.unpack(">q", raw[:8])[0]
    if name == "f64":
        return struct.unpack(">d", raw[:8])[0]
    if name == "string":
        return raw.split(b"\x00", 1)[0].decode("ascii", "replace")
    return struct.unpack(">H", raw[:2])[0]


# ---------------------------------------------------------------------------
# 2.  Two-point value correction + validity range
# ---------------------------------------------------------------------------
def fnum(el_attr: Optional[str], default: float) -> float:
    if el_attr in (None, ""):
        return default
    return float(el_attr)


def correct(raw: float, a: Dict[str, str]) -> Tuple[float, str, Optional[str]]:
    """
    dest = DestValLow + (raw - SourceValLow) * (DestValHigh - DestValLow)
                                             / (SourceValHigh - SourceValLow)
    The Low pair defaults to 0/0 when absent.  Returns (value, human, problem).
    """
    sl = fnum(a.get("SourceValLow"), 0.0)
    sh = fnum(a.get("SourceValHigh"), 0.0)
    dl = fnum(a.get("DestValLow"), 0.0)
    dh = fnum(a.get("DestValHigh"), 0.0)
    if sh == sl:
        return raw, f"identity (SourceValHigh == SourceValLow == {sl})", (
            "SourceValHigh == SourceValLow -> division by zero in the two-point "
            "correction; Loxone's behaviour here is unknown, we pass the raw value"
        )
    gain = (dh - dl) / (sh - sl)
    offset = dl - sl * gain
    val = dl + (raw - sl) * gain
    human = f"x{gain:.10g}" + (f" {offset:+.10g}" if abs(offset) > 1e-12 else "")
    return val, human, None


def range_check(val: float, a: Dict[str, str]) -> Tuple[bool, str]:
    """Return (inside, description).  Absent MinVal/MaxVal = no range."""
    if a.get("MinVal") in (None, "") and a.get("MaxVal") in (None, ""):
        return True, "no range"
    lo = fnum(a.get("MinVal"), float("-inf"))
    hi = fnum(a.get("MaxVal"), float("inf"))
    inside = lo <= val <= hi
    return inside, f"[{lo:g} .. {hi:g}]"


# ---------------------------------------------------------------------------
# 3.  The Loxone `Formula` block language
# ---------------------------------------------------------------------------
# Argument separator ';', decimal separator ','.
#   MIN(MAX((I1*1000)/(400*1,732);6);80)
#   IF(I1>16944;IF(I1<17459;1;0);0)
# Never eval() the raw string -- it is not Python (a comma decimal point makes
# `1,732` a tuple, and `;` is a statement separator).
TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<num>\d+(?:,\d+)?|,\d+)
      | (?P<ident>[A-Za-z_][A-Za-z_0-9]*)
      | (?P<op><=|>=|==|!=|<>|[-+*/^()<>;])
    )
    """,
    re.VERBOSE,
)

FUNCS_1 = {"ABS": abs, "INT": int, "SQRT": lambda x: x ** 0.5}


class FormulaError(Exception):
    pass


class FormulaParser:
    def __init__(self, text: str, inputs: Sequence[Optional[float]]):
        self.text = text
        self.inputs = list(inputs)
        self.toks: List[Tuple[str, str]] = []
        pos = 0
        while pos < len(text):
            m = TOKEN_RE.match(text, pos)
            if not m or m.end() == pos:
                if text[pos:].strip() == "":
                    break
                raise FormulaError(f"cannot tokenise at {pos}: {text[pos:]!r}")
            pos = m.end()
            kind = m.lastgroup
            self.toks.append((kind, m.group(kind)))
        self.i = 0

    # -- token helpers ------------------------------------------------------
    def peek(self) -> Optional[Tuple[str, str]]:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self) -> Tuple[str, str]:
        if self.i >= len(self.toks):
            raise FormulaError("unexpected end of formula")
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, val: str) -> None:
        t = self.take()
        if t[1] != val:
            raise FormulaError(f"expected {val!r}, got {t[1]!r}")

    # -- grammar ------------------------------------------------------------
    def parse(self) -> float:
        v = self.comparison()
        if self.peek() is not None:
            raise FormulaError(f"trailing tokens: {self.toks[self.i:]}")
        return v

    def comparison(self) -> float:
        left = self.additive()
        t = self.peek()
        if t and t[0] == "op" and t[1] in ("<", ">", "<=", ">=", "==", "!=", "<>"):
            op = self.take()[1]
            right = self.additive()
            res = {
                "<": left < right,
                ">": left > right,
                "<=": left <= right,
                ">=": left >= right,
                "==": left == right,
                "!=": left != right,
                "<>": left != right,
            }[op]
            return 1.0 if res else 0.0
        return left

    def additive(self) -> float:
        v = self.multiplicative()
        while True:
            t = self.peek()
            if t and t[0] == "op" and t[1] in "+-":
                op = self.take()[1]
                r = self.multiplicative()
                v = v + r if op == "+" else v - r
            else:
                return v

    def multiplicative(self) -> float:
        v = self.unary()
        while True:
            t = self.peek()
            if t and t[0] == "op" and t[1] in "*/":
                op = self.take()[1]
                r = self.unary()
                if op == "*":
                    v = v * r
                else:
                    if r == 0:
                        # Loxone yields 0 on a divide-by-zero rather than
                        # faulting the block.  Flagged by the caller.
                        raise ZeroDivisionError
                    v = v / r
            else:
                return v

    def unary(self) -> float:
        t = self.peek()
        if t and t[0] == "op" and t[1] in "+-":
            op = self.take()[1]
            v = self.unary()
            return -v if op == "-" else v
        return self.power()

    def power(self) -> float:
        base = self.primary()
        t = self.peek()
        if t and t[0] == "op" and t[1] == "^":
            self.take()
            return base ** self.unary()   # right-associative
        return base

    def primary(self) -> float:
        kind, val = self.take()
        if kind == "num":
            return float(val.replace(",", "."))
        if kind == "op" and val == "(":
            v = self.comparison()
            self.expect(")")
            return v
        if kind == "ident":
            up = val.upper()
            m = re.fullmatch(r"I(\d+)", up)
            if m:
                n = int(m.group(1))
                if not 1 <= n <= len(self.inputs):
                    raise FormulaError(f"{val}: no such input")
                v = self.inputs[n - 1]
                return 0.0 if v is None else float(v)
            if up == "IF":
                self.expect("(")
                cond = self.comparison()
                self.expect(";")
                a = self.comparison()
                self.expect(";")
                b = self.comparison()
                self.expect(")")
                return a if cond != 0 else b
            if up in ("MIN", "MAX"):
                self.expect("(")
                args = [self.comparison()]
                while self.peek() and self.peek()[1] == ";":
                    self.take()
                    args.append(self.comparison())
                self.expect(")")
                return min(args) if up == "MIN" else max(args)
            if up in FUNCS_1:
                self.expect("(")
                a = self.comparison()
                self.expect(")")
                return float(FUNCS_1[up](a))
            raise FormulaError(f"unknown identifier/function {val!r}")
        raise FormulaError(f"unexpected token {val!r}")


def eval_formula(text: str, inputs: Sequence[Optional[float]]) -> Tuple[Optional[float], Optional[str]]:
    try:
        return FormulaParser(text, inputs).parse(), None
    except ZeroDivisionError:
        return None, "division by zero"
    except FormulaError as e:
        return None, str(e)


def formula_inputs_used(text: str) -> List[int]:
    return sorted({int(n) for n in re.findall(r"\bI(\d+)\b", text or "", re.I)})


# ---------------------------------------------------------------------------
# 4.  Project parsing + graph
# ---------------------------------------------------------------------------
OUTPUT_KEYS = {"AQ", "Q", "Qe", "TQ", "OutputAPI"}

# Blocks we model.  Everything else in the file (categories, rooms, weather,
# user management, ...) is scenery.
INTERESTING = {
    "ModbusASensor",
    "ModbusAActor",
    "ModbusActor",
    "ModbusSensor",
    "InputRef",
    "OutputRef",
    "OutputRefLM",
    "Formula",
    "State",
    "Wallbox",
    "Tracker",
}

# Inputs that must be wired for the block to do anything at all.
REQUIRED_INPUTS = {
    "ModbusAActor": ["I"],
    "ModbusActor": ["I"],
    "InputRef": ["AI"],
    "OutputRef": ["AI"],
    "OutputRefLM": ["AI"],
    "State": ["I1"],
}
# Inputs the Veton design is *supposed* to drive (softer than required).
EXPECTED_INPUTS = {"Wallbox": ["connected", "power", "energy", "active"]}


@dataclass
class Conn:
    key: str
    uuid: str
    is_output: bool
    sources: List[str] = field(default_factory=list)   # uuids of driving outputs
    attrs: Dict[str, str] = field(default_factory=dict)


@dataclass
class Obj:
    uuid: str
    type: str
    title: str
    attrs: Dict[str, str]
    conns: List[Conn]
    cp: Optional[int] = None

    def conn(self, key: str) -> Optional[Conn]:
        for c in self.conns:
            if c.key == key:
                return c
        return None

    @property
    def label(self) -> str:
        return f"{self.title or '(untitled)'} [{self.type}]"


def is_output_key(objtype: str, key: str, has_in: bool) -> bool:
    if has_in:
        return False
    if objtype == "Wallbox":
        return key.startswith("out") or key == "OutputAPI"
    return key in OUTPUT_KEYS


def load_project(path: pathlib.Path) -> ET.Element:
    """
    UTF-8 with BOM, CRLF, and NOT well-formed: line 3 carries an attribute
    named `12hTF` (XML names may not start with a digit).  Loxone Config
    accepts it.  We rename it on an in-memory copy only -- the file on disk is
    never rewritten.
    """
    raw = path.read_bytes().decode("utf-8-sig")
    return ET.fromstring(raw.replace("12hTF=", "x12hTF="))


def build_graph(root: ET.Element) -> Tuple[List[Obj], Dict[str, Obj], Dict[str, Tuple[Obj, Conn]]]:
    objs: List[Obj] = []
    by_uuid: Dict[str, Obj] = {}
    conn_index: Dict[str, Tuple[Obj, Conn]] = {}   # every connector uuid in the file

    # Pass 1: index EVERY <C>, so Ref=/In= resolution can see the whole file
    # (an <In> may legitimately point at a block type we do not model).
    for c in root.iter("C"):
        t = c.get("Type", "")
        o = Obj(
            uuid=c.get("U", ""),
            type=t,
            title=c.get("Title", ""),
            attrs=dict(c.attrib),
            conns=[],
        )
        for co in c.findall("Co"):
            ins = co.findall("In")
            key = co.get("K", "")
            conn = Conn(
                key=key,
                uuid=co.get("U", ""),
                is_output=is_output_key(t, key, bool(ins)),
                sources=[i.get("Input", "") for i in ins],
                attrs=dict(co.attrib),
            )
            o.conns.append(conn)
        objs.append(o)
        if o.uuid:
            by_uuid[o.uuid] = o
        for conn in o.conns:
            if conn.uuid:
                conn_index[conn.uuid] = (o, conn)
    return objs, by_uuid, conn_index


# ---------------------------------------------------------------------------
# 5.  Graph health
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    severity: str      # FAIL / WARN / INFO
    kind: str
    where: str
    detail: str


def graph_health(objs: List[Obj], by_uuid, conn_index) -> List[Finding]:
    out: List[Finding] = []
    interesting = [o for o in objs if o.type in INTERESTING]

    # who consumes each output connector
    consumers: Dict[str, List[str]] = defaultdict(list)
    for o in objs:
        for c in o.conns:
            for s in c.sources:
                consumers[s].append(f"{o.label}.{c.key}")

    # a) unresolved <In Input=...>
    for o in objs:
        for c in o.conns:
            for s in c.sources:
                if s not in conn_index and s not in by_uuid:
                    out.append(Finding("FAIL", "unresolved <In>", f"{o.label}.{c.key}",
                                       f"Input={s} matches no connector or object"))

    # b) unresolved Ref=
    for o in objs:
        ref = o.attrs.get("Ref")
        if ref and ref not in by_uuid and ref not in conn_index:
            out.append(Finding("FAIL", "unresolved Ref=", o.label,
                               f"Ref={ref} matches no object"))

    # c) two sources on one input
    for o in objs:
        for c in o.conns:
            if len(c.sources) > 1:
                names = []
                for s in c.sources:
                    src = conn_index.get(s)
                    names.append(f"{src[0].label}.{src[1].key}" if src else s)
                out.append(Finding("FAIL", "multi-driven input", f"{o.label}.{c.key}",
                                   "driven by " + " AND ".join(names)))

    # d) required input not wired
    for o in interesting:
        for key in REQUIRED_INPUTS.get(o.type, []):
            c = o.conn(key)
            if c is None:
                out.append(Finding("FAIL", "missing input connector", o.label,
                                   f"no connector {key!r} at all"))
            elif not c.sources:
                out.append(Finding("FAIL", "required input unconnected", f"{o.label}.{key}",
                                   "block cannot produce anything"))
        for key in EXPECTED_INPUTS.get(o.type, []):
            c = o.conn(key)
            if c is not None and not c.sources:
                out.append(Finding("WARN", "expected input unconnected", f"{o.label}.{key}",
                                   "the Veton design drives this"))
        if o.type == "Formula":
            used = formula_inputs_used(o.attrs.get("Formula", ""))
            for n in used:
                c = o.conn(f"Input{n}")
                if c is None or not c.sources:
                    out.append(Finding("FAIL", "formula input unconnected",
                                       f"{o.label}.Input{n}",
                                       f"formula {o.attrs.get('Formula','')!r} reads I{n}; "
                                       f"Loxone will substitute 0"))
            # Robustness probe: an analogue input reads 0 whenever the charger is
            # unreachable or the register is genuinely 0.  A formula that faults
            # there is a live hazard, not a style issue.
            if used:
                _, err0 = eval_formula(o.attrs.get("Formula", ""), [0.0] * 4)
                if err0:
                    out.append(Finding("WARN", "formula faults on all-zero inputs", o.label,
                                       f"{o.attrs.get('Formula','')!r} -> {err0}; an input "
                                       f"reads 0 whenever the charger is unreachable"))
            if not used and not any(c.sources for c in o.conns if not c.is_output):
                out.append(Finding("INFO", "constant formula (no inputs)", o.label,
                                   f"Formula={o.attrs.get('Formula','')!r} -- KEY UNTESTED "
                                   f"ASSUMPTION: that a Formula with nothing wired emits its "
                                   f"constant.  This is what feeds the watchdog."))

    # e) dangling outputs
    for o in interesting:
        if o.type == "Wallbox":
            continue     # dozens of unused outputs is normal for the block
        for c in o.conns:
            if c.is_output and not consumers.get(c.uuid):
                sev = "WARN"
                if c.key == "Qe" or (o.type == "Formula" and c.key == "TQ"):
                    sev = "INFO"     # secondary/error outputs are routinely unused
                out.append(Finding(sev, "output goes nowhere", f"{o.label}.{c.key}",
                                   "no block consumes this output"))

    # f) cycles (object level)
    edges: Dict[str, List[str]] = defaultdict(list)
    for o in objs:
        for c in o.conns:
            for s in c.sources:
                src = conn_index.get(s)
                if src and src[0].uuid and o.uuid:
                    edges[src[0].uuid].append(o.uuid)
    WHITE, GREY, BLACK = 0, 1, 2
    color = defaultdict(int)
    stack: List[str] = []

    def dfs(u: str):
        color[u] = GREY
        stack.append(u)
        for v in edges.get(u, []):
            if color[v] == GREY:
                i = stack.index(v)
                names = [by_uuid[x].label for x in stack[i:] if x in by_uuid]
                out.append(Finding("FAIL", "cycle", " -> ".join(names) + " -> (back)",
                                   "feedback loop in the block graph"))
            elif color[v] == WHITE:
                dfs(v)
        stack.pop()
        color[u] = BLACK

    sys.setrecursionlimit(10000)
    for o in objs:
        if o.uuid and color[o.uuid] == WHITE:
            dfs(o.uuid)

    return out


def assign_charging_points(objs: List[Obj], conn_index) -> None:
    """Label blocks CP1/CP2/... -- seeded from ModbusAddress // 1000, then
    spread along the wires.  Works for the 1-CP file too (everything -> CP1)."""
    adj: Dict[str, set] = defaultdict(set)
    for o in objs:
        for c in o.conns:
            for s in c.sources:
                src = conn_index.get(s)
                if src:
                    adj[o.uuid].add(src[0].uuid)
                    adj[src[0].uuid].add(o.uuid)
    by_uuid = {o.uuid: o for o in objs if o.uuid}
    frontier = []
    for o in objs:
        addr = o.attrs.get("ModbusAddress")
        if addr:
            o.cp = int(addr) // 1000
            frontier.append(o.uuid)
    seen = set(frontier)
    while frontier:
        nxt = []
        for u in frontier:
            for v in adj.get(u, ()):
                if v in seen or v not in by_uuid:
                    continue
                if by_uuid[v].cp is None:
                    by_uuid[v].cp = by_uuid[u].cp
                    seen.add(v)
                    nxt.append(v)
        frontier = nxt


# ---------------------------------------------------------------------------
# 6.  Modbus reading (FC03 ONLY -- this harness never writes)
# ---------------------------------------------------------------------------
class Reader:
    """Reads holding registers.  `offline` mode returns None for everything."""

    def __init__(self, host: str, port: int, unit: int, offline: bool):
        self.offline = offline
        self.client = None
        if offline:
            return
        from pymodbus.client import ModbusTcpClient   # noqa: import-outside-toplevel
        self.client = ModbusTcpClient(host, port=port, timeout=3)
        if not self.client.connect():
            raise SystemExit(f"cannot connect to Modbus/TCP {host}:{port}")
        self.unit = unit

    def read(self, addr: int, count: int) -> Optional[List[int]]:
        if self.offline:
            return None
        rr = self.client.read_holding_registers(addr, count=count, device_id=self.unit)
        if rr.isError():
            return None
        return list(rr.registers)

    def close(self):
        if self.client:
            self.client.close()


# ---------------------------------------------------------------------------
# 7.  Evaluation
# ---------------------------------------------------------------------------
@dataclass
class SensorRead:
    obj: Obj
    addr: int
    words: Optional[List[int]]
    dtype: str
    raw: Any
    value: Optional[float]
    scale: str
    inside: bool
    rng: str
    problem: Optional[str] = None


def read_sensors(objs: List[Obj], rdr: Reader) -> Dict[str, SensorRead]:
    reads: Dict[str, SensorRead] = {}
    for o in objs:
        if o.type not in ("ModbusASensor", "ModbusSensor"):
            continue
        addr = int(o.attrs.get("ModbusAddress", "0"))
        code, dname, words_n = datatype_of(o.attrs.get("ModbusDataType"))
        regs = rdr.read(addr, words_n)
        if regs is None:
            reads[o.uuid] = SensorRead(o, addr, None, dname, None, None, "-", True,
                                       "-", "read failed / offline")
            continue
        rawv = decode(regs, code)
        if isinstance(rawv, str):
            reads[o.uuid] = SensorRead(o, addr, regs, dname, rawv, None, "-", True,
                                       "-", "string type: not an analogue value")
            continue
        val, scale, prob = correct(float(rawv), o.attrs)
        inside, rng = range_check(val, o.attrs)
        reads[o.uuid] = SensorRead(o, addr, regs, dname, rawv, val, scale, inside, rng, prob)
    return reads


@dataclass
class EvalResult:
    values: Dict[str, Optional[float]]                  # connector uuid -> value
    wallbox_inputs: Dict[str, Dict[str, Optional[float]]]   # wallbox title -> {key: value}
    writes: List[Dict[str, Any]]                        # what would go on the wire
    notes: List[str]


def evaluate(objs: List[Obj], conn_index, reads: Dict[str, SensorRead],
             outlimit: float,
             outlimit_by_wallbox: Optional[Dict[str, float]] = None) -> EvalResult:
    """Propagate values from the sensors to the actuators.

    The Wallbox is a stub: its inputs are recorded, its `outLimit` output is
    injected from the CLI, and every other Wallbox output stays None.

    `outlimit_by_wallbox` (Wallbox block uuid -> kW) overrides the uniform
    `outlimit` per block.  Injecting a DIFFERENT value into every Wallbox is
    the only way to see a cross-wired project: with one shared value, a
    'CP2 3 fase' that reads Wallbox CP1's outLimit writes exactly what a
    correctly wired one would."""
    vals: Dict[str, Optional[float]] = {}
    notes: List[str] = []

    # seed: sensors
    for o in objs:
        if o.uuid in reads:
            r = reads[o.uuid]
            q = o.conn("Q")
            qe = o.conn("Qe")
            if q:
                vals[q.uuid] = r.value
            if qe:
                vals[qe.uuid] = 0.0 if r.words is not None else 1.0

    # seed: wallbox outputs
    wallboxes = [o for o in objs if o.type == "Wallbox"]
    for wb in wallboxes:
        c = wb.conn("outLimit")
        if c:
            vals[c.uuid] = outlimit
            if outlimit_by_wallbox and wb.uuid in outlimit_by_wallbox:
                vals[c.uuid] = outlimit_by_wallbox[wb.uuid]

    def src_val(conn: Conn) -> Optional[float]:
        if not conn.sources:
            return None
        v = vals.get(conn.sources[0])
        # An <In> may point at a *block* uuid rather than a connector uuid
        # (Loxone does this for ref-style links); fall through to None.
        return v

    # fixed-point propagation (graph is acyclic; +2 passes of slack)
    for _ in range(len(objs) + 2):
        changed = False

        def put(conn: Optional[Conn], v: Optional[float]):
            nonlocal changed
            if conn is None:
                return
            if vals.get(conn.uuid) != v:
                vals[conn.uuid] = v
                changed = True

        for o in objs:
            t = o.type
            if t in ("InputRef",):
                put(o.conn("AQ"), src_val(o.conn("AI")) if o.conn("AI") else None)
                put(o.conn("Q"), src_val(o.conn("I")) if o.conn("I") else None)
            elif t in ("OutputRef", "OutputRefLM"):
                put(o.conn("AQ"), src_val(o.conn("AI")) if o.conn("AI") else None)
            elif t == "State":
                put(o.conn("AQ"), src_val(o.conn("I1")) if o.conn("I1") else None)
                v = src_val(o.conn("I1")) if o.conn("I1") else None
                put(o.conn("TQ"), None if v is None else (1.0 if v != 0 else 0.0))
            elif t == "Formula":
                text = o.attrs.get("Formula", "")
                ins = []
                for n in range(1, 5):
                    c = o.conn(f"Input{n}")
                    ins.append(src_val(c) if c else None)
                res, err = eval_formula(text, ins)
                if err:
                    notes.append(f"{o.label}: formula {text!r} -> {err}")
                put(o.conn("AQ"), res)
                put(o.conn("TQ"), None if res is None else (1.0 if res != 0 else 0.0))
        if not changed:
            break

    # wallbox inputs
    wb_in: Dict[str, Dict[str, Optional[float]]] = {}
    for wb in wallboxes:
        d = {}
        for key in ("allow", "prio", "connected", "power", "energy", "active"):
            c = wb.conn(key)
            d[key] = src_val(c) if c else None
        wb_in[wb.title or wb.uuid] = d

    # actuator writes
    writes = []
    for o in objs:
        if o.type not in ("ModbusAActor", "ModbusActor"):
            continue
        c = o.conn("I")
        v = src_val(c) if c else None
        reg = None
        scale = "-"
        if v is not None:
            reg, scale, _ = correct(v, o.attrs)
        writes.append({
            "obj": o,
            "addr": int(o.attrs.get("ModbusAddress", "0")),
            "fc": o.attrs.get("ModbusCmd", "?"),
            "input": v,
            "register": reg,
            "written": None if reg is None else int(round(reg)),
            "scale": scale,
            "repeat": o.attrs.get("RepeatRate"),
            "cp": o.cp,
        })
    return EvalResult(vals, wb_in, writes, notes)


# ---------------------------------------------------------------------------
# 8.  Simulator control
# ---------------------------------------------------------------------------
class Sim:
    def __init__(self, sim_dir: pathlib.Path, enabled: bool):
        self.dir = sim_dir
        self.enabled = enabled and sim_dir.is_dir()
        self.scenario = sim_dir / "scenario.txt"
        self.writelog = sim_dir / "writes.log"
        self.baseline = 0
        if self.enabled and self.writelog.exists():
            self.baseline = len(self.writelog.read_text(errors="replace").splitlines())

    def set(self, line: str, settle: float = 1.6) -> bool:
        if not self.enabled:
            return False
        self.scenario.write_text(line + "\n")
        time.sleep(settle)
        return True

    def new_log_lines(self) -> List[str]:
        if not self.enabled or not self.writelog.exists():
            return []
        return self.writelog.read_text(errors="replace").splitlines()[self.baseline:]


# ---------------------------------------------------------------------------
# 9.  Output helpers
# ---------------------------------------------------------------------------
def hr(ch="=", n=100):
    print(ch * n)


def head(title: str):
    print()
    hr()
    print(title)
    hr()


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    if not rows:
        print("  (none)")
        return
    cols = [[str(h)] + [str(r[i]) for r in rows] for i, h in enumerate(headers)]
    w = [max(len(x) for x in col) for col in cols]
    print("  " + "  ".join(h.ljust(w[i]) for i, h in enumerate(headers)))
    print("  " + "  ".join("-" * w[i] for i in range(len(headers))))
    for r in rows:
        print("  " + "  ".join(str(r[i]).ljust(w[i]) for i in range(len(headers))))


def fmt(v: Optional[float], nd=4) -> str:
    if v is None:
        return "-"
    if isinstance(v, str):
        return v
    return f"{v:.{nd}g}"


# ---------------------------------------------------------------------------
# 10.  Assertions
# ---------------------------------------------------------------------------
class Asserts:
    def __init__(self):
        self.rows: List[Tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        return bool(ok)

    @property
    def failed(self) -> List[Tuple[str, bool, str]]:
        return [r for r in self.rows if not r[1]]

    def report(self):
        head("ASSERTIONS")
        table(["result", "assertion", "detail"],
              [("PASS" if ok else "FAIL", n, d) for n, ok, d in self.rows])
        print()
        print(f"  {len(self.rows) - len(self.failed)}/{len(self.rows)} passed")


STATE_CODES = {
    "A1": 0x4131, "A2": 0x4132,
    "B1": 0x4231, "B2": 0x4232,
    "C1": 0x4331, "C2": 0x4332,
    "D1": 0x4431, "D2": 0x4432,
    "E0": 0x4530, "F0": 0x4630,
    "IN": 0x494E,
}
VC_ON = {"B1", "B2", "C1", "C2", "D1", "D2"}
CAC_ON = {"C1", "C2", "D1", "D2"}


# ---------------------------------------------------------------------------
# 11.  Per-project run
# ---------------------------------------------------------------------------
def run_project(path: pathlib.Path, args, sim: Sim, A: Asserts) -> None:
    head(f"PROJECT  {path.name}")

    root = load_project(path)
    objs, by_uuid, conn_index = build_graph(root)
    assign_charging_points(objs, conn_index)
    interesting = [o for o in objs if o.type in INTERESTING]
    cps = sorted({o.cp for o in objs if o.cp is not None})
    print(f"  {len(objs)} objects parsed, {len(interesting)} modelled, "
          f"charging point(s): {cps or ['-']}")

    tag = path.name

    # ---- 11a. inventory --------------------------------------------------
    head(f"{tag}: MODBUS I/O INVENTORY")
    rows = []
    for o in objs:
        if o.type not in ("ModbusASensor", "ModbusSensor", "ModbusAActor", "ModbusActor"):
            continue
        code, dname, words_n = datatype_of(o.attrs.get("ModbusDataType"))
        _, scale, prob = correct(1.0, o.attrs)
        _, rng = range_check(0.0, o.attrs)
        rows.append([
            f"CP{o.cp}" if o.cp else "-",
            o.type.replace("Modbus", ""),
            o.title,
            o.attrs.get("ModbusAddress", "-"),
            f"FC{o.attrs.get('ModbusCmd', '?')}",
            f"{dname}/{words_n}w",
            o.attrs.get("ModbusPollingCycle", "-"),
            o.attrs.get("RepeatRate", "-"),
            scale,
            rng,
        ])
    table(["CP", "kind", "title", "reg", "fc", "type", "poll", "repeat", "correction", "range"], rows)

    # ---- 11b. graph health ----------------------------------------------
    head(f"{tag}: GRAPH HEALTH")
    findings = graph_health(objs, by_uuid, conn_index)
    order = {"FAIL": 0, "WARN": 1, "INFO": 2}
    findings.sort(key=lambda f: (order[f.severity], f.kind, f.where))
    table(["sev", "kind", "where", "detail"],
          [[f.severity, f.kind, f.where, f.detail] for f in findings])
    n_fail = sum(1 for f in findings if f.severity == "FAIL")
    unresolved = [f for f in findings if f.kind in ("unresolved <In>", "unresolved Ref=")]
    A.check(f"{tag}: every <In>/Ref= resolves", not unresolved,
            f"{len(unresolved)} unresolved")
    A.check(f"{tag}: no input driven by two sources",
            not [f for f in findings if f.kind == "multi-driven input"], "")
    A.check(f"{tag}: no cycle in the block graph",
            not [f for f in findings if f.kind == "cycle"], "")
    A.check(f"{tag}: no required input left unconnected",
            not [f for f in findings if f.kind in
                 ("required input unconnected", "missing input connector",
                  "formula input unconnected")], "")

    # ---- 11c. safety: no X300/X303, no FC05 ------------------------------
    head(f"{tag}: WRITE-SAFETY SCAN")
    bad_addr, bad_fc = [], []
    for o in objs:
        addr = o.attrs.get("ModbusAddress")
        cmd = o.attrs.get("ModbusCmd")
        if addr and o.type in ("ModbusAActor", "ModbusActor"):
            if int(addr) % 1000 in (300, 303):
                bad_addr.append(f"{o.label} @ {addr}")
        if cmd == "5":
            bad_fc.append(f"{o.label} ModbusCmd=5 (FC05 write coil)")
    actuators = [o for o in objs if o.type in ("ModbusAActor", "ModbusActor")]
    table(["actuator", "register", "FC", "RepeatRate"],
          [[o.title, o.attrs.get("ModbusAddress"), o.attrs.get("ModbusCmd"),
            o.attrs.get("RepeatRate", "-")] for o in actuators])
    A.check(f"{tag}: no actuator on X300 / X303", not bad_addr, "; ".join(bad_addr))
    A.check(f"{tag}: no ModbusCmd=5 (FC05 write coil) anywhere", not bad_fc, "; ".join(bad_fc))

    # ---- 11d. Vc / Cac truth table (exhaustive, sim-independent) ---------
    head(f"{tag}: Vc / Cac TRUTH TABLE (all IEC 61851 states)")
    vc_forms = [o for o in objs if o.type == "Formula" and "connected" in (o.title or "").lower()]
    cac_forms = [o for o in objs if o.type == "Formula" and "charging active" in (o.title or "").lower()]
    rows, vc_ok, cac_ok = [], True, True
    for st, code in STATE_CODES.items():
        row = [st, code]
        for f in vc_forms:
            v, _ = eval_formula(f.attrs.get("Formula", ""), [code, None, None, None])
            want = 1.0 if st in VC_ON else 0.0
            row.append(f"{fmt(v)}{'' if v == want else '  <-- WANT ' + fmt(want)}")
            vc_ok &= (v == want)
        for f in cac_forms:
            v, _ = eval_formula(f.attrs.get("Formula", ""), [code, None, None, None])
            want = 1.0 if st in CAC_ON else 0.0
            row.append(f"{fmt(v)}{'' if v == want else '  <-- WANT ' + fmt(want)}")
            cac_ok &= (v == want)
        rows.append(row)
    table(["state", "X299"] + [f.title for f in vc_forms] + [f.title for f in cac_forms], rows)
    A.check(f"{tag}: Vc == 1 exactly for B1/B2/C1/C2/D1/D2, 0 for A/E0/F0/IN",
            vc_ok and len(vc_forms) == len(cps or [1]),
            f"{len(vc_forms)} 'Vehicle connected' formula(s)")
    A.check(f"{tag}: Cac == 1 exactly for C1/C2/D1/D2",
            cac_ok and len(cac_forms) == len(cps or [1]),
            f"{len(cac_forms)} 'Charging active' formula(s)")

    # ---- 11e. X301 sweep over the injected outLimit ----------------------
    head(f"{tag}: X301 SWEEP  (Wallbox outLimit injected, kW -> A)")
    sweep = args.sweep
    rdr_offline = Reader("", 0, 1, offline=True)
    reads_offline = read_sensors(objs, rdr_offline)
    sweep_rows, sweep_ok, sweep_detail = [], True, []
    # per CP: how many sweep rows had outLimit <= 0 AND produced an X301 write
    # of exactly 0.  The pause assertion below counts these -- "0.0 is in the
    # sweep list" proves nothing about what was written.
    pause_hits: Dict[int, int] = defaultdict(int)
    x301_cps: set = set()
    for kw in sweep:
        ev = evaluate(objs, conn_index, reads_offline, kw)
        for w in ev.writes:
            if w["addr"] % 1000 != 301:
                continue
            v = w["written"]
            x301_cps.add(w["cp"])
            # outLimit 0 kW = the Wallbox block says pause / load-shed / not
            # allowed -> X301 must be exactly 0 (CHARX withdraws the charging
            # release).  Anything else -> 6..80 A, never 0.  The formula maps
            # every non-positive target to 0, so the branch is `<= 0`.
            if kw <= 0:
                ok = v == 0
                verdict = "OK (pause)" if ok else f"EXPECTED 0 (pause), got {v}"
                if ok:
                    pause_hits[w["cp"]] += 1
            else:
                ok = v is not None and 6 <= v <= 80
                verdict = "OK" if ok else "OUT OF RANGE 6..80"
            sweep_ok &= ok
            if not ok:
                sweep_detail.append(f"CP{w['cp']} {kw} kW -> {v}")
            sweep_rows.append([f"CP{w['cp']}", f"{kw} kW", fmt(w['input']),
                               fmt(w['register']), v, verdict])
    table(["CP", "outLimit", "formula out [A]", "after correction", "register written", "verdict"],
          sweep_rows)
    A.check(f"{tag}: X301 == 0 for outLimit 0 kW (pause), else in 6..80 A and never 0, "
            f"across the outLimit sweep ({', '.join(str(s) for s in sweep)} kW)",
            sweep_ok and bool(sweep_rows), "; ".join(sweep_detail) or f"{len(sweep_rows)} rows")
    missing_pause = sorted(cp for cp in x301_cps if pause_hits.get(cp, 0) < 1)
    A.check(f"{tag}: every CP's X301 actually WROTE 0 for at least one outLimit <= 0 kW "
            f"row of the sweep (pause exercised, not merely listed)",
            bool(x301_cps) and not missing_pause,
            ("no X301 actuator found" if not x301_cps else
             (f"CP(s) without a 0-write: {missing_pause}" if missing_pause else
              "; ".join(f"CP{cp}: {n} row(s) wrote 0"
                        for cp, n in sorted(pause_hits.items())))))

    # ---- 11e'. cross-wiring: each CP's X301 driven by ITS OWN Wallbox ---
    # A uniform outLimit cannot tell "CP2 3 fase reads Wallbox CP2" from "CP2
    # 3 fase reads Wallbox CP1".  Inject a DISTINCT value per Wallbox (base +
    # 1.0 kW x CP index -> distinct amps after the formula), then require that
    # CP n's X301 write equals what a uniform run at Wallbox n's value would
    # have written for CP n.  Independently, walk the wire graph from each
    # X301 actuator back to the Wallbox block(s) it reaches and require it to
    # be exactly one, carrying the same CP.
    head(f"{tag}: X301 CROSS-WIRING CHECK  (distinct outLimit per Wallbox)")
    wallboxes = [o for o in objs if o.type == "Wallbox"]

    def wallbox_cp(wb: Obj) -> Optional[int]:
        m = re.search(r"\bCP\s*(\d+)\b", wb.title or "", re.I)
        if m:
            return int(m.group(1))
        if len(wallboxes) == 1 and len(cps or [1]) == 1:
            return (cps or [1])[0]
        return wb.cp

    def trace_to_wallboxes(o: Obj, seen: Optional[set] = None) -> List[Obj]:
        """Every Wallbox block reachable upstream of `o` through <In> wires."""
        seen = seen if seen is not None else set()
        if o.uuid in seen:
            return []
        seen.add(o.uuid)
        if o.type == "Wallbox":
            return [o]
        found: List[Obj] = []
        for c in o.conns:
            if c.is_output:
                continue
            for s in c.sources:
                src = conn_index.get(s)
                if src:
                    found.extend(trace_to_wallboxes(src[0], seen))
                elif s in by_uuid:
                    found.extend(trace_to_wallboxes(by_uuid[s], seen))
        return found

    base_kw = 7.4
    inject: Dict[str, float] = {}
    wb_by_cp: Dict[int, Obj] = {}
    for wb in wallboxes:
        cp = wallbox_cp(wb)
        if cp is None:
            continue
        wb_by_cp[cp] = wb
        inject[wb.uuid] = base_kw + 1.0 * cp
    ev_distinct = evaluate(objs, conn_index, reads_offline, base_kw, inject)
    xw_rows, xw_ok, xw_detail = [], True, []
    tr_ok, tr_detail, tr_summary = True, [], []
    for w in ev_distinct.writes:
        if w["addr"] % 1000 != 301:
            continue
        cp = w["addr"] // 1000
        wb = wb_by_cp.get(cp)
        if wb is None:
            xw_ok = False
            xw_detail.append(f"CP{cp}: no Wallbox block titled for CP{cp}")
            xw_rows.append([f"CP{cp}", "-", "-", w["written"], "-", "NO WALLBOX FOR THIS CP"])
            continue
        # what CP n's X301 writes when EVERY Wallbox carries Wallbox n's value
        ev_uniform = evaluate(objs, conn_index, reads_offline, inject[wb.uuid])
        want = next((u["written"] for u in ev_uniform.writes if u["addr"] == w["addr"]), None)
        ok = w["written"] is not None and w["written"] == want
        xw_ok &= ok
        if not ok:
            xw_detail.append(f"CP{cp}: X{w['addr']} wrote {w['written']} but its own "
                             f"'{wb.title}' @ {inject[wb.uuid]} kW implies {want}")
        reached = trace_to_wallboxes(w["obj"])
        names = sorted({r.title or r.uuid for r in reached})
        reached_cps = sorted({wallbox_cp(r) for r in reached})
        t_ok = len(names) == 1 and reached_cps == [cp]
        tr_ok &= t_ok
        tr_summary.append(f"X{w['addr']} <- {', '.join(names) or 'NO Wallbox'}")
        if not t_ok:
            tr_detail.append(f"X{w['addr']} <- {names or 'NO Wallbox'} (CP {reached_cps})")
        xw_rows.append([f"CP{cp}", wb.title, f"{inject[wb.uuid]} kW", w["written"], want,
                        ("OK" if ok else "CROSS-WIRED") + " / trace: " + ", ".join(names or ["-"])])
    table(["CP", "own Wallbox", "injected", "X301 written", "expected from own Wallbox", "verdict"],
          xw_rows)
    A.check(f"{tag}: each CP's X301 is driven by its own Wallbox block",
            xw_ok and bool(xw_rows) and len(inject) == len(wallboxes),
            "; ".join(xw_detail) or
            (f"{len(xw_rows)} X301 actuator(s), injected " +
             ", ".join(f"{wb.title}={inject[wb.uuid]} kW" for wb in wallboxes if wb.uuid in inject)))
    A.check(f"{tag}: each X301 actuator's wire trace reaches exactly one Wallbox, of the same CP",
            tr_ok and bool(xw_rows), "; ".join(tr_detail) or "; ".join(tr_summary))

    # ---- 11f. watchdog chain --------------------------------------------
    head(f"{tag}: WATCHDOG CHAIN (X306 / X307)")
    ev0 = evaluate(objs, conn_index, reads_offline, 0.0)
    wd_rows = []
    for w in ev0.writes:
        if w["addr"] % 1000 in (306, 307):
            wd_rows.append([f"CP{w['cp']}", w["obj"].title, w["addr"], f"FC{w['fc']}",
                            fmt(w["input"]), w["written"], w["repeat"]])
    table(["CP", "actuator", "reg", "fc", "value in", "register", "RepeatRate"], wd_rows)
    x306 = [w for w in ev0.writes if w["addr"] % 1000 == 306]
    x307 = [w for w in ev0.writes if w["addr"] % 1000 == 307]
    A.check(f"{tag}: X306 fallback current = 6 A on every CP",
            bool(x306) and all(w["written"] == 6 for w in x306),
            str([w["written"] for w in x306]))
    A.check(f"{tag}: X307 watchdog timer = 300 s on every CP",
            bool(x307) and all(w["written"] == 300 for w in x307),
            str([w["written"] for w in x307]))
    A.check(f"{tag}: X307 RepeatRate leaves >= 2x margin inside the timer",
            bool(x307) and all(w["repeat"] and float(w["repeat"]) * 2 <= (w["written"] or 0)
                               for w in x307),
            "; ".join(f"repeat={w['repeat']}s timer={w['written']}s" for w in x307))
    print()
    print("  NOTE: both registers are fed by a Formula block with NOTHING wired to its")
    print("        inputs.  That a Loxone Formula in that state emits its constant is")
    print("        ASSUMED, not measured -- it is the single biggest untested assumption")
    print("        in this project.  If it does not, the watchdog is never fed, X307")
    print("        expires and the charger drops to the X306 fallback (or, if X306 was")
    print("        never written either, to whatever it already held).")

    # ---- 11g. live scenarios --------------------------------------------
    if args.no_sim:
        print("\n  (--no-sim: skipping live register evaluation)")
        return

    rdr = Reader(args.host, args.port, args.unit, offline=False)
    try:
        scenarios = args.scenarios
        for scen in scenarios:
            if sim.enabled and not args.no_scenarios:
                sim.set(scen)
            elif not args.no_scenarios:
                print(f"\n  !! simulator dir {sim.dir} not usable; reading whatever the "
                      f"device currently holds instead of driving {scen!r}")
            head(f"{tag}: SCENARIO {scen!r}  @ {args.host}:{args.port}")

            reads = read_sensors(objs, rdr)
            rows = []
            for r in sorted(reads.values(), key=lambda r: (r.obj.cp or 0, r.addr)):
                inside_txt = "in" if r.inside else "*** OUT OF RANGE ***"
                clamped = "-"
                if r.value is not None and not r.inside:
                    lo = fnum(r.obj.attrs.get("MinVal"), float("-inf"))
                    hi = fnum(r.obj.attrs.get("MaxVal"), float("inf"))
                    clamped = (f"clamp->{fmt(min(max(r.value, lo), hi))} | "
                               f"substitute->default/last")
                rows.append([
                    f"CP{r.obj.cp}" if r.obj.cp else "-",
                    r.obj.title, r.addr, r.dtype,
                    " ".join(str(w) for w in r.words) if r.words else "-",
                    fmt(r.raw, 12), fmt(r.value, 9), r.rng, inside_txt, clamped,
                ])
            table(["CP", "sensor", "reg", "type", "raw words", "decoded",
                   "corrected", "valid range", "verdict", "if outside"], rows)

            oob = [r for r in reads.values() if r.value is not None and not r.inside]
            if oob:
                print()
                for r in oob:
                    print(f"  !!! OUT OF RANGE: CP{r.obj.cp} {r.obj.title} @ {r.addr} = "
                          f"{fmt(r.value)} not in {r.rng}  --  Loxone's behaviour here "
                          f"(clamp vs. substitute) is UNRESOLVED; either way the value "
                          f"the project shows is not the value the charger reported.")

            ev = evaluate(objs, conn_index, reads, args.outlimit)
            for n in ev.notes:
                print(f"  note: {n}")

            print("\n  Wallbox inputs:")
            wb_rows = []
            for name, d in ev.wallbox_inputs.items():
                wb_rows.append([name, fmt(d["connected"]), fmt(d["power"], 6),
                                fmt(d["energy"], 9), fmt(d["active"])])
            table(["wallbox", "connected (Vc)", "power [kW]", "energy [kWh]", "active (Cac)"],
                  wb_rows)

            print(f"\n  Modbus writes the Miniserver would issue "
                  f"(Wallbox outLimit injected = {args.outlimit} kW):")
            table(["CP", "actuator", "reg", "fc", "value in", "register", "RepeatRate"],
                  [[f"CP{w['cp']}", w["obj"].title, w["addr"], f"FC{w['fc']}",
                    fmt(w["input"]), w["written"], w["repeat"]] for w in ev.writes])

            # Diagnostic probe: registers the charger has but the PROJECT DOES NOT MAP.
            # X275 (RFID tag of the running session) is the interesting one -- a
            # ModbusDataType=101 String input would be needed and neither project has
            # one, so the RFID scenario is invisible to the Loxone side.
            probe_rows = []
            for cp in (cps or [1]):
                mapped = {int(o.attrs["ModbusAddress"]) for o in objs
                          if o.attrs.get("ModbusAddress")}
                for suffix, label, dcode in ((275, "RFID tag (X275)", 101),
                                             (296, "Max current HW limit (X296)", 96)):
                    a = cp * 1000 + suffix
                    if a in mapped:
                        continue
                    _, dname, wn = datatype_of(str(dcode))
                    regs = rdr.read(a, wn)
                    probe_rows.append([f"CP{cp}", label, a, dname,
                                       " ".join(str(x) for x in regs) if regs else "-",
                                       repr(decode(regs, dcode)) if regs else "-",
                                       "NOT MAPPED by this project"])
            if scen.startswith("rfid"):
                want = (scen.split(None, 1)[1].strip().upper() if " " in scen
                        else "04A1B2C3D4E5F6")
                got = next((r[5] for r in probe_rows if "RFID" in r[1]), None)
                A.check(f"{tag}/{scen}: X275 decodes as the ASCII tag {want!r} "
                        f"(OUR decoder -- Loxone's ModbusDataType=101 is UNVERIFIED)",
                        got is not None and want in got, f"got {got}")
            if probe_rows:
                print("\n  Probe of registers the project does NOT map "
                      "(string decode is an UNVERIFIED guess):")
                table(["CP", "register", "reg", "type", "raw words", "decoded", "note"],
                      probe_rows)

            # --- per-scenario assertions ---
            def sensor_by_reg(suffix: int, cp: int) -> Optional[SensorRead]:
                for r in reads.values():
                    if r.addr == cp * 1000 + suffix:
                        return r
                return None

            for cp in (cps or [1]):
                vs = sensor_by_reg(299, cp)
                if vs is None or vs.value is None:
                    continue
                code = int(vs.value)
                st = next((k for k, v in STATE_CODES.items() if v == code), f"0x{code:04X}")
                wbname = next((n for n in ev.wallbox_inputs
                               if (f"CP{cp}" in n) or len(ev.wallbox_inputs) == 1), None)
                if wbname is None:
                    continue
                d = ev.wallbox_inputs[wbname]
                want_vc = 1.0 if st in VC_ON else 0.0
                want_cac = 1.0 if st in CAC_ON else 0.0
                A.check(f"{tag}/{scen}/CP{cp}: X299={st} -> Vc={want_vc:g}",
                        d["connected"] == want_vc, f"got {fmt(d['connected'])}")
                A.check(f"{tag}/{scen}/CP{cp}: X299={st} -> Cac={want_cac:g}",
                        d["active"] == want_cac, f"got {fmt(d['active'])}")

                # energy: lifetime counter must arrive in kWh, not Wh
                en = sensor_by_reg(250, cp)
                if en and en.value is not None and en.value > 0:
                    A.check(f"{tag}/{scen}/CP{cp}: lifetime energy arrives in kWh "
                            f"(~12182, not 12182070)",
                            12000 <= (d["energy"] or -1) <= 12400,
                            f"Wallbox energy input = {fmt(d['energy'], 9)} kWh "
                            f"(raw {en.raw} Wh)")
                    A.check(f"{tag}/{scen}/CP{cp}: lifetime energy inside its validity range",
                            en.inside, f"{fmt(en.value)} vs {en.rng}")

                # power at 7.4 kW must be kW, not W.  Only assert on a CP that
                # the device actually reports as charging -- in the 2-CP project
                # the simulator only drives CP1 (2xxx reads back all zero), and a
                # CP that is not charging legitimately reports 0 kW.
                if scen == "c2" and st in CAC_ON:
                    A.check(f"{tag}/{scen}/CP{cp}: Wallbox power input ~7.4 kW (NOT 7400)",
                            d["power"] is not None and 7.0 <= d["power"] <= 7.8,
                            f"got {fmt(d['power'], 6)}")
                elif scen == "c2":
                    print(f"  (CP{cp} reports {st}, not charging -- skipping the "
                          f"7.4 kW power assertion for it)")

                # X301 must stay legal whatever the charger reports: exactly 0
                # when the injected outLimit is <= 0 (pause -- the formula maps
                # every non-positive target to 0), else 6..80 A, never 0
                for w in ev.writes:
                    if w["addr"] == cp * 1000 + 301:
                        if args.outlimit <= 0:
                            A.check(f"{tag}/{scen}/CP{cp}: X301 write == 0 (pause) for "
                                    f"outLimit {args.outlimit} kW",
                                    w["written"] == 0, f"got {w['written']}")
                        else:
                            A.check(f"{tag}/{scen}/CP{cp}: X301 write in 6..80 A, never 0, "
                                    f"for outLimit {args.outlimit} kW",
                                    w["written"] is not None and 6 <= w["written"] <= 80,
                                    f"got {w['written']}")
    finally:
        rdr.close()


# ---------------------------------------------------------------------------
# 12.  main
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Evaluate a Loxone Config project's logic against live Modbus data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("USAGE")[-1],
    )
    p.add_argument("projects", nargs="*", default=["Veton.Loxone"],
                   help="project file(s), default Veton.Loxone")
    p.add_argument("--host", default=DEFAULT_HOST, help="Modbus/TCP host (READ ONLY)")
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--unit", type=int, default=1, help="Modbus device/unit id")
    p.add_argument("--sim-dir", default=str(DEFAULT_SIM_DIR),
                   help="simulator directory holding scenario.txt / writes.log")
    p.add_argument("--no-sim", action="store_true",
                   help="static analysis only, do not touch Modbus at all")
    p.add_argument("--no-scenarios", action="store_true",
                   help="read the device as-is; never drive scenario.txt "
                        "(use this against a real charger)")
    p.add_argument("--scenarios", default="a1,b1,b2,c2,e0,f0,fault,reset,rfid 04A1B2C3D4E5F6,reset",
                   help="comma-separated scenario lines to push at the simulator")
    p.add_argument("--outlimit", type=float, default=7.4,
                   help="kW injected at the Wallbox outLimit output during scenarios")
    p.add_argument("--sweep", default=",".join(str(x) for x in DEFAULT_SWEEP),
                   help="comma-separated outLimit values for the X301 sweep")
    args = p.parse_args(argv)

    args.scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    args.sweep = [float(x) for x in args.sweep.split(",") if x.strip()]
    if args.no_scenarios:
        args.scenarios = ["as-found"]

    sim = Sim(pathlib.Path(args.sim_dir), enabled=not (args.no_sim or args.no_scenarios))

    A = Asserts()
    head("Veton Loxone project harness")
    print(f"  repo          : {REPO}")
    print(f"  modbus target : {args.host}:{args.port} unit {args.unit}  "
          f"{'(OFFLINE)' if args.no_sim else '(FC03 reads only -- this harness never writes)'}")
    print(f"  simulator dir : {args.sim_dir}  "
          f"{'usable' if sim.enabled else 'not driven'}")

    for name in args.projects:
        path = pathlib.Path(name)
        if not path.is_absolute():
            path = REPO / name
        if not path.exists():
            print(f"  !! no such project: {path}")
            A.check(f"{name}: file exists", False, str(path))
            continue
        run_project(path, args, sim, A)

    # restore the simulator
    if sim.enabled:
        sim.set("reset", settle=1.0)
        head("SIMULATOR WRITE LOG (lines added while this harness ran)")
        lines = sim.new_log_lines()
        writes = [l for l in lines if ("WRITE" in l or "REJECT" in l)]
        for l in lines:
            print("  " + l)
        A.check("harness provoked no Modbus writes (FC03 reads only)",
                not writes, f"{len(writes)} write/reject line(s): {writes[:5]}")
    else:
        A.check("harness provoked no Modbus writes (FC03 reads only)", True,
                "not verified against a simulator log (sim not driven)")

    A.report()

    head("WHAT THIS RUN DOES NOT PROVE")
    for line in (
        "Loxone Config accepts / can open the project file (Windows-only; untestable here).",
        "That a Formula block with NO inputs wired emits its constant -- this is how the",
        "  X306/X307 watchdog is fed.  KEY UNTESTED ASSUMPTION.",
        "That RepeatRate really re-sends the value (the whole watchdog keep-alive story).",
        "That the Wallbox block behaves as documented -- it is a stub here; outLimit is",
        "  injected, its mode/session/load-shed logic is not modelled at all.",
        "That ModbusDataType=101 really reads a string, or how many registers it reads.",
        "Byte/word order: assumed big-endian, high word first.  No attribute states it.",
        "What Loxone does with a value outside MinVal/MaxVal (clamp vs substitute vs drop).",
        "Any timing behaviour: polling cycles, MinTime/MaxTime/MinChange, block scheduling.",
        "That the four copies of the register map in this repo agree with each other.",
    ):
        print(("    " if line.startswith(" ") else "  - ") + line.strip())

    return 1 if A.failed else 0


if __name__ == "__main__":
    sys.exit(main())
