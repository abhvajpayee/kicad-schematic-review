---
name: review-schematic
description: Reviews a KiCad schematic hierarchy for design errors, then cross-references findings against component datasheets to produce specific fix recommendations. Parses all sheets, resolves net connectivity including hierarchical bus connections, fetches and saves datasheets, and reports floating inputs, undriven nets, single-pin nets, missing footprints, and duplicate references — with datasheet-backed suggestions grouped by sheet. Use when the user wants to audit a schematic, check for connectivity issues, review DDR/interface connections, or prepare a design for layout.
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

Auto-detects the root sheet from a directory. Traverses the full hierarchy. Typical runtime under 1 second.

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

## Step 8: Bus False-Positive Classification

| Net name pattern | Resolution status | Action |
|---|---|---|
| `DDR_DQ*`, `DDR_A*`, `DDR_BA*`, `DDR_CTRL*` | ✅ Resolved | Real issue if single-pin |
| `DDR_CKE`, `DDR_ODT`, `DDR_~{RAS}`, `DDR_~{CAS}` | ✅ Resolved | Real issue if single-pin |
| `ETH0_*`, `JTAG*`, `SDMMC1_*` (numeric suffix) | ✅ Resolved | Real issue if single-pin |
| `USART*`, `SPI*`, `I2C*`, `BT_*`, `WL_*`, `SDIO_*` | ⚠️ Not resolved | Likely artifact — verify manually |
| `USB*_N`, `USB*_P` | ⚠️ Not resolved | Likely artifact |
| `PD_*`, `PA_*`, `PH_*` (GPIO banks) | ⚠️ Not resolved | Likely artifact |

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

All signals below should connect both the processor and the DDR3 chip:

| Signal | Net name | Function |
|---|---|---|
| Clock pair | `DDR_CTRL0`, `DDR_CTRL1` | CK+, CK− |
| CKE | `DDR_CTRL2` or `DDR_CKE` | Clock Enable |
| ~RESET | `DDR_CTRL3` | Reset |
| ~CS | `DDR_CTRL4` | Chip Select |
| ~RAS | `DDR_CTRL5` or `DDR_~{RAS}` | Row Strobe |
| ~CAS | `DDR_CTRL6` or `DDR_~{CAS}` | Column Strobe |
| ~WE | `DDR_CTRL7` | Write Enable |
| ODT | `DDR_CTRL8` or `DDR_ODT` | On-Die Termination |
| LDM | `DDR_CTRL9` | Lower Data Mask |
| UDM | `DDR_CTRL10` or `DDR_UDM` | Upper Data Mask |
| LDQS+/− | `DDR_CTRL11`, `DDR_CTRL12` or `DDR_LDQS_*` | Lower Strobe |
| UDQS+/− | `DDR_CTRL13`, `DDR_CTRL14` | Upper Strobe |
| Data | `DDR_DQ0`–`DDR_DQ15` | 16-bit data bus |
| Address | `DDR_A0`–`DDR_A15`, `DDR_BA0`–`DDR_BA2` | Address / bank |

A single-ref net here is a real disconnection — check for label name mismatch (most common: wire carries both a functional label and a bus-indexed label, parser picks the wrong canonical name on one side).

---

## Step 10: Output Format

```
Schematic: STM32MP1.kicad_sch
Sheets: 10  |  Components: 291  |  Named nets: 276
Total findings: 146  (critical: 41, warnings: 105)
Datasheets fetched: 4  (saved to STM32MP1/datasheets/)

## Ethernet.kicad_sch  (13 findings)

### Floating Inputs (9)

U10 pin 36 (XTAL_IN) [__NC__]
  ↳ [RTL8211F-CG datasheet §4.3] Crystal or clock input. Options:
    a) 25 MHz ±20 ppm crystal + two 10–22 pF load caps to GND
    b) External 25 MHz CMOS oscillator
    c) 10 Ω to GND if using CLKSEL = HIGH with external clock on XTAL_OUT

U10 pin 13 (MDC) [__NC__]
  ↳ [RTL8211F-CG datasheet §5.2] MDIO Management Clock — must be driven by
    MAC/processor at ≤ 2.5 MHz. Leaving floating disables PHY management
    interface; cannot read/write PHY registers.

## Action Items

1. 🔴 [Critical] Fix 41 duplicate references — re-annotate schematic
2. 🔴 [Critical] Connect U10 XTAL_IN: 25 MHz crystal with 10–22 pF load caps
3. 🔴 [High]     Connect U10 RGMII Tx (TXD0–TXD3, TXCTL, TXC) from STM32 RGMII
4. 🔴 [High]     Connect U10 MDC/MDIO management interface from STM32
5. 🟡 [Medium]   Verify DDR3_RAM ZQ resistor = 240 Ω ±1% (AS4C256M16D3 Table 25)
6. 🟡 [Medium]   Verify peripheral bus wiring (USART2, SPI5, I2C) in root schematic
```

---

## Parser Resolution Reference

### Resolved correctly ✅
- Regular wires and labels within a sheet
- Global labels (same name on any sheet = same net)
- Hierarchical buses with numeric-suffix members (`DDR_DQ[0..15]`, `DDR_CTRL[0..14]`, etc.)
- Wires with dual labels (functional + bus-indexed on same wire — both tried for index)
- Split hierarchical labels (same bus on left/right sides of a large chip symbol — merged)
- Duplicate refs — auto-renamed with sheet prefix

### Known remaining limitations ⚠️
- Named-suffix buses (`USART2_TX`, `SPI5_MOSI`, `I2C1_SDA`, `BT_TXD`) — positional bus matching not yet implemented
- Net-tie / zero-ohm resistor connections not traced
