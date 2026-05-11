#!/usr/bin/env python3
"""
parse_schematic.py - Parse KiCad schematics into a JSON netlist.

Connectivity is obtained authoritatively from kicad-cli:
    kicad-cli sch export netlist --format kicadsexpr

The .kicad_sch files are parsed only for supplemental context that is not
available in the netlist: custom component properties, richer descriptions,
and duplicate-reference detection.

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
            "__NC__":  [...],   # floating pins (no wire, net name starts 'unconnected-')
            "__FNC__": [...]    # explicitly no-connected (pintype contains 'no_connect')
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
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# S-expression tokenizer / parser  (used for both netlist and .kicad_sch)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    tokens: List[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in ' \t\n\r':
            i += 1
        elif c == '(':
            tokens.append('('); i += 1
        elif c == ')':
            tokens.append(')'); i += 1
        elif c == '"':
            j = i + 1
            buf: List[str] = []
            while j < n:
                if text[j] == '\\' and j + 1 < n:
                    buf.append(text[j + 1]); j += 2
                elif text[j] == '"':
                    j += 1; break
                else:
                    buf.append(text[j]); j += 1
            tokens.append('\x00' + ''.join(buf))
            i = j
        else:
            j = i
            while j < n and text[j] not in ' \t\n\r()':
                j += 1
            tokens.append(text[i:j]); i = j
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
    with open(path, encoding='utf-8', errors='replace') as f:
        text = f.read()
    tokens = _tokenize(text)
    tree, _ = _parse(tokens, 0)
    return tree


def _children(node: list, tag: str) -> List[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def _child(node: list, tag: str) -> Optional[list]:
    for c in node:
        if isinstance(c, list) and c and c[0] == tag:
            return c
    return None


def _val(node: list, tag: str, default: str = '') -> str:
    c = _child(node, tag)
    return c[1] if c and len(c) > 1 else default


# ---------------------------------------------------------------------------
# Root-sheet auto-detection
# ---------------------------------------------------------------------------

def _find_root(directory: str) -> Optional[str]:
    """Return the root .kicad_sch file (the one not referenced by any other)."""
    sch_files = [
        f for f in os.listdir(directory)
        if f.endswith('.kicad_sch') and not f.startswith('_autosave')
        and not f.endswith('.kicad_sch-bak')
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
# Schematic traversal — context only (no wire tracing)
# ---------------------------------------------------------------------------

_PWR_PREFIXES = ('#PWR', '#FLG')
_STD_PROPS = {'Reference', 'Value', 'Footprint', 'Datasheet', 'Description',
              'ki_keywords', 'ki_fp_filters', 'ki_description', 'Intersheetrefs',
              'Sheetname', 'Sheetfile'}


def _extract_instances_from_sch(sch: list, sheet_name: str) -> List[dict]:
    """Extract component instances from a parsed .kicad_sch for context only."""
    instances = []
    for sym in _children(sch, 'symbol'):
        lib_id_node = _child(sym, 'lib_id')
        if not lib_id_node:
            continue

        # Resolve authoritative reference from instances/path section
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
        if not reference or any(reference.startswith(p) for p in _PWR_PREFIXES):
            continue

        unit_node = _child(sym, 'unit')
        unit = int(unit_node[1]) if unit_node and len(unit_node) > 1 else 1

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

        instances.append({
            'reference': reference,
            'unit': unit,
            'value': value,
            'footprint': footprint,
            'description': description,
            'datasheet': datasheet,
            'properties': custom,
            'sheet_name': sheet_name,
        })
    return instances


def _extract_sheet_refs(sch: list) -> List[str]:
    """Return child sheet filenames referenced from this sheet."""
    filenames = []
    for sheet in _children(sch, 'sheet'):
        for prop in _children(sheet, 'property'):
            if len(prop) > 2 and prop[1] == 'Sheetfile':
                filenames.append(prop[2])
    return filenames


def _traverse(root_path: str) -> List[dict]:
    """
    Walk the schematic hierarchy and collect per-sheet context.

    Returns a list of sheet dicts, each with:
        path, sheet_name, instances (metadata only — no pin/wire data)
    """
    all_sheets: List[dict] = []
    visited: Set[str] = set()

    def _visit(path: str, sheet_name: str):
        abs_path = os.path.abspath(path)
        if abs_path in visited:
            return
        visited.add(abs_path)
        try:
            sch = _load(abs_path)
        except Exception as e:
            print(f'Warning: could not read {path}: {e}', file=sys.stderr)
            return
        instances = _extract_instances_from_sch(sch, sheet_name)
        all_sheets.append({
            'path': abs_path,
            'sheet_name': sheet_name,
            'instances': instances,
        })
        base = os.path.dirname(abs_path)
        for filename in _extract_sheet_refs(sch):
            child_path = os.path.join(base, filename)
            if os.path.exists(child_path):
                child_name = os.path.splitext(filename)[0]
                _visit(child_path, child_name)

    root_name = os.path.splitext(os.path.basename(root_path))[0]
    _visit(root_path, root_name)
    return all_sheets


# ---------------------------------------------------------------------------
# Duplicate-reference detection
# ---------------------------------------------------------------------------

def _make_rename_map(all_sheets: List[dict]) -> Dict[Tuple, str]:
    """
    Build a rename map for references duplicated across sheets.

    Returns {(original_ref, sheet_name): disambiguated_ref}
    """
    ref_unit_sheets: Dict[Tuple, List[str]] = defaultdict(list)

    for sheet in all_sheets:
        sheet_name = sheet['sheet_name']
        seen: Set[Tuple] = set()
        for inst in sheet['instances']:
            key = (inst['reference'], inst['unit'])
            if key not in seen:
                ref_unit_sheets[key].append(sheet_name)
                seen.add(key)

    rename: Dict[Tuple, str] = {}
    for (ref, unit), sheet_names in ref_unit_sheets.items():
        unique = list(dict.fromkeys(sheet_names))
        if len(unique) <= 1:
            continue
        for sname in unique:
            prefix = ''.join(c if c.isalnum() or c == '_' else '_' for c in sname)
            rename[(ref, sname)] = f'{prefix}_{ref}'
    return rename


# ---------------------------------------------------------------------------
# kicad-cli netlist export
# ---------------------------------------------------------------------------

def _export_netlist(root_sch_path: str) -> str:
    """
    Run kicad-cli to export a KiCad s-expression netlist.

    Returns the path to the generated netlist file.
    Raises RuntimeError if kicad-cli fails.
    """
    fd, netlist_path = tempfile.mkstemp(suffix='.net', prefix='kicad_netlist_')
    os.close(fd)
    cmd = [
        'kicad-cli', 'sch', 'export', 'netlist',
        '--format', 'kicadsexpr',
        '--output', netlist_path,
        root_sch_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
    except FileNotFoundError:
        raise RuntimeError(
            'kicad-cli not found. Install KiCad and ensure kicad-cli is on PATH.'
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError('kicad-cli timed out after 120 s')

    if result.returncode != 0:
        raise RuntimeError(
            f'kicad-cli failed (exit {result.returncode}):\n{result.stderr}'
        )
    return netlist_path


# ---------------------------------------------------------------------------
# KiCad netlist parser
# ---------------------------------------------------------------------------

def _strip_pin_num(pinfunction: str, pin_number: str) -> str:
    """Extract pin name from pinfunction field (format: 'NAME_PINNUM')."""
    if not pinfunction or not pin_number:
        return pinfunction
    suffix = '_' + pin_number
    if pinfunction.endswith(suffix):
        return pinfunction[:-len(suffix)]
    return pinfunction


def _normalize_net_name(name: str) -> str:
    """
    Strip KiCad hierarchical path prefix from net names.

    /GND             → GND
    /Ethernet/ETH0_7 → ETH0_7
    Net-(U10-MDC)    → Net-(U10-MDC)   (unchanged, no leading /)
    """
    if name.startswith('/'):
        return name.rsplit('/', 1)[-1]
    return name


def _parse_kicad_netlist(netlist_path: str, rename: Dict[Tuple, str]) -> dict:
    """
    Parse a KiCad s-expression netlist into the standard output schema.

    rename: {(original_ref, sheet_name): new_ref} from _make_rename_map.
            Used to apply the same disambiguation as the schematic traversal.
    """
    nl = _load(netlist_path)

    # Build ref → sheet_name from the components section (for rename lookup)
    ref_to_sheet: Dict[str, str] = {}
    comp_meta: Dict[str, dict] = {}

    comps_node = _child(nl, 'components')
    seen_refs: Dict[str, int] = defaultdict(int)

    if comps_node:
        for comp in _children(comps_node, 'comp'):
            ref_node = _child(comp, 'ref')
            if not ref_node or len(ref_node) < 2:
                continue
            orig_ref = ref_node[1]
            seen_refs[orig_ref] += 1

            val_node = _child(comp, 'value')
            value = val_node[1] if val_node and len(val_node) > 1 else ''

            desc_node = _child(comp, 'description')
            description = desc_node[1] if desc_node and len(desc_node) > 1 else ''

            # Sheetfile and Sheetname from property nodes
            sheetfile = ''
            sheetname = ''
            footprint = ''
            datasheet = ''
            custom: Dict[str, str] = {}

            for prop in _children(comp, 'property'):
                # Format: (property (name "X") (value "Y"))
                pname_node = _child(prop, 'name')
                pval_node  = _child(prop, 'value')
                if not pname_node or len(pname_node) < 2:
                    continue
                pname = pname_node[1]
                pval  = pval_node[1] if pval_node and len(pval_node) > 1 else ''
                if not isinstance(pname, str):
                    continue
                if pname == 'Sheetfile':
                    sheetfile = pval
                elif pname == 'Sheetname':
                    sheetname = pval
                elif pname not in ('ki_keywords', 'ki_fp_filters', 'ki_description',
                                   'Intersheetrefs'):
                    if isinstance(pval, str):
                        custom[pname] = pval

            # Fields section for footprint, datasheet
            fields_node = _child(comp, 'fields')
            if fields_node:
                for field in _children(fields_node, 'field'):
                    name_node = _child(field, 'name')
                    fname = name_node[1] if name_node and len(name_node) > 1 else ''
                    fval = field[2] if len(field) > 2 else ''
                    if isinstance(fval, list):
                        fval = ''
                    if fname == 'Footprint':
                        footprint = fval
                    elif fname == 'Datasheet':
                        datasheet = fval
                    elif fname == 'Description' and not description:
                        description = fval
                    elif fname not in ('Reference', 'Value', 'Footprint',
                                       'Datasheet', 'Description'):
                        if fval:
                            custom.setdefault(fname, fval)

            # Determine sheet for rename lookup
            sname_clean = os.path.splitext(sheetfile)[0] if sheetfile else ''
            ref_to_sheet[orig_ref] = sname_clean
            sheet_display = sheetfile or 'unknown.kicad_sch'

            comp_meta[orig_ref] = {
                'value': value,
                'footprint': footprint,
                'sheet': sheet_display,
                'sheet_name': sname_clean,
                'description': description,
                'datasheet': datasheet,
                'properties': custom,
                '_orig_ref': orig_ref,
            }

    # Apply rename map to components
    # Build ref_to_renamed: orig_ref -> final_ref
    # We need the sheet_name per component to look up in rename map
    ref_to_renamed: Dict[str, str] = {}
    components: Dict[str, dict] = {}
    for orig_ref, meta in comp_meta.items():
        sname = meta['sheet_name']
        new_ref = rename.get((orig_ref, sname), orig_ref)
        ref_to_renamed[orig_ref] = new_ref
        comp_out = {k: v for k, v in meta.items()
                    if not k.startswith('_') and k != 'sheet_name'}
        components[new_ref] = comp_out

    # Parse nets section
    nets: Dict[str, List[dict]] = {}
    nets_node = _child(nl, 'nets')
    if nets_node:
        for net in _children(nets_node, 'net'):
            name_node = _child(net, 'name')
            raw_name = name_node[1] if name_node and len(name_node) > 1 else ''
            net_name = _normalize_net_name(raw_name)

            for node in _children(net, 'node'):
                ref_node = _child(node, 'ref')
                pin_node = _child(node, 'pin')
                pf_node  = _child(node, 'pinfunction')
                pt_node  = _child(node, 'pintype')

                orig_ref = ref_node[1] if ref_node and len(ref_node) > 1 else ''
                pin_num  = pin_node[1] if pin_node and len(pin_node) > 1 else ''
                pf_raw   = pf_node[1]  if pf_node  and len(pf_node)  > 1 else ''
                pin_type = pt_node[1]  if pt_node  and len(pt_node)  > 1 else ''

                pin_name = _strip_pin_num(pf_raw, pin_num)
                ref = ref_to_renamed.get(orig_ref, orig_ref)

                # Classify the pin
                if 'no_connect' in pin_type:
                    target = '__FNC__'
                elif net_name.startswith('unconnected-'):
                    target = '__NC__'
                else:
                    target = net_name

                nets.setdefault(target, []).append({
                    'ref': ref,
                    'pin': pin_num,
                    'pin_name': pin_name,
                    'pin_type': pin_type.replace('+no_connect', '').strip('+'),
                })

    return {'nets': nets, 'components': components}


# ---------------------------------------------------------------------------
# Build final netlist (orchestrates kicad-cli + schematic context)
# ---------------------------------------------------------------------------

def _build_netlist(all_sheets: List[dict]) -> dict:
    """
    Build the final netlist dict.

    Connectivity comes from kicad-cli (authoritative).
    Component descriptions and custom properties are supplemented from
    schematic traversal data where the netlist field is empty.
    """
    if not all_sheets:
        return {'nets': {}, 'components': {}}

    rename = _make_rename_map(all_sheets)
    root_path = all_sheets[0]['path']

    # Export and parse the KiCad netlist
    netlist_path = _export_netlist(root_path)
    try:
        result = _parse_kicad_netlist(netlist_path, rename)
    finally:
        try:
            os.unlink(netlist_path)
        except OSError:
            pass

    # Supplement component metadata from schematic traversal
    # (fill in description/properties that may be richer in the schematic)
    sch_meta: Dict[str, dict] = {}
    for sheet in all_sheets:
        sname = sheet['sheet_name']
        for inst in sheet['instances']:
            orig_ref = inst['reference']
            new_ref = rename.get((orig_ref, sname), orig_ref)
            if new_ref not in sch_meta:
                sch_meta[new_ref] = inst

    for ref, comp in result['components'].items():
        src = sch_meta.get(ref, {})
        if not comp.get('description') and src.get('description'):
            comp['description'] = src['description']
        if not comp.get('datasheet') and src.get('datasheet'):
            comp['datasheet'] = src['datasheet']
        for k, v in src.get('properties', {}).items():
            comp['properties'].setdefault(k, v)

    return result


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
    except RuntimeError as e:
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
