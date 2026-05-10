# kicad-schematic-review

A KiCad schematic parser and design-rule checker. Parses `.kicad_sch` hierarchies into a flat JSON netlist and runs five automated checks, grouped by sheet.

## What it does

**`parse_schematic.py`** — parser and public API

Traverses a full KiCad schematic hierarchy (root sheet + all sub-sheets), resolves wire connectivity via coordinate-based union-find, and produces a JSON netlist:

```json
{
  "nets": {
    "GND": [{"ref": "C1", "pin": "2", "pin_name": "", "pin_type": "passive"}, ...],
    "__NC__":  [...],
    "__FNC__": [...]
  },
  "components": {
    "U1": {
      "value": "STM32MP135D",
      "footprint": "Package_BGA:BGA-448",
      "sheet": "STM32MP1_Processor.kicad_sch",
      "description": "...",
      "datasheet": "...",
      "properties": {"MPN": "...", "Manufacturer": "..."}
    }
  }
}
```

**`review_schematic.py`** — design-rule checker

Runs five checks and reports findings grouped by sheet:

| Check | What it catches |
|-------|----------------|
| Floating inputs | `input` pins with no wire attached |
| Undriven nets | Nets with no output/bidirectional driver |
| Single-pin nets | Named net connected to only one pin (dangling wire) |
| Missing footprint | BOM component with no footprint assigned |
| Duplicate references | Same ref designator on different components across sheets (auto-renamed) |

## Usage

```bash
# Parse a schematic to JSON (auto-detects root sheet from directory)
python parse_schematic.py STM32MP1/
python parse_schematic.py STM32MP1/STM32MP1.kicad_sch --output netlist.json

# Run the design-rule checker
python review_schematic.py STM32MP1/ --format text
python review_schematic.py STM32MP1/ --output report.json
```

## Python API

```python
from parse_schematic import parse_kicad_sch

netlist = parse_kicad_sch("path/to/design/")
# or
netlist = parse_kicad_sch("path/to/root.kicad_sch")

nets       = netlist["nets"]        # {net_name: [{ref, pin, pin_name, pin_type}]}
components = netlist["components"]  # {ref: {value, footprint, sheet, ...}}
```

```python
from review_schematic import review_schematic, format_text_report

report = review_schematic("path/to/design/")
print(format_text_report(report))
```

## Implementation notes

### Coordinate system
KiCad lib_symbols use a Y-up coordinate system while schematics use Y-down. Pin positions from lib_symbols have their Y coordinate negated before transformation to schematic coordinates. Getting this wrong silently swaps power supply and GND connections.

### Wire connectivity
Labels and pins connect to wires at any point along the wire — not just at endpoints. Each connection point is checked against all wire segments (`_on_segment`) and unioned into the same connected component if it lies on any wire.

### Duplicate reference disambiguation
When the same reference designator appears on components in different sheets (annotation error), the ref is automatically prefixed with the sheet name: `C1` in `wifi.kicad_sch` becomes `wifi_C1`, `C1` in `DDR3_RAM.kicad_sch` becomes `DDR3_RAM_C1`. Both appear as distinct entries in the netlist.

### Known limitation — bus hierarchical connections
Nets connected through hierarchical bus sheet pins (e.g. `DDR_DQ[0..15]` spanning processor and memory sheets) are not merged when the bus member names differ between sheets (`DQ0` vs `DDR_DQ0`). These appear as single-pin nets in the output and produce false-positive warnings in the reviewer. Cross-sheet bus members with matching names (via global labels) are merged correctly.

## Requirements

Python 3.7+ — no external dependencies.

## Claude Code skill

The `.claude/skills/review-schematic/SKILL.md` file registers this as a Claude Code skill. When invoked as `/review-schematic`, Claude runs the checker and presents the findings with severity ratings and suggested actions.
