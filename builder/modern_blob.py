#!/usr/bin/env python3
"""Link the modern variant's changed code into a loader blob for retail 1.06.

Run from the modernizer repository after scripts/prepare.sh (see README.md).
upstream/ is Open-BFME-2 with patches/modern-variant.patch applied; its tools
compile the reference and modern objects. loader/ (dinput8.dll) maps the
blob into the running, unmodified retail game.dat and jumps each changed
function's retail entry to its modern copy. The blob holds the changed and new
code with what it needs that retail lacks; every other symbol resolves to its
retail address. It is not a verification result and never touches build/match.

Inputs, for every Code source that mentions BFME2_MODERN:
  reference object  build.obj_path(source), current (./build.sh <source>)
  modern object     build/modern/objects, current (build.sh --variant modern --compile)
and the baseline game.dat. A function is changed when its bytes or relocation
targets differ between the two objects; it must be a matched ledger row of at
least 5 bytes at an address the ledger does not mark as an ICF fold
(fold-proof=). A modern change to data is refused:
only code is replaced. Retail callers that inlined a changed function keep the
retail behaviour.

Blob format (version 1, little endian):
  "BFME2MDL", u32 version, sha256 of the target game.dat,
  u32 base, size, runs, tls_index_va, tls_start, tls_len,
      entry_site, entry_orig, stub_va, hooks, abs, rel, secrel,
  hooks  (u32 retail va, u32 target va, 5 retail bytes, 3 pad),
  abs    u32 sites holding an address inside the blob (+delta when moved),
  rel    u32 sites holding a rel32 to outside the blob (-delta when moved),
  secrel u32 sites holding a TLS offset (+ the loader's TLS bias),
  runs   (u32 offset, u32 length, bytes); the rest of the blob is zero.
"""
import csv
import hashlib
import json
import os
import re
import struct
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import sys

HERE = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("BFME2_SOURCE", HERE / "upstream")).resolve()
sys.path.insert(0, str(ROOT / "tools"))
import pe_imports  # noqa: E402  (Open-BFME-2's tools)

BASELINE = ROOT / "baselines/bfme2/workshop-vanilla-1.06/files/game.dat"
LOADER = HERE / "loader"
BASE = 0x400000
BLOB_VERSION = 1
HEADER = struct.Struct("<8sI32s13I")
HOOK = struct.Struct("<II5s3x")
THUNK = 8
THUNK_AREA = 0x100

REL_DIR32, REL_DIR32NB, REL_SECREL, REL_REL32 = 6, 7, 0xB, 0x14
CNT_CODE, CNT_UDATA = 0x20, 0x80
LNK_REMOVE, MEM_WRITE = 0x800, 0x80000000


# ---------------------------------------------------------------- COFF
# link_cycle.parse_coff keeps no section data, COMDAT association, alignment
# or symbol types, which the linker needs.
@dataclass
class Section:
    index: int
    name: str
    flags: int
    size: int
    data: bytes
    relocs: list
    comdat_sel: int = 0
    assoc: int = 0
    align: int = 1

    @property
    def is_code(self):
        return bool(self.flags & CNT_CODE)

    @property
    def writable(self):
        return bool(self.flags & MEM_WRITE)

    @property
    def dropped(self):
        return bool(self.flags & LNK_REMOVE) or self.name.startswith((".debug", ".drectve", ".sxdata"))


@dataclass
class Symbol:
    index: int
    name: str
    value: int
    section: int
    type: int
    sclass: int
    naux: int

    @property
    def is_function(self):
        return (self.type >> 4) == 2

    @property
    def external(self):
        return self.sclass == 2


@dataclass
class Obj:
    path: str
    source: str = ""
    sections: dict = field(default_factory=dict)
    symbols: dict = field(default_factory=dict)
    changed: set = field(default_factory=set)
    new: set = field(default_factory=set)
    run_inits: list = field(default_factory=list)
    whole: bool = False  # never split into chunks (CRT helper objects)
    function_list: list = None


def read_coff(raw, path):
    machine, nsec, _, symptr, nsym, opt, _ = struct.unpack_from("<HHIIIHH", raw, 0)
    if machine != 0x14C:
        raise ValueError(f"not an i386 COFF object: {path}")
    strtab = symptr + nsym * 18

    def string(offset):
        return raw[strtab + offset:raw.index(b"\0", strtab + offset)].decode("latin1")

    o = Obj(str(path))
    for i in range(nsec):
        h = 20 + opt + i * 40
        name = raw[h:h + 8].rstrip(b"\0").decode("latin1")
        _, _, size, ptr, rptr, _, nrel, _, flags = struct.unpack_from("<IIIIIIHHI", raw, h + 8)
        if name.startswith("/"):
            name = string(int(name[1:]))
        relocs = [struct.unpack_from("<IIH", raw, rptr + k * 10) for k in range(nrel)]
        al = (flags >> 20) & 0xF
        o.sections[i + 1] = Section(i + 1, name, flags, size, raw[ptr:ptr + size] if ptr else b"",
                                    relocs, align=(1 << (al - 1)) if al else 16)
    i = 0
    while i < nsym:
        b = raw[symptr + i * 18:symptr + i * 18 + 18]
        value, sec, typ, sclass, naux = struct.unpack_from("<IhHBB", b, 8)
        name = string(struct.unpack_from("<I", b, 4)[0]) if b[:4] == b"\0\0\0\0" else b[:8].rstrip(b"\0").decode("latin1")
        o.symbols[i] = Symbol(i, name, value, sec, typ, sclass, naux)
        if sclass == 3 and value == 0 and naux and sec > 0 and name == o.sections[sec].name:
            aux = raw[symptr + (i + 1) * 18:symptr + (i + 2) * 18]
            _, _, _, _, num, sel = struct.unpack_from("<IHHIHB", aux, 0)
            o.sections[sec].comdat_sel, o.sections[sec].assoc = sel, num
        i += 1 + naux
    return o


def section_symbol(o, s):
    return s.section > 0 and s.sclass == 3 and s.value == 0 and s.naux and s.name == o.sections[s.section].name


def functions(o):
    """(section, start, end, symbol) for each function symbol in a code section."""
    if o.function_list is None:
        by_sec = defaultdict(list)
        for s in o.symbols.values():
            if s.section > 0 and o.sections[s.section].is_code and s.sclass in (2, 3) and s.is_function:
                by_sec[s.section].append(s)
        o.function_list = []
        for sec, syms in by_sec.items():
            syms.sort(key=lambda s: s.value)
            for k, s in enumerate(syms):
                end = syms[k + 1].value if k + 1 < len(syms) else o.sections[sec].size
                o.function_list.append((sec, s.value, end, s))
    return o.function_list


def chunk_bounds(o):
    """section index -> [(start, end)]: whole for COMDAT/.tls/.CRT/.xdata and
    whole objects, else split at defined symbols (code only at functions)."""
    starts = defaultdict(set)
    for s in o.symbols.values():
        if (s.section > 0 and s.sclass in (2, 3, 6, 101) and not section_symbol(o, s)
                and (s.is_function or not o.sections[s.section].is_code)):
            starts[s.section].add(s.value)
    out = {}
    for idx, sec in o.sections.items():
        if sec.dropped:
            continue
        whole = o.whole or sec.comdat_sel or sec.name.startswith((".tls", ".CRT", ".xdata")) or sec.size == 0
        st = [0] if whole else sorted({0} | {v for v in starts[idx] if v < sec.size})
        out[idx] = [(a, st[k + 1] if k + 1 < len(st) else sec.size) for k, a in enumerate(st)]
    return out


# ---------------------------------------------------------------- what changed
SG = re.compile(r"^\$SG\d+$")
LOCAL = re.compile(r"^\$[A-Z]+\d+$")
EH = ("__ehhandler$", "__ehfuncinfo$", "__unwindtable$", "__tryblocktable$", "__catchsym$", "__unwindfunclet$")


def helper(name):
    return name.startswith(("$", "__ehhandler$", "__unwindfunclet$"))


def target_key(o, sym, sec, start, end, addend):
    """A build-independent description of a relocation target."""
    if section_symbol(o, sym):
        tgt = o.sections[sym.section]
        if sym.section == sec and start <= addend < end:
            return ("self", addend - start)
        if tgt.is_code:
            for s2, a, b, f in functions(o):
                if s2 == sym.section and a <= addend < b:
                    return ("fn", f.name, addend - a)
        return ("sec", tgt.name, bytes(tgt.data[addend:addend + 32]) if tgt.data else addend)
    if SG.match(sym.name) and sym.section > 0:
        d = o.sections[sym.section].data
        return ("str", bytes(d[sym.value:d.index(b"\0", sym.value) + 1]) if d else sym.value)
    if LOCAL.match(sym.name):
        return ("local", sym.name.rstrip("0123456789"))
    return ("sym", sym.name)


def signature(o, sec, start, end):
    s = o.sections[sec]
    body = bytearray(s.data[start:end])
    rels = []
    for off, si, typ in s.relocs:
        if start <= off < end:
            addend = int.from_bytes(s.data[off:off + 4], "little")
            rels.append((off - start, typ, target_key(o, o.symbols[si], sec, start, end, addend)))
            if typ in (REL_DIR32, REL_DIR32NB):
                body[off - start:off - start + 4] = b"\0\0\0\0"
    return bytes(body), tuple(rels)


def changed_functions(modern, reference):
    rf = {f.name: (sec, a, b) for sec, a, b, f in functions(reference)}
    return [f.name for sec, a, b, f in functions(modern)
            if not helper(f.name) and f.name in rf and signature(modern, sec, a, b) != signature(reference, *rf[f.name])]


def changed_data(modern, reference):
    """Named data both objects define whose bytes or targets differ."""
    def named(o):
        bounds = chunk_bounds(o)
        out = {}
        for s in o.symbols.values():
            sec = o.sections.get(s.section)
            if (sec is None or sec.is_code or sec.dropped or section_symbol(o, s) or s.sclass not in (2, 3)
                    or sec.name.startswith((".CRT", ".tls", ".xdata")) or helper(s.name)
                    or s.name.startswith(EH) or SG.match(s.name) or LOCAL.match(s.name)):
                continue
            for a, b in bounds[s.section]:
                if a <= s.value < b:
                    out[s.name] = (s.section, a, b)
        return out
    m, r = named(modern), named(reference)
    return sorted(n for n in m.keys() & r.keys() if signature(modern, *m[n]) != signature(reference, *r[n]))


# ---------------------------------------------------------------- ledger
def _rva(text):
    text = (text or "").strip()
    return int(text, 16) if text.startswith("0x") else None


def norm_src(source):
    return (source or "").replace("\\", "/").lower()


def load_ledger(size_of_image, root=ROOT):
    """name -> (rva, kind, size, status, source); (name, source) -> code row;
    RVAs of code rows that are ICF folds."""
    csv.field_size_limit(1 << 30)
    names, by_src, folds = {}, {}, set()

    def put(name, rva, kind, size=0, status="", source=""):
        if name and rva is not None:
            names.setdefault(name, (rva, kind, size, status, source))
            if kind == "code":
                by_src.setdefault((name, norm_src(source)), (rva, kind, size, status, source))

    def rows(name):
        with open(root / "reverse" / name, encoding="utf-8", newline="") as f:
            yield from csv.DictReader(f)

    for r in rows("functions.csv"):
        put(r["name"], _rva(r["target_rva"]), "code", int(r["target_size"] or 0), r["status"], r["source"])
        if "fold-proof=" in r["notes"] and _rva(r["target_rva"]) is not None:
            folds.add(_rva(r["target_rva"]))
    for r in rows("symbols.csv"):
        put(r["name"], _rva(r["address"]), "symbol")
    for r in rows("data_converged.csv"):
        a = _rva(r["address"])  # RVAs inside the image, VAs above it
        put(r["name"], a - BASE if a is not None and a >= size_of_image else a, "data")
    for r in rows("data_ledger.csv"):
        if r["kind"] and r["name"] and r["address"] != "0x00000000":
            for alias in [r["name"]] + (r.get("names") or "").split(";"):
                put(alias.strip(), _rva(r["address"]), "data:" + r["section"], int(r["size"] or 0),
                    r["status"], r["source"])
    return names, by_src, folds


# ---------------------------------------------------------------- PE
class Image:
    """The baseline game.dat: file bytes by RVA, entry, imports."""

    def __init__(self, raw):
        self.raw = raw
        pe = struct.unpack_from("<I", raw, 0x3C)[0]
        opt = pe + 24
        self.entry = struct.unpack_from("<I", raw, opt + 16)[0]
        self.size_of_image = struct.unpack_from("<I", raw, opt + 56)[0]
        first = opt + struct.unpack_from("<H", raw, pe + 20)[0]
        self.sections = [struct.unpack_from("<IIII", raw, first + 40 * i + 8)
                         for i in range(struct.unpack_from("<H", raw, pe + 6)[0])]
        self.slots = defaultdict(set)
        for slot, entry in pe_imports.read_imports(raw).items():
            if entry.name:
                self.slots[entry.name].add(slot)

    def read(self, rva, n):
        for _, va, rsize, ptr in self.sections:
            if va <= rva and rva + n <= va + rsize:
                return self.raw[ptr + rva - va:ptr + rva - va + n]
        raise ValueError(f"rva {rva:#x} has no file bytes")

    def import_slot(self, symbol):
        """IAT slot of an __imp_ symbol, or None. The undecorated C name wins over
        the literal spelling: __imp__exit is exit, not the CRT's _exit."""
        spellings = pe_imports.coff_import_names(symbol)
        literal = symbol[len("__imp_"):]
        for names in (spellings - {literal}, {literal} & spellings):
            slots = set().union(*(self.slots.get(n, set()) for n in names))
            if len(slots) > 1:
                raise ValueError(f"{symbol} names {len(slots)} imports")
            if slots:
                return slots.pop()
        return None

    def entry_calls(self):
        """(offset from the entry, target) of each call in the CRT entry's first 0x400 bytes."""
        e = BASE + self.entry
        b = self.read(self.entry, 0x400)
        return [(k, (e + k + 5 + struct.unpack_from("<i", b, k + 1)[0]) & 0xFFFFFFFF)
                for k in range(0x3FB) if b[k] == 0xE8]

    def winmain_call(self, winmain):
        """Offset from the entry of the CRT entry's single call to WinMain."""
        calls = [k for k, t in self.entry_calls() if t == winmain]
        if len(calls) != 1:
            raise ValueError(f"the CRT entry has {len(calls)} calls to WinMain")
        return calls[0]


# ---------------------------------------------------------------- retail correspondence
def correspondence(pairs, image, code_va):
    """Retail addresses of reference-object symbols, walked from matched functions.

    A chunk whose bytes differ from retail outside its relocation sites is not
    trusted; only code and compiler EH tables are walked further (vtables in
    reconstructed data may hold placeholders)."""
    by_name, by_loc = {}, {}
    bounds = {id(r): chunk_bounds(r) for _, r in pairs}
    queue = []

    def chunk_of(r, sec, off):
        for a, b in bounds[id(r)].get(sec, []):
            if a <= off < b or (off == b and a == b):
                return a, b
        return None

    def learn(r, sec, off, va):
        if (id(r), sec, off) not in by_loc:
            by_loc[(id(r), sec, off)] = va
            queue.append((r, sec, off, va))

    for src, r in pairs:
        for sec, a, b, f in functions(r):
            va = code_va(f.name, src, b - a)
            if va:
                learn(r, sec, a, va)
    while queue:
        r, sec, off, va = queue.pop()
        c = chunk_of(r, sec, off)
        s = r.sections[sec]
        if not c or (s.flags & CNT_UDATA and not s.data):
            continue
        a, b = c
        try:
            theirs = image.read(va - (off - a) - BASE, b - a)
        except ValueError:
            continue
        rel = [(o2, si, t) for o2, si, t in s.relocs if a <= o2 < b]
        mine = bytearray(s.data[a:b])
        for o2, _, _ in rel:
            mine[o2 - a:o2 - a + 4] = theirs[o2 - a:o2 - a + 4]
        if mine != theirs or not (s.is_code or s.name.startswith(".xdata")):
            continue
        for o2, si, t in rel:
            if t not in (REL_REL32, REL_DIR32):
                continue
            site = va - (off - a) + (o2 - a)
            v = struct.unpack_from("<I", theirs, o2 - a)[0]
            target = (site + 4 + v) & 0xFFFFFFFF if t == REL_REL32 else v
            addend = struct.unpack_from("<I", s.data, o2)[0]
            sym = r.symbols[si]
            if section_symbol(r, sym):
                tc = chunk_of(r, sym.section, addend)
                if tc:
                    learn(r, sym.section, tc[0], (target - (addend - tc[0])) & 0xFFFFFFFF)
            elif sym.section > 0:
                learn(r, sym.section, sym.value, (target - addend) & 0xFFFFFFFF)
                if sym.external:
                    by_name.setdefault(sym.name, (target - addend) & 0xFFFFFFFF)
            elif sym.section == 0:
                by_name.setdefault(sym.name, (target - addend) & 0xFFFFFFFF)
    return by_name, by_loc


# ---------------------------------------------------------------- link
class Chunk:
    def __init__(self, o, sec, start, end):
        self.o, self.sec, self.start, self.end, self.va = o, sec, start, end, None

    @property
    def kind(self):
        s = self.sec
        if s.name.startswith(".tls"):
            return "tls"
        if s.is_code:
            return "code"
        if s.flags & CNT_UDATA:
            return "bss"
        return "data" if s.writable else "rdata"


ABS = {"__tls_array": 0x2C, "__fltused": 0, "__except_list": 0}
GNAME = re.compile(r"^\??_?g_([0-9A-F]{8})(@@|$)")
ORDER = {"code": 0, "rdata": 1, "data": 2, "tls": 3, "bss": 4}


def modern_initialisers(m, r):
    """Dynamic initialisers of m with no twin among r's."""
    def inits(o):
        return [o.symbols[si] for sec in o.sections.values() if sec.name.startswith(".CRT$XC")
                for _, si, _ in sec.relocs]
    rfun = {f.name: (s, a, b) for s, a, b, f in functions(r)}
    mfun = {f.name: (s, a, b) for s, a, b, f in functions(m)}
    twins = {signature(r, *rfun[s.name]) for s in inits(r) if s.name in rfun}
    return [s for s in inits(m) if s.name in mfun and signature(m, *mfun[s.name]) not in twins]


def eh_of_changed(o, name):
    """EH helpers and tables of a changed function belong to the modern copy."""
    for p in EH:
        if name.startswith(p):
            rest = name[len(p):]
            return any(rest == f or rest.startswith(f + "$") for f in o.changed)
    return False


class Linker:
    def __init__(self, pairs, crt_objects, target, ledger):
        """pairs: [(source, modern Obj with .changed, reference Obj)]; target: Retail106, Engine or Replay."""
        self.target = target
        self.names, self.by_src, self.folds = ledger
        target.attach(self)
        self.base = (BASE + target.size_of_image + 0xFFFF) & ~0xFFFF  # VirtualAlloc granularity
        self.objs = []
        for src, m, r in pairs:
            defined = {s.name for s in r.symbols.values() if s.section > 0}
            m.source = src
            m.new = {s.name for s in m.symbols.values() if s.section > 0 and s.name not in defined
                     and not section_symbol(m, s)}
            m.run_inits = modern_initialisers(m, r)
            self.objs.append(m)
        for x in crt_objects:
            x.new = {s.name for s in x.symbols.values() if s.section > 0 and s.external}
            self.objs.append(x)
        self.ref_of = {id(m): r for _, m, r in pairs}
        self.chunks = {}
        for o in self.objs:
            for idx, spans in chunk_bounds(o).items():
                self.chunks[(id(o), idx)] = [Chunk(o, o.sections[idx], a, b) for a, b in spans]
        self.owned, self.defs = {}, {}
        for o in self.objs:
            o.new |= {s.name for s in o.symbols.values() if s.section > 0 and eh_of_changed(o, s.name)}
            for s in o.symbols.values():
                if s.section <= 0:
                    continue
                if s.name in o.changed or s.name in o.new:
                    if s.external:
                        self.owned.setdefault(s.name, (o, s))
                    if s.sclass == 3:
                        self.owned.setdefault((id(o), s.name), (o, s))
                if s.external and not o.sections[s.section].dropped:
                    self.defs.setdefault(s.name, (o, s))
        self.rows_at = defaultdict(set)
        for (name, _), row in self.by_src.items():
            self.rows_at[row[0]].add(name)
        self.corr_name, self.corr_loc = target.correspondence([(src, r) for src, _, r in pairs])
        self.thunks, self.warnings, self.unresolved = {}, [], defaultdict(list)
        self.included, self.seen, self.queue, self.edges = [], set(), [], {}

    def code_va(self, name, src, size=None):
        return self.target.code_va(name, src, size)

    def chunk_at(self, o, sec, off):
        lst = self.chunks[(id(o), sec)]
        for c in lst:
            if c.start <= off < c.end or (off == c.end == c.sec.size and c is lst[-1]):
                return c
        raise KeyError((o.path, sec, off))

    def at(self, o, s):
        c = self.chunk_at(o, s.section, s.value)
        return ("chunk", c, s.value - c.start)

    def retail_of(self, o, s):
        """Ledger (1.06) address of a symbol in the target; a static only binds to a row of its own file."""
        m = GNAME.match(s.name)
        if m and s.external:
            return self.target.translate(int(m.group(1), 16))
        e = self.names.get(s.name)
        if not e or (s.sclass == 3 and (not e[4] or norm_src(e[4]) != norm_src(o.source))):
            return None
        return self.target.retail_address(s.name, e)

    def retail_static(self, o, s):
        r = self.ref_of.get(id(o))
        if r is None or LOCAL.match(s.name):
            return None
        for rs in r.symbols.values():
            if rs.name == s.name and rs.section > 0:
                return self.corr_loc.get((id(r), rs.section, rs.value))
        return None

    def resolve(self, o, s, user):
        """('va', address) | ('chunk', chunk, offset) | ('section', index) | ('tls_index',)"""
        if section_symbol(o, s):
            return ("section", s.section)
        if s.section > 0 and not s.external:
            if (id(o), s.name) in self.owned:
                return self.at(o, s)
            r = self.retail_static(o, s)
            if r is None:
                r = self.retail_of(o, s)
            if r is not None:
                return ("va", r)
            if o.sections[s.section].writable and not o.sections[s.section].name.startswith(".tls"):
                self.warnings.append(f"copy of writable static {s.name} in {o.source} (from {user})")
            return self.at(o, s)
        if s.name in self.owned:
            return self.at(*self.owned[s.name])
        if s.name in self.corr_name:  # what retail's own matched code references
            return ("va", self.corr_name[s.name])
        if s.name.startswith("__imp_"):
            slot = self.target.import_slot(s.name)
            if slot is not None:
                return ("va", slot)
        elif s.name in ABS:
            return ("va", ABS[s.name])
        elif s.name == "__tls_index":
            return ("tls_index",)
        else:
            r = self.retail_of(o, s)
            if r is not None:
                return ("va", r)
            slot = self.target.import_slot("__imp_" + s.name) if s.section == 0 and s.name.startswith("_") else None
            if slot is not None:
                # a C runtime function retail imports: a jmp [IAT] thunk in the blob
                if s.name not in self.thunks:
                    if (len(self.thunks) + 1) * THUNK > THUNK_AREA:
                        raise ValueError("too many import thunks")
                    self.thunks[s.name] = (self.base + THUNK * len(self.thunks), slot)
                return ("va", self.thunks[s.name][0])
            if s.name in self.defs:
                oo, ss = self.defs[s.name]
                if oo.sections[ss.section].writable:
                    self.warnings.append(f"copy of writable {s.name} (from {user})")
                return self.at(oo, ss)
        self.unresolved[s.name].append(user)
        return ("va", 0)

    def need(self, c):
        if id(c) in self.seen:
            return
        self.seen.add(id(c))
        self.included.append(c)
        self.queue.append(c)
        for idx, sec in c.o.sections.items():  # associative COMDATs follow their parent
            if sec.comdat_sel == 5 and sec.assoc == c.sec.index and (id(c.o), idx) in self.chunks:
                for cc in self.chunks[(id(c.o), idx)]:
                    self.need(cc)

    def collect(self):
        """Hooks; everything reachable from the changed code, new initialisers and TLS."""
        hooks = []
        for o in self.objs:
            for name in sorted(o.changed):
                s = next(s for s in o.symbols.values() if s.name == name and s.section > 0)
                _, c, off = self.at(o, s)
                self.need(c)
                row = self.by_src.get((name, norm_src(o.source)))
                if not row or row[3] != "matched":
                    raise ValueError(f"changed function {name} has no matched 1.06 ledger row")
                if row[2] < 5:
                    raise ValueError(f"changed function {name} is {row[2]} bytes, too short for a jump")
                if row[0] in self.folds:
                    raise ValueError(f"changed function {name} is an ICF fold: the jump would reach its twins")
                for other in sorted(self.rows_at[row[0]] - {name}):
                    self.warnings.append(f"{name} is also reconstructed as {other}; the jump serves both")
                hva = self.target.code_va(name, o.source)
                if hva is None:
                    raise ValueError(f"changed function {name} is not found in the target")
                hooks.append((hva, c, off, name))
            for s in o.run_inits:
                self.need(self.at(o, s)[1])
            for idx, sec in o.sections.items():
                if sec.name.startswith(".tls") and (id(o), idx) in self.chunks:
                    for c in self.chunks[(id(o), idx)]:
                        self.need(c)
        while self.queue:
            c = self.queue.pop()
            out = []
            for off, si, typ in c.sec.relocs:
                if not c.start <= off < c.end:
                    continue
                res = self.resolve(c.o, c.o.symbols[si], f"{Path(c.o.source).name}:{c.sec.name}+{off:x}")
                addend = int.from_bytes(c.sec.data[off:off + 4], "little") if c.sec.data else 0
                if res[0] == "section":
                    tc = self.chunk_at(c.o, res[1], addend)
                    res, addend = ("chunk", tc, addend - tc.start), 0
                if res[0] == "chunk":
                    self.need(res[1])
                out.append((off - c.start, typ, res, addend))
            self.edges[id(c)] = out
        if self.unresolved:
            raise ValueError("unresolved: " + ", ".join(sorted(self.unresolved)[:20]))
        return hooks

    def layout(self):
        """Import thunks, code, rdata, data, tls, bss, the TLS index, the init stub."""
        self.included.sort(key=lambda c: (ORDER[c.kind], c.sec.name, c.o.path, c.start))
        va = self.base + (THUNK_AREA if self.thunks else 0)
        self.tls = [None, None]
        for c in self.included:
            if c.kind == "tls" and self.tls[0] is None:
                va = self.tls[0] = (va + 15) & ~15
            if c.kind == "bss" and self.tls[0] is not None and self.tls[1] is None:
                self.tls[1] = va
            al = max(c.sec.align if c.start == 0 else 1, 1)
            c.va = va = (va + al - 1) & ~(al - 1)
            va += c.end - c.start
        if self.tls[0] is not None and self.tls[1] is None:
            self.tls[1] = va
        self.tls_index_va = (va + 15) & ~15
        self.stub_va = self.tls_index_va + 4
        self.inits = [self.at(o, s) for o in self.objs for s in o.run_inits]
        self.size = self.stub_va + 5 * (len(self.inits) + 1) - self.base

    def relocate(self, memory):
        """Write every relocation; return the loader's site lists and every site."""
        inside = lambda v: self.base <= v < self.base + self.size  # noqa: E731
        lists, sites = {"abs": [], "rel": [], "secrel": []}, []
        for c in self.included:
            for off, typ, res, addend in self.edges[id(c)]:
                site = c.va + off
                target = (res[1].va + res[2] if res[0] == "chunk" else
                          self.tls_index_va if res[0] == "tls_index" else res[1]) + addend
                if typ == REL_DIR32:
                    val = target
                elif typ == REL_DIR32NB:
                    val = target - BASE
                elif typ == REL_REL32:
                    val = target - (site + 4)
                elif typ == REL_SECREL and res[0] == "chunk" and res[1].kind == "tls":
                    val = target - self.tls[0]
                    lists["secrel"].append(site)
                else:
                    raise ValueError(f"relocation type {typ:#x} at {site:#x}")
                if typ in (REL_DIR32, REL_DIR32NB) and inside(target):
                    lists["abs"].append(site)
                elif typ == REL_REL32 and not inside(target):
                    lists["rel"].append(site)
                sites.append((site, typ, target))
                struct.pack_into("<I", memory, site - self.base, val & 0xFFFFFFFF)
        return lists, sites

    def link(self):
        hooks = self.collect()
        self.layout()
        memory = bytearray(self.size)
        for c in self.included:
            if c.sec.data and c.kind != "bss":
                memory[c.va - self.base:c.va - self.base + c.end - c.start] = c.sec.data[c.start:c.end]
        for tva, slot in self.thunks.values():
            memory[tva - self.base:tva - self.base + THUNK] = b"\xFF\x25" + struct.pack("<I", slot) + b"\xCC\xCC"
        lists, sites = self.relocate(memory)

        # init stub: call each modern-only initialiser, then continue to WinMain
        entry_site, entry_orig = self.target.winmain_call(BASE + self.names["_WinMain@16"][0])
        winmain = (entry_site + 4 + entry_orig) & 0xFFFFFFFF
        stub = bytearray()
        for _, c, off in self.inits:
            stub += b"\xE8" + struct.pack("<i", c.va + off - (self.stub_va + len(stub) + 5))
        stub += b"\xE9" + struct.pack("<i", winmain - (self.stub_va + len(stub) + 5))
        memory[self.stub_va - self.base:self.stub_va - self.base + len(stub)] = stub
        lists["rel"].append(self.stub_va + len(stub) - 4)
        sites.append((self.stub_va + len(stub) - 4, REL_REL32, winmain))

        runs = data_runs(bytes(memory))
        tls_start, tls_end = self.tls
        blob = (HEADER.pack(b"BFME2MDL", BLOB_VERSION, self.target.sha256,
                            self.base, self.size, len(runs), self.tls_index_va, tls_start or 0,
                            (tls_end - tls_start) if tls_start else 0, entry_site, entry_orig, self.stub_va,
                            len(hooks), len(lists["abs"]), len(lists["rel"]), len(lists["secrel"]))
                + b"".join(HOOK.pack(hva, c.va + off, self.target.hook_bytes(hva)) for hva, c, off, _ in hooks)
                + b"".join(struct.pack(f"<{len(v)}I", *v) for v in lists.values())
                + b"".join(struct.pack("<II", o, len(b)) + b for o, b in runs))
        report = {"base": self.base, "size": self.size, "hooks": [[hva, name] for hva, _, _, name in hooks],
                  "initialisers": len(self.inits), "thunks": sorted(self.thunks), "tls": self.tls,
                  "warnings": sorted(set(self.warnings))}
        return blob, report, sites


def data_runs(data, gap=64):
    """(offset, bytes) runs of data; zero gaps of at least `gap` bytes are left out."""
    runs, i = [], 0
    while i < len(data):
        while i < len(data) and data[i] == 0:
            i += 1
        if i >= len(data):
            break
        j, zeros = i, 0
        while j < len(data) and zeros < gap:
            zeros = zeros + 1 if data[j] == 0 else 0
            j += 1
        runs.append((i, data[i:j - zeros]))
        i = j - zeros
    return runs


# ---------------------------------------------------------------- blob reader and self-check
FIELDS = ("base", "size", "runs", "tls_index_va", "tls_start", "tls_len", "entry_site", "entry_orig",
          "stub_va", "hooks", "abs", "rel", "secrel")


def parse_blob(blob):
    magic, version, sha, *values = HEADER.unpack_from(blob, 0)
    if magic != b"BFME2MDL" or version != BLOB_VERSION:
        raise ValueError("not a version 1 modern blob")
    head = dict(zip(FIELDS, values), sha256=sha)
    pos = HEADER.size
    head["hook_list"] = [HOOK.unpack_from(blob, pos + HOOK.size * i) for i in range(head["hooks"])]
    pos += HOOK.size * head["hooks"]
    for key in ("abs", "rel", "secrel"):
        head[key + "_sites"] = list(struct.unpack_from(f"<{head[key]}I", blob, pos))
        pos += 4 * head[key]
    memory = bytearray(head["size"])
    for _ in range(head["runs"]):
        off, n = struct.unpack_from("<II", blob, pos)
        pos += 8
        if off + n > head["size"] or pos + n > len(blob):
            raise ValueError("blob data run out of range")
        memory[off:off + n] = blob[pos:pos + n]
        pos += n
    if pos != len(blob):
        raise ValueError("trailing bytes in blob")
    return head, memory


def load_at(blob, address, tls_bias=0):
    """The blob's memory as the loader leaves it when it maps the blob at address."""
    head, memory = parse_blob(blob)
    delta = address - head["base"]
    for key, change in (("abs_sites", delta), ("rel_sites", -delta), ("secrel_sites", tls_bias)):
        for site in head[key]:
            o = site - head["base"]
            struct.pack_into("<I", memory, o, (struct.unpack_from("<I", memory, o)[0] + change) & 0xFFFFFFFF)
    return head, memory


def check_relocation(blob, sites, moved=0x10000000):
    """After the loader maps the blob `moved` bytes away, every site must reach its
    target, moved with the blob when the target is inside it."""
    base = HEADER.unpack_from(blob, 0)[3]
    head, memory = load_at(blob, base + moved)
    for site, typ, target in sites:
        if typ == REL_SECREL:
            continue
        value = struct.unpack_from("<I", memory, site - base)[0]
        actual = {REL_REL32: value + site + moved + 4, REL_DIR32NB: value + BASE}.get(typ, value) & 0xFFFFFFFF
        expected = (target + moved if base <= target < base + head["size"] else target) & 0xFFFFFFFF
        if actual != expected:
            raise ValueError(f"site {site:#x} reaches {actual:#x} after a move, expected {expected:#x}")
    return len(sites)


# ---------------------------------------------------------------- inputs and outputs
def modern_sources(root=ROOT):
    out = subprocess.run(["git", "grep", "-l", "BFME2_MODERN", "--", "Code/*.cpp", "Code/*.c"],
                         cwd=root, capture_output=True, text=True, check=True).stdout
    return sorted(line.strip() for line in out.splitlines() if line.strip())


def crt_helpers():
    """The 64-bit remainder helper retail has no copy of, from VC7.1's msvcrt.lib."""
    import build
    import coffar
    lib = build.vc71_root() / "Vc7/lib/msvcrt.lib"
    for member, raw in coffar.read_archive(lib):
        if member.replace("\\", "/").endswith("dll_obj/llrem.obj"):
            o = read_coff(raw, f"{lib.name}:{member}")
            o.source, o.whole = "crt/llrem.obj", True
            return [o]
    raise ValueError("msvcrt.lib has no dll_obj/llrem.obj")


def object_pairs(sources, root=ROOT):
    """(source, modern path, reference path) of the 1.06 build: verified reference objects."""
    import build
    context = build.CompilationContext()
    out = []
    for name in sources:
        source = (root / name).resolve(strict=True)
        reference = build.obj_path(source)
        modern = root / "build/modern/objects" / reference.name
        if not build.compile_is_current(source, reference):
            raise ValueError(f"reference object is absent or stale (./build.sh {name}): {reference}")
        if not build.compile_is_current(source, modern, strict=True, context=context):
            raise ValueError(f"modern object is absent or stale (build.sh --variant modern --compile): {name}")
        out.append((name, modern, reference))
    return out


def build_blob(out_dir, target=None, *, root=ROOT):
    """Link the blob for a target (default 1.06); returns its path and report."""
    target = target or TARGETS["bfme2-106"]
    sources = modern_sources(root)
    pairs = []
    for name, modern, reference in (object_pairs(sources, root) if target.name == "bfme2-106"
                                    else target.compile(sources, root)):
        m = read_coff(modern.read_bytes(), modern)
        r = read_coff(reference.read_bytes(), reference)
        data = changed_data(m, r)
        if data:
            raise ValueError(f"{name}: the modern variant changes data, which the blob cannot replace: {data}")
        m.changed = set(changed_functions(m, r))
        pairs.append((name, m, r))
    retail = Image(BASELINE.read_bytes())
    ledger = load_ledger(retail.size_of_image, root)
    blob, report, sites = Linker(pairs, crt_helpers(), target.open(retail, pairs), ledger).link()
    checked = check_relocation(blob, sites)
    target.save()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / target.blob
    path.write_bytes(blob)
    report.update(target=target.name, sources=sources, relocation_sites_checked=checked,
                  game_sha256=target.sha256_hex, blob_sha256=hashlib.sha256(blob).hexdigest(),
                  changed={name: sorted(m.changed) for name, m, _ in pairs if m.changed})
    (out_dir / f"blob_report-{target.name}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path, report


def build_loader(out_dir):
    """dinput8.dll from loader/, built with the installed Visual Studio's x86 compiler."""
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / \
        "Microsoft Visual Studio/Installer/vswhere.exe"
    if not vswhere.exists():
        raise ValueError("vswhere.exe not found: the loader needs Visual Studio with the C++ x86 tools")
    install = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires",
                              "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property",
                              "installationPath"], capture_output=True, text=True, check=True).stdout.strip()
    vcvars = Path(install) / "VC/Auxiliary/Build/vcvars32.bat"
    if not vcvars.exists():
        raise ValueError("vcvars32.bat not found")
    work = out_dir / "loader-obj"
    work.mkdir(parents=True, exist_ok=True)
    dll = out_dir / "dinput8.dll"
    script = work / "build.cmd"
    script.write_text(
        f'@call "{vcvars}" >nul || exit /b 1\r\n'
        f'@cl /nologo /O2 /MT /W3 /LD /Brepro "{LOADER / "modern_loader.c"}" /Fo"{work / "modern_loader.obj"}" '
        f'/Fe"{dll}" /link /DEF:"{LOADER / "dinput8.def"}" /DYNAMICBASE /NXCOMPAT:NO /Brepro '
        f'/IMPLIB:"{work / "dinput8.lib"}"\r\n', encoding="ascii")
    result = subprocess.run(["cmd", "/d", "/c", str(script)], capture_output=True, text=True)
    if result.returncode != 0 or not dll.exists():
        raise ValueError("loader build failed:\n" + result.stdout[-3000:] + result.stderr[-2000:])
    return dll


# ---------------------------------------------------------------- targets
# The ledger and the reference objects describe 1.06. The RotWK 2.01 engine (its
# own game.dat, and the community BFME II 1.09's) holds the same functions at
# other addresses, with a few longer classes (patches/layout-201.patch). Its
# game.dat cannot be shipped, so `--describe GAME_DAT` links once against it and
# records every address and byte the link read in targets/<name>.json; every
# later build replays that record and needs no game.dat.
TARGETS_DIR = HERE / "targets"


class StaleDescription(ValueError):
    pass


class Retail106:
    """The 1.06 baseline game.dat: ledger addresses are its addresses."""

    def __init__(self, image):
        self.image = image
        self.size_of_image = image.size_of_image
        self.sha256 = hashlib.sha256(image.raw).digest()

    def attach(self, linker):
        self.by_src = linker.by_src

    def correspondence(self, pairs):
        return correspondence(pairs, self.image, self.code_va)

    def code_va(self, name, src, size=None):
        row = self.by_src.get((name, norm_src(src)))
        if row and row[3] == "matched" and (size is None or row[2] == size):
            return BASE + row[0]
        return None

    def translate(self, va):
        return va

    def retail_address(self, name, row):
        return BASE + row[0]

    def import_slot(self, symbol):
        return self.image.import_slot(symbol)

    def winmain_call(self, winmain):
        k = self.image.winmain_call(winmain)
        return BASE + self.image.entry + k + 1, struct.unpack_from("<I", self.image.read(self.image.entry + k + 1, 4))[0]

    def hook_bytes(self, hva):
        return self.image.read(hva - BASE, 5)


def masked_pattern(body, masked, lo, hi):
    return re.compile(b"".join(b"." if k in masked else re.escape(body[k:k + 1]) for k in range(lo, hi)),
                      re.DOTALL)


def relocation_mask(section, a, b):
    out = set()
    for off, _, _ in section.relocs:
        if a <= off < b:
            out.update(range(off - a, off - a + 4))
    return out


def layout_map(pairs, image, by_src):
    """{name|source: {"va", "size", "diffs" or "cands"}} for each matched reference function
    of at least 12 bytes, found in the target's .text by its bytes (relocations wildcarded).
    A function found only by 16-byte anchors gets the instruction differences, so a
    function whose code changed is never taken for its 1.06 self."""
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    number = re.compile(r"\b(0x[0-9a-f]+|\d+)\b")
    text = next(s for s in image.sections if s[1] <= image.entry < s[1] + s[0])
    tbase, tdata = BASE + text[1], image.read(text[1], text[2])

    def norm(code, va, relocs=()):
        out = []
        for i in md.disasm(code, va):
            rel = i.address - va
            relocated = any(rel <= r < rel + i.size for r in relocs)
            out.append((number.sub(lambda m: "A" if int(m.group(1), 0) >= 0x400000 or
                                   (relocated and int(m.group(1), 0) == 0) else m.group(1),
                                   f"{i.mnemonic} {i.op_str}"), rel, f"{i.mnemonic} {i.op_str}"))
        return out

    out = {}
    for src, r in pairs:
        for sec, a, b, f in functions(r):
            row = by_src.get((f.name, norm_src(src)))
            if helper(f.name) or b - a < 12 or not row or row[3] != "matched":
                continue
            s, n, key = r.sections[sec], b - a, f.name + "|" + src
            body, masked = s.data[a:b], relocation_mask(s, a, b)
            hits = [m.start() for m in masked_pattern(body, masked, 0, n).finditer(tdata)]
            if len(hits) == 1:
                out[key] = {"va": tbase + hits[0], "size": n, "diffs": []}
                continue
            if hits:
                out[key] = {"va": None, "cands": [tbase + h for h in hits], "size": n, "diffs": []}
                continue
            votes = {}
            for st in range(0, max(1, n - 16), max(4, n // 24)):
                found = [m.start() - st for m in masked_pattern(body, masked, st, st + 16).finditer(tdata)]
                if len(found) == 1:
                    votes[found[0]] = votes.get(found[0], 0) + 1
            if not votes:
                out[key] = {"va": None, "size": n, "diffs": None}
                continue
            start = max(votes, key=votes.get)
            mine = norm(body, 0x10000000, [off - a for off, _, _ in s.relocs if a <= off < b])
            theirs = norm(tdata[start:start + n + 64], tbase + start)
            import difflib
            sm = difflib.SequenceMatcher(None, [x[0] for x in mine], [x[0] for x in theirs], autojunk=False)
            diffs = []
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == "equal":
                    continue
                if tag == "insert" and i1 == len(mine):
                    break
                diffs.append({"tag": tag, "ref": [x[2] for x in mine[i1:i2]], "target": [x[2] for x in theirs[j1:j2]]})
            out[key] = {"va": tbase + start, "size": n, "diffs": diffs}
    return out


class Engine(Retail106):
    """A RotWK 2.01-engine game.dat: 1.06 addresses translated through the layout map,
    and ledger functions outside the map found by their 1.06 bytes."""

    def __init__(self, image, retail, pairs):
        super().__init__(image)
        self.retail, self.pairs = retail, pairs

    def attach(self, linker):
        super().attach(linker)
        self.layout = layout_map(self.pairs, self.image, self.by_src)
        self.v106 = {}
        for key, v in self.layout.items():
            row = self.by_src.get(tuple_key(key))
            if row and v.get("va") and not v.get("diffs"):
                self.v106[BASE + row[0]] = v["va"]
        # an ambiguous twin: the one candidate between the found neighbours (order is kept)
        found = sorted(self.v106.items())
        for key, v in self.layout.items():
            row = self.by_src.get(tuple_key(key))
            if v.get("va") or not v.get("cands") or not row:
                continue
            a = BASE + row[0]
            lo = max((t for s, t in found if s < a), default=0)
            hi = min((t for s, t in found if s > a), default=1 << 32)
            inside = [c for c in v["cands"] if lo < c < hi]
            if len(inside) == 1:
                v["va"] = self.v106[a] = inside[0]
        text = next(s for s in self.image.sections if s[1] <= self.image.entry < s[1] + s[0])
        self.text = (BASE + text[1], self.image.read(text[1], text[2]))

    def code_va(self, name, src, size=None):
        v = self.layout.get(name + "|" + src)
        if v and v.get("va") and not v.get("diffs") and (size is None or v["size"] == size):
            return v["va"]
        return None

    def translate(self, va):
        return self.v106.get(va)

    def retail_address(self, name, row):
        va = self.translate(BASE + row[0])
        return va if va is not None else self.find(name, row)

    def find(self, name, row):
        """A ledger function outside the map, by its 1.06 bytes (relocations wildcarded when
        its verified object is at hand)."""
        if row[1] != "code" or not row[2]:
            return None
        body = self.retail.read(row[0], row[2])
        masked = set()
        obj = self.reference_object(row[4])
        if obj:
            for sec, a, b, f in functions(obj):
                if f.name == name and b - a == row[2]:
                    masked = relocation_mask(obj.sections[sec], a, b)
        tbase, tdata = self.text
        hits = [m.start() for m in masked_pattern(body, masked, 0, len(body)).finditer(tdata)]
        return tbase + hits[0] if len(hits) == 1 else None

    def reference_object(self, source):
        """The 1.06 object of a ledger source (for its relocation sites), compiled on demand."""
        if not source:
            return None
        cache = self.__dict__.setdefault("objects", {})
        if source not in cache:
            import build
            path = (ROOT / source).resolve()
            obj = build.obj_path(path)
            if path.suffix.lower() not in (".c", ".cpp"):
                obj = None
            elif not obj.exists():
                obj = ROOT / "build/targets/find" / obj.name
                obj.parent.mkdir(parents=True, exist_ok=True)
                cmd, env = build.compiler_command(path, obj)
                if subprocess.run(cmd, env=env, cwd=ROOT, capture_output=True).returncode or not obj.exists():
                    obj = None
            cache[source] = read_coff(obj.read_bytes(), obj) if obj else None
        return cache[source]

    def winmain_call(self, winmain):
        """The CRT entry is the same code in every build: the call at 1.06's WinMain offset."""
        k = self.retail.winmain_call(winmain)
        if k not in dict(self.image.entry_calls()):
            raise ValueError("the target's CRT entry has no call at 1.06's WinMain offset")
        return BASE + self.image.entry + k + 1, struct.unpack_from("<I", self.image.read(self.image.entry + k + 1, 4))[0]


def tuple_key(key):
    name, src = key.split("|", 1)
    return name, norm_src(src)


class Recorder:
    """Passes every call through to a target and keeps what it answered."""

    def __init__(self, inner):
        self.inner, self.facts = inner, {}
        self.size_of_image, self.sha256 = inner.size_of_image, inner.sha256

    def attach(self, linker):
        self.inner.attach(linker)

    def _keep(self, key, value):
        self.facts[key] = value
        return value

    def correspondence(self, pairs):
        by_name, by_loc = self.inner.correspondence(pairs)
        src_of = {id(r): src for src, r in pairs}
        self.facts["correspondence"] = {"names": by_name,
                                        "locations": sorted([src_of[o], sec, off, va] for (o, sec, off), va in by_loc.items())}
        return by_name, by_loc

    def code_va(self, name, src, size=None):
        return self._keep(f"code|{name}|{src}|{size}", self.inner.code_va(name, src, size))

    def translate(self, va):
        return self._keep(f"translate|{va}", self.inner.translate(va))

    def retail_address(self, name, row):
        return self._keep(f"address|{name}", self.inner.retail_address(name, row))

    def import_slot(self, symbol):
        return self._keep(f"import|{symbol}", self.inner.import_slot(symbol))

    def winmain_call(self, winmain):
        return tuple(self._keep(f"winmain|{winmain}", list(self.inner.winmain_call(winmain))))

    def hook_bytes(self, hva):
        return bytes.fromhex(self._keep(f"hook|{hva}", self.inner.hook_bytes(hva).hex()))

    def description(self):
        return {"sha256": self.sha256.hex(), "size_of_image": self.size_of_image,
                **{k: self.facts[k] for k in sorted(self.facts)}}


class Replay:
    """Answers from a recorded description; a question it was never asked means the
    modern code changed what it needs from the game.dat: describe the target again."""

    def __init__(self, description, path):
        self.d, self.path = description, path
        self.size_of_image, self.sha256 = description["size_of_image"], bytes.fromhex(description["sha256"])

    def attach(self, linker):
        pass

    def _get(self, key):
        if key not in self.d:
            raise StaleDescription(f"{self.path.name} has no record of {key}: run --describe with that game.dat")
        return self.d[key]

    def correspondence(self, pairs):
        corr = self._get("correspondence")
        ids = {src: id(r) for src, r in pairs}
        return dict(corr["names"]), {(ids[src], sec, off): va for src, sec, off, va in corr["locations"]}

    def code_va(self, name, src, size=None):
        return self._get(f"code|{name}|{src}|{size}")

    def translate(self, va):
        return self._get(f"translate|{va}")

    def retail_address(self, name, row):
        return self._get(f"address|{name}")

    def import_slot(self, symbol):
        return self._get(f"import|{symbol}")

    def winmain_call(self, winmain):
        return tuple(self._get(f"winmain|{winmain}"))

    def hook_bytes(self, hva):
        return bytes.fromhex(self._get(f"hook|{hva}"))


@dataclass
class Target:
    name: str
    blob: str
    defines: tuple = ()
    game: Path = None  # set by describe()
    recorder: Recorder = None

    @property
    def description_path(self):
        return TARGETS_DIR / f"{self.name}.json"

    def compile(self, sources, root=ROOT):
        """Reference and modern objects with this target's defines, under build/targets/<name>/."""
        import build
        import concurrent.futures
        out = root / "build/targets" / self.name

        def one(job):
            name, variant = job
            source = (root / name).resolve(strict=True)
            obj = out / variant / build.obj_path(source).name
            obj.parent.mkdir(parents=True, exist_ok=True)
            obj.unlink(missing_ok=True)
            cmd, env = build.compiler_command(source, obj)
            i = next(k for k, a in enumerate(cmd) if a.startswith("-Fo"))
            cmd[i:i] = ["-D" + d for d in self.defines] + (["-DBFME2_MODERN=1"] if variant == "modern" else [])
            r = subprocess.run(cmd, env=env, cwd=root, capture_output=True, text=True)
            if r.returncode or not obj.exists():
                raise ValueError(f"{variant} compile of {name} failed:\n{r.stdout[-2000:]}{r.stderr[-1000:]}")
            return obj

        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            objs = list(pool.map(one, [(s, v) for s in sources for v in ("modern", "ref")]))
        return [(s, objs[2 * k], objs[2 * k + 1]) for k, s in enumerate(sources)]

    def open(self, retail, pairs):
        if self.name == "bfme2-106":
            target = Retail106(retail)
        elif self.game:
            raw = self.game.read_bytes()
            target = self.recorder = Recorder(Engine(Image(raw), retail, [(src, r) for src, _, r in pairs]))
        else:
            target = Replay(json.loads(self.description_path.read_text(encoding="utf-8")), self.description_path)
        self.sha256_hex = target.sha256.hex()
        return target

    def describe(self, game):
        if self.name == "bfme2-106":
            raise ValueError("bfme2-106 links against upstream's baseline; it needs no description")
        self.game = Path(game)

    def save(self):
        """After a describing link: write the record."""
        if self.recorder:
            TARGETS_DIR.mkdir(exist_ok=True)
            self.description_path.write_text(json.dumps(self.recorder.description(), indent=1, sort_keys=True) + "\n",
                                             encoding="utf-8")
            self.game = self.recorder = None


TARGETS = {t.name: t for t in (
    Target("bfme2-106", "bfme2-modern-106.blob"),
    Target("bfme2-109", "bfme2-modern-109.blob", ("BFME2_LAYOUT_201", "BFME2_HUD_169")),
    Target("rotwk-201", "bfme2-modern-rotwk-201.blob", ("BFME2_LAYOUT_201",)),
)}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Link a target's blob; build dinput8.dll.")
    parser.add_argument("--target", choices=sorted(TARGETS), default="bfme2-106")
    parser.add_argument("--describe", type=Path, metavar="GAME_DAT",
                        help="link against this game.dat and write targets/<target>.json (not for bfme2-106)")
    parser.add_argument("--loader", action="store_true", help="also build dinput8.dll")
    parser.add_argument("--out", type=Path, default=HERE / "build/blob")
    args = parser.parse_args(argv)
    target = TARGETS[args.target]
    if args.describe:
        target.describe(args.describe)
        described = build_blob(args.out, target)[0].read_bytes()
    blob, report = build_blob(args.out, target)
    if args.describe and blob.read_bytes() != described:
        raise SystemExit(f"replaying {target.description_path.name} gives another blob")
    summary = {"target": target.name, "blob": str(blob), "hooks": len(report["hooks"]), "size": report["size"],
               "relocation_sites_checked": report["relocation_sites_checked"], "warnings": report["warnings"]}
    if args.loader:
        summary["loader"] = str(build_loader(args.out))
    print(json.dumps(summary, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
