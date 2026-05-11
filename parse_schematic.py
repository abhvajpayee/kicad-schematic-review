#!/usr/bin/env python3
"""
parse_schematic.py - Parse KiCad .kicad_sch schematics into a JSON netlist.

Traverses the full sheet hierarchy starting from a root .kicad_sch file,
resolves wire connectivity, and emits a flat JSON netlist with rich per-pin
context (pin type, pin name, footprint, etc.).

Usage:
    python parse_schematic.py <root.kicad_sch>
    python parse_schematic.py <root.kicad_sch> --output netlist.json
    python parse_schematic.py <directory>          # auto-detects root sheet

Output schema:
    {
        "nets": {
            "NET_NAME": [
                {"ref": "U1", "pin": "A3", "pin_name": "PA0", "pin_type": "bidirectional"},
                ...
            ],
            "__NC__":  [...],   # floating pins (no wire attached)
            "__FNC__": [...]    # explicitly no-connected by designer
        },
        "components": {
            "U1": {
                "value": "STM32MP135D",
                "footprint": "BGA-448",
                "sheet": "STM32MP1_Processor.kicad_sch",
                "description": "...",
                "datasheet": "...",
                "properties": {"MPN": "...", "Manufacturer": "..."}
            }
        }
    }
"""

import json
import math
import os
import re
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# S-expression tokenizer / parser
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    tokens: List[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in ' \t\n\r':
            i += 1
        elif c == '(':
            tokens.append('(')
            i += 1
        elif c == ')':
            tokens.append(')')
            i += 1
        elif c == '"':
            j = i + 1
            buf: List[str] = []
            while j < n:
                if text[j] == '\\' and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                elif text[j] == '"':
                    j += 1
                    break
                else:
                    buf.append(text[j])
                    j += 1
            tokens.append('\x00' + ''.join(buf))  # \x00 prefix marks quoted strings
            i = j
        else:
            j = i
            while j < n and text[j] not in ' \t\n\r()':
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def _parse(tokens: List[str], pos: int) -> Tuple[list, int]:
    assert tokens[pos] == '('
    pos += 1
    result: list = []
    while pos < len(tokens) and tokens[pos] != ')':
        if tokens[pos] == '(':
            child, pos = _parse(tokens, pos)
            result.append(child)
        else:
            tok = tokens[pos]
            result.append(tok[1:] if tok.startswith('\x00') else tok)
            pos += 1
    return result, pos + 1


def _load(path: str) -> list:
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    tokens = _tokenize(text)
    node, _ = _parse(tokens, 0)
    return node


# ---------------------------------------------------------------------------
# S-expression navigation helpers
# ---------------------------------------------------------------------------

def _children(node: list, tag: str) -> List[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def _child(node: list, tag: str) -> Optional[list]:
    for c in node:
        if isinstance(c, list) and c and c[0] == tag:
            return c
    return None


def _val(node: list, tag: str, default=None):
    c = _child(node, tag)
    return c[1] if c and len(c) > 1 else default


def _flag(node: list, tag: str) -> bool:
    return _child(node, tag) is not None


# ---------------------------------------------------------------------------
# Coordinate math
# ---------------------------------------------------------------------------

_PREC = 4  # decimal places; KiCad grid ≥ 0.0254 mm, so 4dp is safe


def _key(x: float, y: float) -> Tuple[float, float]:
    return (round(x, _PREC), round(y, _PREC))


def _transform(lx: float, ly: float,
               sx: float, sy: float, rot: float,
               mirror_x: bool, mirror_y: bool) -> Tuple[float, float]:
    """Map a lib-symbol-local pin tip to schematic global coordinates.

    KiCad lib_symbols use a Y-up coordinate system (mathematical Y), but
    schematics use Y-down (screen coordinates).  The Y axis must be negated
    before rotation.  Mirror flags are also applied in lib-symbol space (before
    Y-inversion).
    """
    # Mirror in lib-symbol space (before Y-inversion)
    if mirror_x:
        ly = -ly
    if mirror_y:
        lx = -lx
    # Convert lib-symbol Y-up → schematic Y-down
    ly = -ly
    a = math.radians(rot)
    cos_a, sin_a = math.cos(a), math.sin(a)
    gx = sx + lx * cos_a + ly * sin_a
    gy = sy - lx * sin_a + ly * cos_a
    return _key(gx, gy)


# ---------------------------------------------------------------------------
# Union-Find
# ---------------------------------------------------------------------------

class _UF:
    def __init__(self):
        self._p: dict = {}

    def ensure(self, x):
        if x not in self._p:
            self._p[x] = x

    def find(self, x) -> object:
        self.ensure(x)
        while self._p[x] != x:
            self._p[x] = self._p[self._p[x]]
            x = self._p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._p[rb] = ra

    def root(self, x):
        return self.find(x)


# ---------------------------------------------------------------------------
# lib_symbols extraction
# ---------------------------------------------------------------------------

_PWR_PIN_TYPES = {'power_in', 'power_out', 'pwr'}


def _is_power_lib_sym(sym: list) -> bool:
    """True when the lib symbol is a power-only symbol (no BOM entry, all pwr pins)."""
    if _val(sym, 'in_bom', 'yes') == 'yes':
        return False
    has_pin = False
    for part in _children(sym, 'symbol'):
        for pin in _children(part, 'pin'):
            has_pin = True
            pin_type = pin[1] if len(pin) > 1 else ''
            if pin_type not in _PWR_PIN_TYPES:
                return False
    return has_pin


def _extract_lib_symbols(sch: list) -> Dict[str, dict]:
    """
    Parse lib_symbols section.
    Returns {lib_id: {'pins': {pin_num: {name, type, lx, ly}}, 'is_power': bool}}
    Includes ALL pins from ALL sub-parts (unit 0 shared + unit N specific).
    Hidden duplicate pins are included because they represent real PCB pads.
    """
    result: Dict[str, dict] = {}
    lib_syms_node = _child(sch, 'lib_symbols')
    if not lib_syms_node:
        return result

    for sym in _children(lib_syms_node, 'symbol'):
        if len(sym) < 2 or not isinstance(sym[1], str):
            continue
        lib_id = sym[1]
        is_power = _is_power_lib_sym(sym)
        pins: Dict[str, dict] = {}

        for part in _children(sym, 'symbol'):
            for pin in _children(part, 'pin'):
                # lib pins: ['pin', type, shape, ['at', x, y, angle], ['length', n], ...]
                at = _child(pin, 'at')
                if not at or len(at) < 3:
                    continue
                pin_type = pin[1] if len(pin) > 1 else 'unspecified'
                lx, ly = float(at[1]), float(at[2])
                pin_name_node = _child(pin, 'name')
                pin_name = pin_name_node[1] if pin_name_node and len(pin_name_node) > 1 else ''
                pin_num_node = _child(pin, 'number')
                pin_num = pin_num_node[1] if pin_num_node and len(pin_num_node) > 1 else ''
                if pin_num and pin_num not in pins:
                    pins[pin_num] = {'name': pin_name, 'type': pin_type, 'lx': lx, 'ly': ly}

        result[lib_id] = {'pins': pins, 'is_power': is_power}
    return result


# ---------------------------------------------------------------------------
# Symbol instance extraction
# ---------------------------------------------------------------------------

_STD_PROPS = {'Reference', 'Value', 'Footprint', 'Datasheet', 'Description',
              'ki_keywords', 'ki_fp_filters', 'ki_description', 'Intersheetrefs'}
_PWR_REF_PREFIXES = ('#PWR', '#FLG')


def _extract_instances(sch: list) -> List[dict]:
    """Return all symbol instances from a sheet node."""
    instances = []
    for sym in _children(sch, 'symbol'):
        lib_id_node = _child(sym, 'lib_id')
        if not lib_id_node:
            continue
        lib_id = lib_id_node[1]

        at = _child(sym, 'at')
        if not at or len(at) < 3:
            continue
        sx = float(at[1])
        sy = float(at[2])
        rot = float(at[3]) if len(at) > 3 else 0.0

        mirror_node = _child(sym, 'mirror')
        mirror_x = mirror_node is not None and len(mirror_node) > 1 and mirror_node[1] == 'x'
        mirror_y = mirror_node is not None and len(mirror_node) > 1 and mirror_node[1] == 'y'

        in_bom_node = _child(sym, 'in_bom')
        in_bom = (in_bom_node[1] != 'no') if in_bom_node and len(in_bom_node) > 1 else True

        # Authoritative reference comes from the instances path section
        reference = None
        inst_node = _child(sym, 'instances')
        if inst_node:
            for proj in _children(inst_node, 'project'):
                for path_node in _children(proj, 'path'):
                    ref = _val(path_node, 'reference')
                    if ref:
                        reference = ref
                        break
                if reference:
                    break
        if not reference:
            for prop in _children(sym, 'property'):
                if len(prop) > 2 and prop[1] == 'Reference':
                    reference = prop[2]
                    break
        if not reference:
            continue

        value = description = datasheet = footprint = ''
        custom: Dict[str, str] = {}
        for prop in _children(sym, 'property'):
            if len(prop) < 3:
                continue
            pname, pval = prop[1], prop[2]
            if pname == 'Value':
                value = pval
            elif pname == 'Footprint':
                footprint = pval
            elif pname == 'Datasheet':
                datasheet = pval
            elif pname == 'Description':
                description = pval
            elif pname not in _STD_PROPS:
                custom[pname] = pval

        unit_node = _child(sym, 'unit')
        unit = int(unit_node[1]) if unit_node and len(unit_node) > 1 else 1

        instances.append({
            'lib_id': lib_id, 'reference': reference,
            'sx': sx, 'sy': sy, 'rot': rot,
            'mirror_x': mirror_x, 'mirror_y': mirror_y,
            'in_bom': in_bom, 'unit': unit,
            'value': value, 'footprint': footprint,
            'datasheet': datasheet, 'description': description,
            'properties': custom,
        })
    return instances


# ---------------------------------------------------------------------------
# Wire / label / no-connect extraction
# ---------------------------------------------------------------------------

def _extract_wires(sch: list) -> List[Tuple]:
    wires = []
    for wire in _children(sch, 'wire'):
        pts = _child(wire, 'pts')
        if not pts:
            continue
        xys = _children(pts, 'xy')
        if len(xys) < 2:
            continue
        p1 = _key(float(xys[0][1]), float(xys[0][2]))
        p2 = _key(float(xys[1][1]), float(xys[1][2]))
        wires.append((p1, p2))
    return wires


def _extract_labels(sch: list) -> List[dict]:
    labels = []
    for tag in ('label', 'global_label', 'hierarchical_label'):
        for node in _children(sch, tag):
            if len(node) < 2:
                continue
            name = node[1]
            at = _child(node, 'at')
            if not at or len(at) < 3:
                continue
            labels.append({
                'kind': tag,
                'name': name,
                'pos': _key(float(at[1]), float(at[2])),
            })
    return labels


def _extract_no_connects(sch: list) -> Set[Tuple]:
    ncs: Set[Tuple] = set()
    for nc in _children(sch, 'no_connect'):
        at = _child(nc, 'at')
        if at and len(at) >= 3:
            ncs.add(_key(float(at[1]), float(at[2])))
    return ncs


def _extract_buses(sch: list) -> List[Tuple]:
    """Extract bus wire segments — same coordinate format as regular wires."""
    buses = []
    for bus in _children(sch, 'bus'):
        pts = _child(bus, 'pts')
        if not pts:
            continue
        xys = _children(pts, 'xy')
        if len(xys) < 2:
            continue
        buses.append((_key(float(xys[0][1]), float(xys[0][2])),
                      _key(float(xys[1][1]), float(xys[1][2]))))
    return buses


def _extract_bus_entries(sch: list) -> List[Tuple]:
    """
    Extract bus_entry stubs as (bus_side, wire_side) pairs.
    KiCad convention: 'at' = the point that touches the bus wire,
    'at + size' = the point that touches the individual signal wire.
    """
    entries = []
    for be in _children(sch, 'bus_entry'):
        at = _child(be, 'at')
        size = _child(be, 'size')
        if not at or not size or len(at) < 3 or len(size) < 3:
            continue
        x, y = float(at[1]), float(at[2])
        dx, dy = float(size[1]), float(size[2])
        entries.append((_key(x, y), _key(x + dx, y + dy)))
    return entries


# ---------------------------------------------------------------------------
# Bus member resolution
# ---------------------------------------------------------------------------

_BUS_RANGE_RE = re.compile(r'^(.+)\[(\d+)\.\.(\d+)\]$')


def _parse_bus_label(name: str) -> Optional[Tuple[str, int, int]]:
    """Parse "Base[low..high]" → (base, low, high), or None if not a bus name."""
    m = _BUS_RANGE_RE.match(name)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def _label_bus_index(label_name: str, base: str, low: int, high: int) -> Optional[int]:
    """
    If label_name is a member of base[low..high], return its 0-based index.
    Example: "DDR_DQ5" in "DDR_DQ[0..15]" → 5.
    """
    if not label_name.startswith(base):
        return None
    suffix = label_name[len(base):]
    try:
        num = int(suffix)
        if low <= high and low <= num <= high:
            return num - low
        if low > high and high <= num <= low:
            return low - num
    except ValueError:
        pass
    return None


def _compute_bus_member_maps(
    labels: list,
    buses: List[Tuple],
    bus_entries: List[Tuple],
    wire_uf: '_UF',
    comp_names: dict,
    wires: List[Tuple],
) -> Dict[str, Dict[int, str]]:
    """
    For each hierarchical_label with a bus name (e.g. "DDR_DQ[0..15]"), find
    which individual net name occupies each bus member index.

    Returns: {hier_label_name → {member_index → net_name}}
    """
    # Build a bus-only union-find (buses + bus_entries as edges)
    bus_uf = _UF()
    for p1, p2 in buses:
        bus_uf.ensure(p1)
        bus_uf.ensure(p2)
        bus_uf.union(p1, p2)
    for bus_side, wire_side in bus_entries:
        bus_uf.ensure(bus_side)
        bus_uf.ensure(wire_side)
        bus_uf.union(bus_side, wire_side)

    # Connect hierarchical bus label positions to the bus UF
    for lbl in labels:
        if lbl['kind'] == 'hierarchical_label' and _parse_bus_label(lbl['name']):
            _connect_point_to_wires(lbl['pos'], buses, bus_uf)

    result: Dict[str, Dict[int, str]] = {}

    for lbl in labels:
        if lbl['kind'] != 'hierarchical_label':
            continue
        parsed = _parse_bus_label(lbl['name'])
        if not parsed:
            continue
        base, low, high = parsed
        pos = lbl['pos']
        if pos not in bus_uf._p:
            continue
        bus_comp = bus_uf.root(pos)

        member_map: Dict[int, str] = {}
        for bus_side, wire_side in bus_entries:
            if bus_uf.root(bus_side) != bus_comp:
                continue
            # wire_side is a wire endpoint — connect it into the wire UF and
            # look up which net it belongs to
            _connect_point_to_wires(wire_side, wires, wire_uf)
            info = comp_names.get(wire_uf.root(wire_side))
            if not info:
                continue
            net_name = info[0]
            idx = _label_bus_index(net_name, base, low, high)
            if idx is not None:
                member_map[idx] = net_name

        if member_map:
            result[lbl['name']] = member_map

    return result


def _extract_sheet_refs(sch: list) -> List[dict]:
    refs = []
    for sheet in _children(sch, 'sheet'):
        filename = None
        for prop in _children(sheet, 'property'):
            if len(prop) > 2 and prop[1] == 'Sheetfile':
                filename = prop[2]
                break
        if not filename:
            continue
        pins = []
        for pin in _children(sheet, 'pin'):
            if len(pin) < 2:
                continue
            at = _child(pin, 'at')
            if at and len(at) >= 3:
                pins.append({'name': pin[1], 'pos': _key(float(at[1]), float(at[2]))})
        refs.append({'filename': filename, 'pins': pins})
    return refs


# ---------------------------------------------------------------------------
# Wire connectivity and net assignment (per sheet)
# ---------------------------------------------------------------------------

_LABEL_PRIORITY = {'global_label': 3, 'label': 2, 'hierarchical_label': 1}
_SEG_TOL = 1e-3  # 1 µm: tolerance for point-on-segment checks


def _on_segment(px: float, py: float,
                ax: float, ay: float, bx: float, by: float) -> bool:
    """True if (px, py) lies on the segment (ax,ay)→(bx,by) within _SEG_TOL."""
    dx, dy = bx - ax, by - ay
    len_sq = dx * dx + dy * dy
    if len_sq < _SEG_TOL * _SEG_TOL:
        return abs(px - ax) < _SEG_TOL and abs(py - ay) < _SEG_TOL
    cross = (px - ax) * dy - (py - ay) * dx
    if abs(cross) > _SEG_TOL * math.sqrt(len_sq):
        return False
    t = ((px - ax) * dx + (py - ay) * dy) / len_sq
    return -_SEG_TOL <= t <= 1.0 + _SEG_TOL


def _connect_point_to_wires(pos: Tuple, wires: List[Tuple], uf: '_UF'):
    """Union pos with any wire segment it lies on (handles midpoint connections)."""
    px, py = pos
    uf.ensure(pos)
    for w_p1, w_p2 in wires:
        if _on_segment(px, py, w_p1[0], w_p1[1], w_p2[0], w_p2[1]):
            uf.union(pos, w_p1)
            return  # One segment match is enough


def _resolve_nets(instances, lib_symbols, wires, labels, nc_positions):
    """
    Build wire graph, assign net names to pins, return per-pin data.

    Returns list of:
        {reference, pin_number, pin_name, pin_type, pos, net_name}
    """
    uf = _UF()

    # Wire graph — connect wire endpoints
    for p1, p2 in wires:
        uf.ensure(p1)
        uf.ensure(p2)
        uf.union(p1, p2)

    # Connect labels and NC positions to the wire graph (may be at midpoints)
    for lbl in labels:
        _connect_point_to_wires(lbl['pos'], wires, uf)
    for pos in nc_positions:
        _connect_point_to_wires(pos, wires, uf)

    # Assign names to connected components
    # comp_root → (name, priority)
    comp_names: Dict[object, Tuple[str, int]] = {}

    for lbl in labels:
        root = uf.root(lbl['pos'])
        pri = _LABEL_PRIORITY.get(lbl['kind'], 0)
        existing = comp_names.get(root)
        if existing is None or pri > existing[1]:
            comp_names[root] = (lbl['name'], pri)

    # Power symbols: treat Value as a label at the pin position
    for inst in instances:
        ref = inst['reference']
        if not any(ref.startswith(p) for p in _PWR_REF_PREFIXES):
            lib_sym = lib_symbols.get(inst['lib_id'], {})
            if not lib_sym.get('is_power', False):
                continue
        lib_sym = lib_symbols.get(inst['lib_id'], {})
        for pin_def in lib_sym.get('pins', {}).values():
            gpos = _transform(pin_def['lx'], pin_def['ly'],
                              inst['sx'], inst['sy'], inst['rot'],
                              inst['mirror_x'], inst['mirror_y'])
            _connect_point_to_wires(gpos, wires, uf)
            root = uf.root(gpos)
            net_name = inst['value']
            existing = comp_names.get(root)
            if existing is None or 2 > existing[1]:
                comp_names[root] = (net_name, 2)

    # Resolve pins for non-power components
    pin_data = []
    seen_pins: Set[Tuple[str, str]] = set()

    for inst in instances:
        ref = inst['reference']
        # Skip power symbols
        if any(ref.startswith(p) for p in _PWR_REF_PREFIXES):
            continue
        lib_sym = lib_symbols.get(inst['lib_id'], {})
        if lib_sym.get('is_power', False):
            continue

        for pin_num, pin_def in lib_sym.get('pins', {}).items():
            dedup_key = (ref, pin_num)
            if dedup_key in seen_pins:
                continue
            seen_pins.add(dedup_key)

            gpos = _transform(pin_def['lx'], pin_def['ly'],
                              inst['sx'], inst['sy'], inst['rot'],
                              inst['mirror_x'], inst['mirror_y'])
            _connect_point_to_wires(gpos, wires, uf)
            root = uf.root(gpos)

            if gpos in nc_positions:
                net_name = '__FNC__'
            else:
                info = comp_names.get(root)
                net_name = info[0] if info else '__NC__'

            pin_data.append({
                'reference': ref,
                'pin_number': pin_num,
                'pin_name': pin_def['name'],
                'pin_type': pin_def['type'],
                'pos': gpos,
                'net_name': net_name,
            })

    return pin_data, uf, comp_names


# ---------------------------------------------------------------------------
# Parse a single sheet file
# ---------------------------------------------------------------------------

def _parse_sheet(path: str, sheet_name: str) -> dict:
    sch = _load(path)
    lib_syms = _extract_lib_symbols(sch)
    instances = _extract_instances(sch)
    wires = _extract_wires(sch)
    buses = _extract_buses(sch)
    bus_entries = _extract_bus_entries(sch)
    labels = _extract_labels(sch)
    nc_positions = _extract_no_connects(sch)
    sheet_refs = _extract_sheet_refs(sch)
    pin_data, wire_uf, comp_names = _resolve_nets(
        instances, lib_syms, wires, labels, nc_positions)
    bus_member_maps = _compute_bus_member_maps(
        labels, buses, bus_entries, wire_uf, comp_names, wires)

    return {
        'path': os.path.abspath(path),
        'sheet_name': sheet_name,
        'instances': instances,
        'lib_symbols': lib_syms,
        'pin_data': pin_data,
        'sheet_refs': sheet_refs,
        'buses': buses,
        'bus_entries': bus_entries,
        'bus_member_maps': bus_member_maps,
    }


# ---------------------------------------------------------------------------
# Root sheet auto-detection
# ---------------------------------------------------------------------------

def _find_root(directory: str) -> Optional[str]:
    """Return the root .kicad_sch file (the one not referenced by any other)."""
    sch_files = [
        f for f in os.listdir(directory)
        if f.endswith('.kicad_sch') and not f.endswith('.kicad_sch-bak')
    ]
    if not sch_files:
        return None
    if len(sch_files) == 1:
        return os.path.join(directory, sch_files[0])

    referenced: Set[str] = set()
    for fname in sch_files:
        try:
            sch = _load(os.path.join(directory, fname))
            for sheet in _children(sch, 'sheet'):
                for prop in _children(sheet, 'property'):
                    if len(prop) > 2 and prop[1] == 'Sheetfile':
                        referenced.add(prop[2])
        except Exception:
            pass

    roots = [f for f in sch_files if f not in referenced]
    chosen = roots[0] if roots else sch_files[0]
    return os.path.join(directory, chosen)


# ---------------------------------------------------------------------------
# Hierarchy traversal
# ---------------------------------------------------------------------------

def _traverse(root_path: str) -> List[dict]:
    all_sheets: List[dict] = []
    visited: Set[str] = set()

    def _visit(path: str, sheet_name: str):
        abs_path = os.path.abspath(path)
        if abs_path in visited:
            return
        visited.add(abs_path)
        data = _parse_sheet(path, sheet_name)
        all_sheets.append(data)
        base = os.path.dirname(abs_path)
        for ref in data['sheet_refs']:
            child_path = os.path.join(base, ref['filename'])
            if os.path.exists(child_path):
                child_name = os.path.splitext(ref['filename'])[0]
                _visit(child_path, child_name)

    root_name = os.path.splitext(os.path.basename(root_path))[0]
    _visit(root_path, root_name)
    return all_sheets


# ---------------------------------------------------------------------------
# Build final JSON structure
# ---------------------------------------------------------------------------

def _make_rename_map(all_sheets: List[dict]) -> Dict[tuple, str]:
    """
    Build a rename map for references that are duplicated across sheets.

    When the same (ref, unit) pair appears in more than one sheet, it means
    two physically distinct components share a reference designator — a
    schematic annotation error.  We disambiguate by prepending the sheet name:
    C1 in DDR3_RAM → DDR3_RAM_C1, C1 in wifi → wifi_C1.

    Returns: {(original_ref, sheet_name): disambiguated_ref}
    """
    ref_unit_sheets: Dict[tuple, List[str]] = defaultdict(list)

    for sheet in all_sheets:
        sheet_name = sheet['sheet_name']
        seen_in_sheet: Set[tuple] = set()
        lib_syms = sheet['lib_symbols']
        for inst in sheet['instances']:
            ref = inst['reference']
            if any(ref.startswith(p) for p in _PWR_REF_PREFIXES):
                continue
            lib_sym = lib_syms.get(inst['lib_id'], {})
            if lib_sym.get('is_power', False):
                continue
            unit = inst.get('unit', 1)
            key = (ref, unit)
            if key not in seen_in_sheet:
                ref_unit_sheets[key].append(sheet_name)
                seen_in_sheet.add(key)

    rename: Dict[tuple, str] = {}
    for (ref, unit), sheet_names in ref_unit_sheets.items():
        unique_sheets = list(dict.fromkeys(sheet_names))  # preserve order, deduplicate
        if len(unique_sheets) <= 1:
            continue
        for sname in unique_sheets:
            # Sanitise sheet name: keep alphanumerics and underscores
            prefix = ''.join(c if c.isalnum() or c == '_' else '_' for c in sname)
            rename[(ref, sname)] = f'{prefix}_{ref}'

    return rename


def _build_netlist(all_sheets: List[dict]) -> dict:
    rename = _make_rename_map(all_sheets)

    nets: Dict[str, List[dict]] = {}
    components: Dict[str, dict] = {}
    seen_comps: Set[str] = set()

    for sheet in all_sheets:
        sheet_name = sheet['sheet_name']
        lib_syms = sheet['lib_symbols']

        def _ref(original: str) -> str:
            return rename.get((original, sheet_name), original)

        # Collect component metadata
        for inst in sheet['instances']:
            orig_ref = inst['reference']
            if any(orig_ref.startswith(p) for p in _PWR_REF_PREFIXES):
                continue
            lib_sym = lib_syms.get(inst['lib_id'], {})
            if lib_sym.get('is_power', False):
                continue
            ref = _ref(orig_ref)
            if ref in seen_comps:
                continue
            seen_comps.add(ref)
            components[ref] = {
                'value': inst['value'],
                'footprint': inst['footprint'],
                'sheet': sheet_name + '.kicad_sch',
                'description': inst['description'],
                'datasheet': inst['datasheet'],
                'properties': inst['properties'],
            }

        # Collect pin→net mappings
        for pin in sheet['pin_data']:
            entry = {
                'ref': _ref(pin['reference']),
                'pin': pin['pin_number'],
                'pin_name': pin['pin_name'],
                'pin_type': pin['pin_type'],
            }
            nets.setdefault(pin['net_name'], []).append(entry)

    # Merge nets that are connected through hierarchical bus sheet pins
    _apply_bus_merges(nets, all_sheets)

    return {'nets': nets, 'components': components}


def _apply_bus_merges(nets: dict, all_sheets: List[dict]) -> None:
    """
    Find cross-sheet bus connections and merge the individual member nets
    in-place.  Example: "DQ0" (processor sheet) and "DDR_DQ0" (DDR3 sheet)
    are connected through the bus sheet pins DQ[0..15] ↔ DDR_DQ[0..15] in
    the parent sheet; they become one net.
    """
    merge_pairs = _build_bus_merge_pairs(all_sheets)
    if not merge_pairs:
        return

    # Union-find over net name strings
    net_uf = _UF()
    for net_a, net_b in merge_pairs:
        net_uf.ensure(net_a)
        net_uf.ensure(net_b)
        net_uf.union(net_a, net_b)

    # Canonical name for each component: alphabetically first (deterministic)
    comp_canonical: Dict[object, str] = {}
    for name in list(net_uf._p):
        root = net_uf.find(name)
        if root not in comp_canonical or name < comp_canonical[root]:
            comp_canonical[root] = name

    def _canonical(name: str) -> str:
        if name not in net_uf._p:
            return name
        return comp_canonical.get(net_uf.find(name), name)

    # Rebuild nets dict with merged names
    merged: Dict[str, list] = {}
    for net_name, pins in list(nets.items()):
        merged.setdefault(_canonical(net_name), []).extend(pins)
    nets.clear()
    nets.update(merged)


def _build_bus_merge_pairs(all_sheets: List[dict]) -> List[Tuple[str, str]]:
    """
    For each parent sheet, find pairs of bus-type sheet pins that are
    connected by a bus wire.  Return (net_a, net_b) merge pairs derived
    from the child sheets' bus_member_maps.
    """
    sheet_by_file: Dict[str, dict] = {
        os.path.basename(s['path']): s for s in all_sheets
    }

    merge_pairs: List[Tuple[str, str]] = []

    for sheet in all_sheets:
        buses = sheet.get('buses', [])
        if not buses:
            continue
        bus_entries = sheet.get('bus_entries', [])
        sheet_refs = sheet.get('sheet_refs', [])

        # Collect bus-type sheet pins: pos → (child_filename, pin_name)
        bus_pins: Dict[Tuple, Tuple[str, str]] = {}
        for ref in sheet_refs:
            for pin in ref['pins']:
                if _parse_bus_label(pin['name']):
                    bus_pins[pin['pos']] = (ref['filename'], pin['name'])

        if len(bus_pins) < 2:
            continue

        # Build bus UF for this parent sheet and connect sheet pin positions
        bus_uf = _UF()
        for p1, p2 in buses:
            bus_uf.ensure(p1); bus_uf.ensure(p2); bus_uf.union(p1, p2)
        for bs, ws in bus_entries:
            bus_uf.ensure(bs); bus_uf.ensure(ws); bus_uf.union(bs, ws)
        for pos in bus_pins:
            _connect_point_to_wires(pos, buses, bus_uf)

        # Group sheet pins by bus component
        comp_to_pins: Dict[object, list] = defaultdict(list)
        for pos, (filename, pin_name) in bus_pins.items():
            if pos in bus_uf._p:
                comp_to_pins[bus_uf.root(pos)].append((filename, pin_name))

        # For each connected group, retrieve member maps and emit merge pairs
        for _comp, group in comp_to_pins.items():
            if len(group) < 2:
                continue
            maps: List[Dict[int, str]] = []
            for filename, pin_name in group:
                child = sheet_by_file.get(filename)
                if not child:
                    continue
                m = child.get('bus_member_maps', {}).get(pin_name)
                if m:
                    maps.append(m)

            if len(maps) < 2:
                continue

            ref_map = maps[0]
            for other_map in maps[1:]:
                for idx, net_a in ref_map.items():
                    net_b = other_map.get(idx)
                    if net_b and net_b != net_a:
                        merge_pairs.append((net_a, net_b))

    return merge_pairs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_kicad_sch(path: str) -> dict:
    """
    Parse a KiCad schematic and return a JSON-serializable netlist dict.

    Args:
        path: Root .kicad_sch file, or a directory (auto-detects root).

    Returns:
        {'nets': {...}, 'components': {...}}
    """
    if os.path.isdir(path):
        root = _find_root(path)
        if not root:
            raise FileNotFoundError(f'No .kicad_sch files found in: {path}')
        path = root

    if not os.path.isfile(path):
        raise FileNotFoundError(f'File not found: {path}')

    all_sheets = _traverse(path)
    return _build_netlist(all_sheets)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(
        description='Parse KiCad schematics into a JSON netlist.'
    )
    ap.add_argument('schematic', help='Root .kicad_sch file or directory')
    ap.add_argument('--output', '-o', metavar='FILE',
                    help='Write JSON to FILE instead of stdout')
    args = ap.parse_args()

    try:
        netlist = parse_kicad_sch(args.schematic)
    except FileNotFoundError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f'Error parsing schematic: {e}', file=sys.stderr)
        raise

    output = json.dumps(netlist, indent=2)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f'Netlist written to {args.output}', file=sys.stderr)
    else:
        print(output)


if __name__ == '__main__':
    main()
