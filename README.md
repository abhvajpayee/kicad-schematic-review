# kicad-schematic-review

A KiCad schematic parser and design-rule checker. Uses `kicad-cli` as the authoritative connectivity source and runs five automated DRC checks, grouped by sheet.

## Requirements

- Python 3.7+, no third-party packages
- KiCad 7+ with `kicad-cli` on PATH (used for netlist export)

## What it does

**`parse_schematic.py`** — parser and public API

Runs `kicad-cli sch export netlist` to get the authoritative netlist, then parses `.kicad_sch` files only for supplemental component metadata. Produces a flat JSON netlist:

```json
{
  "nets": {
    "GND":             [{"ref": "C1", "pin": "2", "pin_name": "", "pin_type": "passive"}, ...],
    "Net-(U10-MDC)":   [{"ref": "R130", "pin": "1", ...}, {"ref": "U10", "pin": "13", ...}],
    "__NC__":          [...],
    "__FNC__":         [...]
  },
  "components": {
    "U10": {
      "value":       "RTL8211F-CG",
      "footprint":   "RTL8211F-CG:QFN40P500X500X90-41N",
      "sheet":       "Ethernet.kicad_sch",
      "description": "GbE PHY with RGMII interface",
      "datasheet":   "",
      "properties":  {"MANUFACTURER": "Realtek", "MP": "RTL8211F-CG", ...}
    }
  }
}
```

**Net name conventions** after path-prefix stripping:

| KiCad netlist name | Output name | Meaning |
|---|---|---|
| `/GND` | `GND` | Global net |
| `/Ethernet/ETH0_7` | `ETH0_7` | Local net in Ethernet sheet |
| `Net-(U10-MDC)` | `Net-(U10-MDC)` | Unnamed net, anchored to U10 MDC pin |
| `unconnected-(U10-RSET-Pad39)` | `__NC__` | Truly floating (no connection) |
| pintype `input+no_connect` | `__FNC__` | Explicit no-connect marker placed by designer |

**`review_schematic.py`** — design-rule checker

Runs five checks and reports findings grouped by sheet:

| Check | What it catches |
|---|---|
| Floating inputs | `input` pins classified `__NC__` or `__FNC__` |
| Undriven nets | Named nets with no output/bidirectional driver |
| Single-pin nets | Named net connected to exactly one pin (dangling label) |
| Missing footprint | BOM component with no footprint assigned |
| Duplicate references | Same ref designator across multiple sheets (auto-renamed) |

## Usage

```bash
# Parse to JSON (auto-detects root sheet)
python parse_schematic.py STM32MP1/
python parse_schematic.py STM32MP1/STM32MP1.kicad_sch --output netlist.json

# Design-rule check
python review_schematic.py STM32MP1/ --format text
python review_schematic.py STM32MP1/ --output report.json
```

## Python API

```python
from parse_schematic import parse_kicad_sch

netlist    = parse_kicad_sch("path/to/design/")
nets       = netlist["nets"]        # {net_name: [{ref, pin, pin_name, pin_type}]}
components = netlist["components"]  # {ref: {value, footprint, sheet, ...}}
```

```python
from review_schematic import review_schematic, format_text_report

report = review_schematic("path/to/design/")
print(format_text_report(report))
```

## Implementation notes

### Connectivity source

All net connectivity comes from `kicad-cli sch export netlist --format kicadsexpr`. This is the same data KiCad's own ERC and PCB import use — cross-sheet hierarchical connections, bus members, and power nets are all resolved by KiCad itself.

The `.kicad_sch` files are parsed only for supplemental context:
- Component descriptions and custom properties (MPN, manufacturer) that may be richer in the schematic
- Duplicate-reference detection (scanning per-sheet symbol instances to find refs that appear on more than one sheet)

### Why not wire-trace from .kicad_sch?

The previous version used coordinate-based union-find to trace wires, labels, and bus connections directly from the schematic files. This required handling KiCad's Y-up/Y-down coordinate system, midpoint label connections, hierarchical bus member resolution, and unlabeled wire segment naming — and still produced false NC reports for connected-but-unlabeled wire segments. kicad-cli eliminates all of that: it gives every net a name, correctly handles all connection types, and is the authoritative ground truth.

### Duplicate reference disambiguation

When the same reference designator appears on different components in different sheets (annotation error), the ref is prefixed with the sheet name: `C1` in `wifi.kicad_sch` → `wifi_C1`, `C1` in `DDR3_RAM.kicad_sch` → `DDR3_RAM_C1`. Both appear as distinct entries in the netlist.

### Pin name extraction

kicad-cli encodes pin names as `NAME_PINNUM` in the `pinfunction` field (e.g. `MDC_13`, `NRST_CORE_R1`). The parser strips the trailing `_<pin_number>` suffix to recover the clean pin name (`MDC`, `NRST_CORE`).

## Claude Code skill

`.claude/skills/review-schematic/SKILL.md` registers this as a Claude Code skill. Invoke as `/review-schematic` to run the checker, fetch datasheets for flagged ICs, and get datasheet-backed fix recommendations per finding.
