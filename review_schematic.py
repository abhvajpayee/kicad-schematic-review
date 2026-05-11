#!/usr/bin/env python3
"""
review_schematic.py - Run design-rule checks on a KiCad schematic hierarchy.

Checks performed:
  1. floating_inputs   - input pins with no net connection (__NC__ or __FNC__)
  2. undriven_nets     - nets with no output/bidirectional driver
  3. single_pin_nets   - named nets with exactly one pin (likely a dangling wire)
  4. missing_footprint - BOM components with no footprint assigned
  5. duplicate_refs    - same reference designator placed more than once (same unit)

Findings are grouped by sheet.  Bus signals crossing hierarchical boundaries may
produce false-positive single_pin_net warnings (known v1 limitation).

Usage:
    python review_schematic.py <root.kicad_sch | directory>
    python review_schematic.py <root.kicad_sch> --output report.json
    python review_schematic.py <root.kicad_sch> --format text
"""

import json
import os
import sys
from collections import defaultdict
from typing import Dict, List

from parse_schematic import _find_root, _traverse, _build_netlist, _make_rename_map  # noqa: F401


# ---------------------------------------------------------------------------
# Driver pin types — a net is "driven" if at least one pin has one of these
# ---------------------------------------------------------------------------

_DRIVER_TYPES = {
    'output', 'bidirectional', 'tristate',
    'open_collector', 'open_emitter', 'open_drain',
    'power_out',
    # passive pins on single-component nets are exempt from undriven check
}

# Pin types that are pure consumers (flag if only these exist on a net)
_INPUT_ONLY_TYPES = {'input', 'power_in'}

# Bus-like net name patterns that generate known false-positive single-pin nets
# (hierarchical bus members that couldn't be resolved across sheets)
import re as _re
_BUS_MEMBER_PATTERN = _re.compile(
    r'^(DDR_|SDMMC|ETH|I2C|SPI|UART|USART|USB|BT_|WL_|JTAG|PD_|PA_|PB_|PC_|PD_|PE_|PF_|PG_|PH_|PI_|PJ_|PK_)',
    _re.IGNORECASE
)


def _is_likely_bus_signal(net_name: str) -> bool:
    """Heuristic: single-pin net that is probably an unresolved bus member."""
    # Bus member notation: ends in a digit after a known bus prefix
    if _BUS_MEMBER_PATTERN.match(net_name) and net_name[-1].isdigit():
        return True
    # Bracketed bus member not resolved
    if '[' in net_name or ']' in net_name:
        return True
    return False


# ---------------------------------------------------------------------------
# Finding collectors
# ---------------------------------------------------------------------------

def _check_floating_inputs(nets: dict, components: dict) -> Dict[str, list]:
    """Pins of type 'input' that land on __NC__ or __FNC__."""
    by_sheet: Dict[str, list] = defaultdict(list)
    for net_name in ('__NC__', '__FNC__'):
        for pin in nets.get(net_name, []):
            if pin['pin_type'] == 'input':
                ref = pin['ref']
                sheet = components.get(ref, {}).get('sheet', 'unknown')
                by_sheet[sheet].append({
                    'ref': ref,
                    'pin': pin['pin'],
                    'pin_name': pin['pin_name'],
                    'net': net_name,
                })
    return dict(by_sheet)


def _check_undriven_nets(nets: dict, components: dict) -> Dict[str, list]:
    """Named nets where every pin is an input (no driver)."""
    by_sheet: Dict[str, list] = defaultdict(list)
    for net_name, pins in nets.items():
        if net_name.startswith('__'):
            continue
        if not pins:
            continue
        has_driver = any(p['pin_type'] in _DRIVER_TYPES for p in pins)
        all_passive = all(p['pin_type'] == 'passive' for p in pins)
        if has_driver or all_passive:
            continue
        # All pins are inputs (or power_in) with no driver → undriven
        # Attribute to the sheet of the first component
        ref0 = pins[0]['ref']
        sheet = components.get(ref0, {}).get('sheet', 'unknown')
        by_sheet[sheet].append({
            'net': net_name,
            'pins': [{'ref': p['ref'], 'pin': p['pin'], 'pin_type': p['pin_type']}
                     for p in pins],
        })
    return dict(by_sheet)


def _check_single_pin_nets(nets: dict, components: dict) -> Dict[str, list]:
    """Named nets with exactly one pin (dangling wire or missing connection)."""
    by_sheet: Dict[str, list] = defaultdict(list)
    for net_name, pins in nets.items():
        if net_name.startswith('__'):
            continue
        if len(pins) != 1:
            continue
        if _is_likely_bus_signal(net_name):
            continue
        ref = pins[0]['ref']
        sheet = components.get(ref, {}).get('sheet', 'unknown')
        by_sheet[sheet].append({
            'net': net_name,
            'ref': ref,
            'pin': pins[0]['pin'],
            'pin_name': pins[0]['pin_name'],
        })
    return dict(by_sheet)


def _check_missing_footprint(components: dict) -> Dict[str, list]:
    """BOM components with an empty footprint field."""
    by_sheet: Dict[str, list] = defaultdict(list)
    for ref, comp in components.items():
        if not comp.get('footprint', '').strip():
            by_sheet[comp.get('sheet', 'unknown')].append({
                'ref': ref,
                'value': comp.get('value', ''),
                'description': comp.get('description', ''),
            })
    return dict(by_sheet)


def _check_duplicate_refs(all_sheets: list) -> Dict[str, list]:
    """
    Report references that were renamed due to duplication across sheets.
    Uses the same rename map as _build_netlist so results are consistent.
    """
    rename = _make_rename_map(all_sheets)
    if not rename:
        return {}

    # Group by original ref to show all affected sheets together
    orig_to_renames: Dict[str, list] = defaultdict(list)
    for (orig_ref, sheet_name), new_ref in rename.items():
        orig_to_renames[orig_ref].append({
            'sheet': sheet_name + '.kicad_sch',
            'renamed_to': new_ref,
        })

    by_sheet: Dict[str, list] = defaultdict(list)
    for orig_ref, entries in orig_to_renames.items():
        # Report under the first sheet alphabetically
        first_sheet = sorted(entries, key=lambda e: e['sheet'])[0]['sheet']
        by_sheet[first_sheet].append({
            'original_ref': orig_ref,
            'renamed': entries,
        })
    return dict(by_sheet)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def review_schematic(path: str) -> dict:
    """
    Run all schematic checks and return a structured report dict.

    Args:
        path: Root .kicad_sch file or directory.

    Returns:
        {
          "summary": {...},
          "by_sheet": {sheet_name: {check_name: [findings]}}
        }
    """
    if os.path.isdir(path):
        root = _find_root(path)
        if not root:
            raise FileNotFoundError(f'No .kicad_sch files in: {path}')
        path = root

    all_sheets = _traverse(path)
    netlist = _build_netlist(all_sheets)
    nets = netlist['nets']
    components = netlist['components']

    findings: Dict[str, dict] = defaultdict(lambda: {
        'floating_inputs': [],
        'undriven_nets': [],
        'single_pin_nets': [],
        'missing_footprint': [],
        'duplicate_refs': [],
    })

    def _merge(check_name: str, by_sheet: dict):
        for sheet, items in by_sheet.items():
            findings[sheet][check_name].extend(items)

    _merge('floating_inputs', _check_floating_inputs(nets, components))
    _merge('undriven_nets', _check_undriven_nets(nets, components))
    _merge('single_pin_nets', _check_single_pin_nets(nets, components))
    _merge('missing_footprint', _check_missing_footprint(components))
    _merge('duplicate_refs', _check_duplicate_refs(all_sheets))

    # Build summary
    total_findings = sum(
        len(v) for sheet_data in findings.values()
        for v in sheet_data.values()
    )
    sheets_with_findings = [s for s, d in findings.items()
                            if any(len(v) > 0 for v in d.values())]

    return {
        'summary': {
            'schematic': os.path.basename(path),
            'sheets_parsed': len(all_sheets),
            'components': len(components),
            'nets': len(nets),
            'named_nets': len([k for k in nets if not k.startswith('__')]),
            'nc_pins': len(nets.get('__NC__', [])),
            'fnc_pins': len(nets.get('__FNC__', [])),
            'total_findings': total_findings,
            'sheets_with_findings': len(sheets_with_findings),
        },
        'by_sheet': {k: dict(v) for k, v in sorted(findings.items())},
    }


# ---------------------------------------------------------------------------
# Text formatter
# ---------------------------------------------------------------------------

def format_text_report(report: dict) -> str:
    lines = []
    s = report['summary']
    lines.append(f"# Schematic Review: {s['schematic']}")
    lines.append(f"Parsed {s['sheets_parsed']} sheets, "
                 f"{s['components']} components, "
                 f"{s['named_nets']} named nets")
    lines.append(f"Found **{s['total_findings']} findings** across "
                 f"{s['sheets_with_findings']} sheets\n")

    check_labels = {
        'floating_inputs':   'Floating Inputs',
        'undriven_nets':     'Undriven Nets',
        'single_pin_nets':   'Single-Pin Nets',
        'missing_footprint': 'Missing Footprint',
        'duplicate_refs':    'Duplicate References',
    }

    for sheet, checks in sorted(report['by_sheet'].items()):
        sheet_total = sum(len(v) for v in checks.values())
        if sheet_total == 0:
            continue
        lines.append(f"## {sheet}  ({sheet_total} findings)")

        fi = checks.get('floating_inputs', [])
        if fi:
            lines.append(f"\n### Floating Inputs ({len(fi)})")
            lines.append("Input pins with no wire — may cause undefined logic levels")
            for f in fi:
                lines.append(f"  - {f['ref']} pin {f['pin']} "
                              f"({f['pin_name'] or 'unnamed'})  [{f['net']}]")

        ud = checks.get('undriven_nets', [])
        if ud:
            lines.append(f"\n### Undriven Nets ({len(ud)})")
            lines.append("Nets with no output/bidirectional driver")
            for f in ud:
                pin_list = ', '.join(f"{p['ref']}.{p['pin']}" for p in f['pins'][:4])
                more = f" +{len(f['pins'])-4} more" if len(f['pins']) > 4 else ''
                lines.append(f"  - {f['net']}: {pin_list}{more}")

        sp = checks.get('single_pin_nets', [])
        if sp:
            lines.append(f"\n### Single-Pin Nets ({len(sp)})")
            lines.append("Nets connected to only one pin — dangling wire or missing connection")
            for f in sp:
                lines.append(f"  - {f['net']}: {f['ref']}.{f['pin']} ({f['pin_name'] or 'unnamed'})")

        mf = checks.get('missing_footprint', [])
        if mf:
            lines.append(f"\n### Missing Footprint ({len(mf)})")
            for f in mf:
                lines.append(f"  - {f['ref']}: {f['value']} — {f['description'][:60]}")

        dr = checks.get('duplicate_refs', [])
        if dr:
            lines.append(f"\n### Duplicate References — auto-renamed ({len(dr)})")
            lines.append("These refs appeared in multiple sheets and have been prefixed with the sheet name")
            for f in dr:
                renames = ', '.join(
                    f"{e['renamed_to']} (in {e['sheet']})"
                    for e in f['renamed']
                )
                lines.append(f"  - {f['original_ref']} → {renames}")

        lines.append('')

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(
        description='Schematic design-rule checker for KiCad .kicad_sch files.'
    )
    ap.add_argument('schematic', help='Root .kicad_sch file or directory')
    ap.add_argument('--output', '-o', metavar='FILE',
                    help='Write JSON report to FILE instead of stdout')
    ap.add_argument('--format', choices=['json', 'text'], default='json',
                    help='Output format (default: json)')
    args = ap.parse_args()

    try:
        report = review_schematic(args.schematic)
    except FileNotFoundError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)

    if args.format == 'text':
        output = format_text_report(report)
    else:
        output = json.dumps(report, indent=2)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f'Report written to {args.output}', file=sys.stderr)
    else:
        print(output)


if __name__ == '__main__':
    main()
