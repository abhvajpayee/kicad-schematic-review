---
name: review-schematic
description: Reviews a KiCad schematic hierarchy for design errors, then cross-references findings against component datasheets to produce specific fix recommendations. Uses kicad-cli for authoritative net connectivity, fetches and saves datasheets, and reports floating inputs, undriven nets, single-pin nets, missing footprints, and duplicate references — with datasheet-backed suggestions grouped by sheet. Use when the user wants to audit a schematic, check for connectivity issues, review DDR/interface connections, or prepare a design for layout.
---

# Review Schematic

When this skill is invoked, run a full DRC, look up datasheets for flagged components, and present findings with specific recommendations.

---

## Step 1: Locate the Schematic

If the user gave a path, use it directly. If not, ask:

> Which schematic would you like to review? Provide the path to the root `.kicad_sch` file or its directory.

Determine `SCHEMATIC_DIR` (the directory containing the `.kicad_sch` files) — all datasheets are saved relative to this.

---

## Step 2: Run the Reviewer

```bash
python review_schematic.py <path> --output /tmp/review.json
python review_schematic.py <path> --format text
```

Auto-detects the root sheet from a directory. Calls `kicad-cli` internally for connectivity — requires KiCad 7+ on PATH. Typical runtime 2–5 seconds (kicad-cli startup dominates).

To query specific nets after the review:
```bash
python -c "
from parse_schematic import parse_kicad_sch
netlist = parse_kicad_sch('<path>')
nets, comps = netlist['nets'], netlist['components']
for p in nets.get('NET_NAME', []):
    print(p)
"
```

Net names match KiCad's internal naming with the sheet path prefix stripped:
- `/Ethernet/ETH0_7`  →  `ETH0_7`
- `Net-(U10-MDC)`     →  `Net-(U10-MDC)`  (unnamed nets keep KiCad's anchor name)
- `unconnected-(...)` →  classified as `__NC__` automatically

---

## Step 3: Identify Components Needing Datasheet Lookup

From the review output, collect the unique **IC references** (prefix U, Y, TR, IC — not R, C, L, D) that appear in any finding. Prioritise in this order:

1. **ICs with floating inputs** — datasheet specifies required pull resistors, tie-offs, or external components for each unused pin
2. **ICs on undriven power nets** — datasheet specifies supply voltage, sequencing, and decoupling requirements
3. **ICs on critical single-pin nets** — datasheet specifies interface partner requirements
4. **Passive components with questionable values** (e.g. calibration resistors) — datasheet specifies exact value and tolerance

Skip components already resolved (DDR DQ/A/CTRL signals, clock pairs) unless the user specifically asks about them.

---

## Step 4: Fetch and Save Datasheets

### Directory structure

Save datasheets under `SCHEMATIC_DIR/datasheets/` using the component **value** (not reference) as the folder name:

```
SCHEMATIC_DIR/datasheets/
  index.md                      ← master index of all fetched datasheets
  RTL8211F-CG/
    datasheet.pdf               ← original PDF
    review_notes.md             ← extracted pin requirements relevant to findings
  AS4C256M16D3/
    datasheet.pdf
    review_notes.md
  STM32MP157DAAx/
    datasheet.pdf
    review_notes.md
  STPMIC1APQR/
    datasheet.pdf
    review_notes.md
```

### Search and download procedure

For each component value:

**4a. Search for the datasheet**
```
WebSearch: "<component_value> datasheet filetype:pdf"
WebSearch: "<component_value> datasheet site:st.com"          (for ST parts)
WebSearch: "<component_value> datasheet site:alliancememory.com"  (DDR)
WebSearch: "<component_value> datasheet site:realtek.com"     (Ethernet PHY)
```
Prefer the manufacturer's own site, then Mouser/Digikey product pages which link directly to PDFs.

**4b. Download the datasheet**
```
WebFetch: <datasheet_pdf_url>
```
Save the result to `SCHEMATIC_DIR/datasheets/<VALUE>/datasheet.pdf`.

If a PDF is not directly fetchable, save the datasheet page URL and extract the relevant sections as text.

**4c. Read the relevant sections**

For each finding on this component, read the datasheet section that covers the flagged pin:
- Look for: "Pin Description", "Application Circuit", "Unused Pins", "Absolute Maximum Ratings", "Electrical Characteristics"
- Search within the document for the pin name or number

**4d. Write `review_notes.md`**

```markdown
# <COMPONENT_VALUE> — Review Notes

Source: <datasheet URL or filename>
Fetched: <date>

## Findings from schematic review

### <PIN_NAME> (pin <NUMBER>) — Floating Input
**Datasheet requirement (§<section>):**
> "<exact quote from datasheet>"

**Recommendation:** <specific fix>

### VDD supply — Undriven net
**Datasheet requirement:**
> "<exact quote>"

**Recommendation:** <specific fix with values>
```

**4e. Update `datasheets/index.md`**

```markdown
# Datasheet Index

| Component | Folder | Source | Date | Notes |
|---|---|---|---|---|
| RTL8211F-CG | [RTL8211F-CG/](RTL8211F-CG/) | realtek.com | 2026-05-11 | XTAL, RGMII requirements |
| AS4C256M16D3 | [AS4C256M16D3/](AS4C256M16D3/) | alliancememory.com | 2026-05-11 | ZQ resistor, ODT |
```

---

## Step 5: Augment Findings with Datasheet Recommendations

For each finding that has a corresponding `review_notes.md`, append the datasheet-specific recommendation directly below the finding in the output. Format:

```
U10 pin 36 (XTAL_IN) [__NC__]
  ↳ [Datasheet RTL8211F-CG §4.3] Crystal input. Requires either:
    a) 25 MHz ±20 ppm crystal with two 10–22 pF load capacitors to GND, or
    b) External 25 MHz CMOS oscillator (3.3 V LVCMOS), or
    c) 10 Ω resistor to GND if using internal reference clock mode (requires
       setting CLKSEL pin HIGH and driving XTAL_OUT with external clock).
```

If no datasheet section covers the specific pin, still note which sections were checked.

---

## Step 6: Interpret and Present Findings

### Summary line (always show first)
```
Schematic: <name>
Sheets: N  |  Components: M  |  Named nets: K
Total findings: N  (critical: X, warnings: Y)
Datasheets fetched: N
```

### Per-sheet sections
```
## SheetName.kicad_sch  (N findings)
```

### Check-by-check guidance

---

#### 🔴 Floating Inputs
**What it means:** A pin with `pin_type = "input"` has no wire connected. Causes undefined logic levels — may indicate a missing pull resistor, broken connection, or wrong pin type in the symbol.

**Report:** `ref`, `pin`, `pin_name`, marker (`__NC__` or `__FNC__`), then datasheet recommendation.

`__FNC__` (explicit no-connect by designer) may be intentional but still audit — some ICs require unused inputs to be tied, not left open.

---

#### 🔴 Undriven Nets
**What it means:** A named net has only input-type pins — no component drives it.

**Report:** Net name, all pins and their types, datasheet power-supply or signal-source requirement.

False positive pattern: named-suffix bus signals (USART2_TX, SPI5_SCK, etc.) — driver is on another sheet not yet resolved. Check net name pattern against the bus classification table below.

---

#### 🟡 Single-Pin Nets
**What it means:** A named net with exactly one pin — label goes nowhere.

**Report:** Net name, component, pin, pin_name, sheet, then classify:

| Likely real issue | Likely bus artifact |
|---|---|
| `BOOT_0` with only a pull resistor | `USART2_TX`, `SPI5_SCK`, `I2C1_SDA` |
| DDR_DQ / DDR_CTRL / ETH0_* (numeric) | `BT_TXD`, `SDIO_CMD`, GPIO bank signals |

Numeric-suffix bus signals are **resolved** — single-pin findings on these are real.

---

#### 🟡 Missing Footprint
**Report:** `ref`, `value`, `description`. If the datasheet specifies a package, include it:
```
U5 (FC905AAAMD) — no footprint
  ↳ [Datasheet] Available in QFN-24 (4×4 mm, 0.5 mm pitch)
```

---

#### 🟡 Duplicate References — auto-renamed
Duplicate refs across sheets are **auto-renamed** with a sheet prefix in the netlist (`C1` → `wifi_C1`, `DDR3_RAM_C1`). The finding reports what was renamed.

**Fix:** Tools → Annotate Schematic in KiCad → Reset and re-annotate.

---

## Step 7: Severity and Prioritization

| Priority | Check | Why |
|---|---|---|
| 🔴 Critical | Floating inputs | ESD damage or logic faults |
| 🔴 Critical | Duplicate references | BOM and assembly corruption |
| 🔴 High | Undriven nets (non-bus) | Signal floats at runtime |
| 🟡 Medium | Single-pin nets (non-bus) | Missing connection |
| 🟡 Medium | Missing footprint | Blocks PCB layout |
| ℹ️ Info | Named-suffix bus nets | Parser limitation — verify manually |

---

## Step 8: Net Name Conventions

kicad-cli strips hierarchical path prefixes in the netlist. After `_normalize_net_name`:

| Raw KiCad net name | Displayed as | Meaning |
|---|---|---|
| `/GND` | `GND` | Global net |
| `/Ethernet/ETH0_7` | `ETH0_7` | Local net in Ethernet sheet |
| `Net-(U10-MDC)` | `Net-(U10-MDC)` | Unnamed net anchored to U10 MDC pin |
| `unconnected-(U10-RSET-Pad39)` | `__NC__` | Truly floating pin |
| pin with `pintype "input+no_connect"` | `__FNC__` | Explicit no-connect marker |

Single-pin net false-positive patterns (hierarchical bus signals with no partner on the local sheet):

| Pattern | Likely artifact | Action |
|---|---|---|
| `ETH0_*`, `DDR_*`, `SDMMC*` with numeric suffix | Bus member, other end on different sheet | Cross-check against both chips |
| `USART*`, `SPI*`, `I2C*`, `BT_*` | Peripheral signal, other end on processor sheet | Verify manually in schematic |
| `USB*_N`, `USB*_P`, `PA_*`, `PB_*` (GPIO banks) | Same as above | Verify manually |

---

## Step 9: DDR Interface Analysis

When the user asks about DDR connections specifically:

```python
from parse_schematic import parse_kicad_sch
netlist = parse_kicad_sch('<path>')
nets = netlist['nets']

# Find DDR nets with only one chip connected (broken connections)
for net, pins in nets.items():
    if 'DDR' not in net or net.startswith('__'): continue
    refs = set(p['ref'] for p in pins)
    if len(refs) == 1:
        print(f"BROKEN: {net} → only {refs}")
```

All signals below should connect both the processor and the DDR3 chip. Net names reflect KiCad's internal naming after path-prefix stripping — check the actual schematic if a net name seems unexpected.

---

## Step 10: Output Format

```
Schematic: STM32MP1.kicad_sch
Sheets: 10  |  Components: 294  |  Named nets: 370
Total findings: 116  (critical: X, warnings: Y)
Datasheets fetched: 1  (saved to STM32MP1/datasheets/)

## Ethernet.kicad_sch  (13 findings)

### Undriven Nets (13)

Net-(U10-XTAL_IN): R137.1, U10.36
  ↳ [RTL8211F-CG §10.3] XTAL_IN must be tied to GND via 10 Ω in external-oscillator
    mode. Currently routed to processor ETH_CLK via R137/R138 — replace with GND.

Net-(U10-MDC): R130.1, U10.13
  ↳ [RTL8211F-CG §7.11.2] MDC driven by MAC (ETH_MDC net via R130). Net between
    R130 and U10 has no driver — this is the series resistor connection, not an error.
    Mark as info if confirmed series resistor is intentional.

## Action Items

1. 🔴 [Critical] Fix 41 duplicate references — re-annotate schematic
2. 🔴 [Critical] Fix KC2520Z (Ethernet_U5): VCC shorted to GND; INH floating
3. 🔴 [Critical] Connect XTAL_IN (U10 pin 36) to GND via 10 Ω (external-oscillator mode)
4. 🔴 [Critical] Add 12.1 kΩ ±1% from U10 RSET (pin 39) to GND
5. 🔴 [High]     Connect 3V3 to L9/L10 — AVDD33/DVDD33 are undriven
6. 🟡 [Medium]   Verify R120/R121/R122: one pad on 3V3_DVDD_GMII (may short signal to supply)
```

---

## Connectivity source

Connectivity comes from `kicad-cli sch export netlist --format kicadsexpr`. This is KiCad's own netlist — the same data ERC and PCB layout use. All cross-sheet and hierarchical connections are resolved correctly by KiCad.

The `.kicad_sch` files are parsed only for:
- Component descriptions and custom properties (MPN, manufacturer) not always in the netlist
- Duplicate-reference detection (scanning per-sheet symbol instances)

**Pin classification:**
- `pintype "input+no_connect"` in the netlist → `__FNC__` (explicit no-connect marker)
- Net name `unconnected-(ref-pinname-padN)` → `__NC__` (genuinely floating, no connection)
- Anything else → named net (path prefix stripped)
