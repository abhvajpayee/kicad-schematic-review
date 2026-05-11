# Automated KiCad Schematic Review with kicad-cli and Python

Schematic review is the most skipped step in hardware design. It is tedious to do by hand, easy to rush, and expensive to get wrong — a floating input on a PHY chip or a power supply left unconnected will not show up until the PCB comes back from fab. This post walks through a Python tool that automates the mechanical parts of schematic review, and the lessons learned building it.

---

## The problem with manual review

A modern embedded design can have ten or more schematic sheets, 300+ components, and hundreds of named nets. A careful reviewer checks:

- Every input pin has a driver or pull resistor
- Every power net has a source
- No net label goes nowhere (dangling wire)
- Every BOM component has a footprint
- No reference designator is reused across sheets

Doing this manually against a PDF export takes hours and still misses things — especially across hierarchical sheet boundaries, where a signal named `ETH_MDC` on one sheet connects through a hierarchical label to `ETH0_7` on another.

---

## What the tool does

[`kicad-schematic-review`](https://github.com/abhvajpayee/kicad-schematic-review) is two Python scripts:

**`parse_schematic.py`** calls `kicad-cli` to export the authoritative netlist, then parses it into a flat JSON structure:

```json
{
  "nets": {
    "GND":            [{"ref": "C1",  "pin": "2", "pin_name": "",    "pin_type": "passive"}],
    "Net-(U10-MDC)":  [{"ref": "R130","pin": "1", "pin_name": "",    "pin_type": "passive"},
                       {"ref": "U10", "pin": "13","pin_name": "MDC", "pin_type": "input"}],
    "__NC__":         [{"ref": "U10", "pin": "39","pin_name": "RSET","pin_type": "output"}],
    "__FNC__":        [{"ref": "U8",  "pin": "33","pin_name": "VLXBST","pin_type": "input"}]
  },
  "components": {
    "U10": {
      "value":      "RTL8211F-CG",
      "footprint":  "RTL8211F-CG:QFN40P500X500X90-41N",
      "sheet":      "Ethernet.kicad_sch",
      "properties": {"MANUFACTURER": "Realtek", "MP": "RTL8211F-CG"}
    }
  }
}
```

**`review_schematic.py`** runs five DRC checks over that netlist and groups findings by sheet:

| Check | What it catches |
|---|---|
| Floating inputs | `input` pins with no connection at all |
| Undriven nets | Nets with only input-type pins — no driver anywhere |
| Single-pin nets | A named label that connects to exactly one pin |
| Missing footprint | BOM component with no footprint assigned |
| Duplicate references | Same ref designator on two different components across sheets |

---

## Running it

```bash
# Requirements: Python 3.7+, KiCad 7+ with kicad-cli on PATH

git clone https://github.com/abhvajpayee/kicad-schematic-review
cd kicad-schematic-review

# Text report
python review_schematic.py path/to/design/ --format text

# JSON for scripting
python review_schematic.py path/to/design/ --output report.json

# Query specific nets
python - <<'EOF'
from parse_schematic import parse_kicad_sch
n = parse_kicad_sch("path/to/design/")
for pin in n["nets"].get("__NC__", []):
    print(pin["ref"], pin["pin"], pin["pin_name"])
EOF
```

On a 10-sheet, 294-component STM32MP1 design with two DDR3 chips, a GbE PHY, eMMC, WiFi/BT, and PMIC, it completes in about 3 seconds — almost all of that is kicad-cli startup.

---

## A real example: catching errors on the RTL8211F Ethernet PHY

The STM32MP1 design includes an RTL8211F-CG Gigabit Ethernet PHY. Running the checker against the Ethernet sheet (`Ethernet.kicad_sch`) surfaced 13 findings. After cross-referencing with the RTL8211F-CG datasheet, the real issues were:

### 1. Oscillator VCC shorted to GND

The KC2520Z 25 MHz crystal oscillator (Ethernet_U5) had its VCC pin wired to the GND net:

```
Ethernet_U5 pin 4 (VCC) → GND    ← oscillator receives no power
```

**Impact:** The PHY has no 25 MHz reference clock. Its internal PLL cannot lock. The entire chip is dead.

**Datasheet requirement (RTL8211F-CG §10.3):** When using an external oscillator in this chip, the oscillator output drives `XTAL_OUT/EXT_CLK` (pin 37). `XTAL_IN` (pin 36) must be tied to GND via 10 Ω. The schematic correctly routes KC2520Z CLK → R136 → U10 pin 37; the only error is the power supply.

### 2. XTAL_IN routed to processor instead of GND

Pin 36 (`XTAL_IN`) was wired through R137 → XTAL2 → R138 to the STM32MP1's `ETH_CLK` GPIO, not to GND:

```python
# From the netlist query
nets["Net-(U10-XTAL_IN)"]  # → [R137, U10 pin 36]
nets["XTAL2"]              # → [R137, R138, R45]
nets["ETH_CLK"]            # → [U13 pin AA8, R138]
```

The datasheet is explicit: connect `XTAL_IN` to GND (via 10 Ω) when driving `XTAL_OUT/EXT_CLK` from an external oscillator. The R137/R138 routing appears to be a leftover from an earlier design iteration.

### 3. RSET — the only truly floating pin on U10

After the parser rewrite (more on that below), pin 39 (`RSET`) was the only pin still reported as `__NC__`:

```
unconnected-(U10-RSET-Pad39)   ← kicad-cli's own name for a floating pin
```

`RSET` is the external bias resistor reference. A 12.1 kΩ ±1% resistor to GND is required; without it, the PHY's internal analog bias is undefined.

### 4. Four power supply nets with no source

```
AVDD10: C111, C112, C113, C114, U10.3, U10.8, U10.38  → undriven
AVDD33: C117, C119, C120, C121, U10.11, U10.40         → undriven
1V05_DVDD_GMII: C109, C110, R145, U10.21               → undriven
3V3_DVDD_GMII:  C116, C118, ..., U10.28, U10.29        → undriven
```

All four U10 power domains had decoupling capacitors and filter inductors placed, but the source side of those inductors (L9, L10) had no 3V3 net label attached. The 1.05V core supply (`AVDD10`, `DVDD10`) is generated by the RTL8211F's internal switching regulator from `DVDD33`, so fixing the 3.3V supply also restores the 1.05V path.

### 5. RGMII series resistors connecting signal lines to the 3.3V rail

Three resistors (R120, R121, R122) had one pad on RGMII receive data nets (`ETH_RXD0`, `ETH_RXD1`, `ETH_RXD3`) and the other pad on `3V3_DVDD_GMII`:

```
R120: pin 2 → ETH_RXD0 (STM32MP1 side)
      pin 1 → 3V3_DVDD_GMII (U10 DVDD33 supply net)
```

Each RXD signal already has its correct series resistor to U10 (R114, R113, R111). R120/R121/R122 appear to be a second parallel path that accidentally connects signal lines to the power rail. Depending on value, this either pulls RGMII lines toward 3.3V (signal integrity) or provides a DC current path through signal resistors.

---

## What the checker confirmed was correct

An equally important output is the false-alarm-free confirmation that these connections are properly wired:

| Signal group | Status |
|---|---|
| RGMII TX (TXD0–3, TXCTL, TXC) via R123–R128 | ✅ Connected to U13 |
| RGMII RX (RXD0–3, RXCTL, RXC) via R111–R118 | ✅ Connected to U13 |
| MDC (pin 13) via R130 | ✅ Connected to ETH_MDC → U13 |
| MDIO (pin 14) via R131 | ✅ Connected to ETH_MDIO → U13 |
| INTB (pin 31) via R129 | ✅ Connected to ETH_MDINT → U13 |
| PHY hardware reset (pin 12) | ✅ PHYRSTB → U13 PG0 |
| REG_OUT (pin 30) | ✅ Connected to L7 → 1.05V supply chain |
| XTAL_OUT/EXT_CLK (pin 37) | ✅ KC2520Z CLK → R136 → U10 |
| MDI pairs 0–3 | ✅ ESD arrays → magnetics |

That is 41 pins confirmed correct in under three seconds, with no ambiguity about which direction the series resistors face.

---

## The parser rewrite: why wire-tracing from .kicad_sch was the wrong approach

The first version of `parse_schematic.py` was ~1,100 lines of coordinate-based wire tracing. It worked by:

1. Parsing every `(wire ...)` segment from each `.kicad_sch` file
2. Building a union-find over wire endpoints
3. Connecting pin positions, label positions, and no-connect markers to the wire graph via `_on_segment` checks
4. Propagating net names through the union-find

This worked for simple cases, but had a fundamental flaw that a code review uncovered.

### The bug: unlabeled wire segments all got `__NC__`

When R130 (the 33 Ω MDC series resistor) was parsed, the tool reported:

```
U10 pin 13 (MDC) [__NC__]
```

The user pushed back: "R130's other end is connected to U10 pin 13 in the schematic."

Tracing through the code revealed the issue. The wire between R130 pin 1 and U10 pin 13 existed — 5.08 mm horizontal at y=121.92 mm — and the union-find correctly grouped the two pin positions into the same component. But when assigning net names, the code did:

```python
info = comp_names.get(root)
net_name = info[0] if info else '__NC__'
```

`comp_names` only contained entries for components that had a **labeled** net somewhere on them. The tiny unlabeled wire between a series resistor and an IC pin had no label, so `comp_names.get(root)` returned `None`, and both pins — despite being physically wired together — got assigned `__NC__`.

The parser then dumped all `__NC__` pins into a single bucket, making it impossible to distinguish:
- Pins connected to each other on an unlabeled segment (not floating)
- Pins with literally no wire at all (genuinely floating)

A post-processing fix was added: group `__NC__` pins by UF root, and assign synthetic names to any root with more than one pin. But this was treating the symptom.

### The real fix: use kicad-cli

KiCad already solves this problem. `kicad-cli sch export netlist` gives every wire segment a name — KiCad calls unlabeled segments `Net-(U10-MDC)`, anchored to the most significant pin on that segment. Pins that are truly floating get net names starting with `unconnected-(ref-pinname-padN)`. The distinction is explicit and authoritative.

The rewrite replaced 700+ lines of wire-tracing code with a subprocess call and a netlist parser:

```python
def _export_netlist(root_sch_path: str) -> str:
    fd, netlist_path = tempfile.mkstemp(suffix='.net')
    os.close(fd)
    subprocess.run([
        'kicad-cli', 'sch', 'export', 'netlist',
        '--format', 'kicadsexpr',
        '--output', netlist_path,
        root_sch_path,
    ], check=True, capture_output=True)
    return netlist_path
```

Classification is then three lines:

```python
if 'no_connect' in pin_type:         target = '__FNC__'
elif net_name.startswith('unconnected-'): target = '__NC__'
else:                                 target = net_name
```

The `.kicad_sch` files are still parsed, but only for component metadata (manufacturer, MPN, description) and duplicate-reference detection — no wire tracing.

**Before the rewrite:** 9 false floating-input reports on U10, including MDC, MDIO, all RGMII signals, and REG_OUT.  
**After the rewrite:** 0 false reports. One genuine `__NC__` pin (RSET), confirmed by cross-referencing with the schematic.

---

## Datasheet integration

The tool is designed to be used alongside datasheet research. After running the DRC, flagged ICs are looked up:

```
SCHEMATIC_DIR/datasheets/
  index.md
  RTL8211F-CG/
    datasheet.pdf
    review_notes.md    ← findings cross-referenced with specific datasheet sections
```

`review_notes.md` for each component captures the datasheet requirement alongside the finding, so the reviewer has everything in one place:

```markdown
### RSET (pin 39) — Floating

**Datasheet requirement (RTL8211F-CG §6.8):**
> "External Resistor Reference."

**Recommendation:** Connect a 12.1 kΩ ±1% resistor from RSET (pin 39) to GND.
This sets the internal analog bias current reference. Leaving it floating causes
undefined bias; the PHY analog front-end will not function correctly.
```

---

## Net name conventions

kicad-cli uses hierarchical path-qualified names internally. The parser strips the path prefix to keep net names readable:

| kicad-cli output | Displayed as |
|---|---|
| `/GND` | `GND` |
| `/3V3` | `3V3` |
| `/Ethernet/ETH0_7` | `ETH0_7` |
| `Net-(U10-MDC)` | `Net-(U10-MDC)` |
| `unconnected-(U10-RSET-Pad39)` | classified as `__NC__` |

Pin names come from kicad-cli's `pinfunction` field, which encodes `NAME_PINNUM` (e.g. `MDC_13`, `NRST_CORE_R1`). The parser strips the suffix, handling both numeric (`_13`) and alphanumeric BGA pin numbers (`_R1`, `_AB3`):

```python
def _strip_pin_num(pinfunction: str, pin_number: str) -> str:
    suffix = '_' + pin_number
    if pinfunction.endswith(suffix):
        return pinfunction[:-len(suffix)]
    return pinfunction
```

---

## Claude Code integration

The tool ships as a Claude Code skill (`/review-schematic`). When invoked, Claude runs the DRC, identifies which flagged ICs need datasheet research, fetches and saves the relevant PDFs, and presents findings with specific datasheet-backed fix recommendations.

```
/review-schematic Ethernet IC (RTL8211F) connections in ./STM32MP1
```

Output is structured as actionable items grouped by severity, with datasheet citations:

```
## Ethernet.kicad_sch  (13 findings)

### Undriven Nets (13)

Net-(U10-XTAL_IN): R137.1, U10.36
  ↳ [RTL8211F-CG §10.3] External oscillator mode requires XTAL_IN tied to GND
    via 10 Ω. Currently routed to U13 ETH_CLK via R137/R138 — replace with GND.

## Action Items

1. 🔴 [Critical] Fix KC2520Z VCC: disconnect from GND, connect to 3V3
2. 🔴 [Critical] Tie U10 XTAL_IN (pin 36) to GND via 10 Ω
3. 🔴 [Critical] Add 12.1 kΩ ±1% from U10 RSET (pin 39) to GND
4. 🔴 [Critical] Connect 3V3 to L9/L10 — AVDD33/DVDD33 are undriven
```

---

## Source

[github.com/abhvajpayee/kicad-schematic-review](https://github.com/abhvajpayee/kicad-schematic-review)

Requirements: Python 3.7+, KiCad 7+ (`kicad-cli` on PATH).
