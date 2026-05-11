---
name: review-schematic
description: Reviews a KiCad schematic hierarchy for design errors. Parses all sheets, resolves net connectivity including hierarchical bus connections, and reports floating inputs, undriven nets, single-pin nets, missing footprints, and duplicate references — grouped by sheet. Use when the user wants to audit a schematic, check for connectivity issues, review DDR/interface connections, or prepare a design for layout.
---

# Review Schematic

When this skill is invoked, run a full schematic design-rule check and present the findings clearly to the user.

## Step 1: Locate the Schematic

If the user gave a path, use it directly. If not, ask:

> Which schematic would you like to review? Provide the path to the root `.kicad_sch` file or its directory.

## Step 2: Run the Reviewer

```bash
python review_schematic.py <path> --format text
```

Auto-detects the root sheet from a directory. Traverses the full hierarchy. Typical runtime under 1 second.

For raw JSON when you need to query specific nets or do deeper analysis:
```bash
python review_schematic.py <path> --output /tmp/review.json
```

To query the parsed netlist directly (e.g. to investigate a specific net or component):
```bash
python -c "
from parse_schematic import parse_kicad_sch
netlist = parse_kicad_sch('<path>')
nets = netlist['nets']
comps = netlist['components']
# e.g. show all pins on a net:
for p in nets.get('DDR_CKE', []):
    print(p)
"
```

## Step 3: What the Parser Resolves

Understanding what is and is not resolved avoids misreading the output.

### Resolved correctly ✅
- All regular wires and labels within a sheet
- Global labels (same name on any sheet → same net)
- Hierarchical labels with **numeric-suffix bus members** — e.g. `DDR_DQ[0..15]`, `DDR_A[0..15]`, `DDR_BA[0..2]`, `DDR_CTRL[0..14]`, `SDMMC1_[0..9]`, `ETH0_[0..17]`, `JTAG[0..4]`
- Wires carrying **dual labels** (both a functional name like `DDR_CKE` and a bus-indexed name like `DDR_CTRL2` on the same wire) — both names are tried when resolving bus member indices
- Duplicate hierarchical label instances of the same bus name within one sheet (e.g. a wide BGA chip with its bus split across left and right symbol sides)
- Duplicate reference designators across sheets — **auto-renamed** with a sheet prefix (`C1` in `DDR3_RAM` → `DDR3_RAM_C1`, `C1` in `wifi` → `wifi_C1`)

### Known remaining limitations ⚠️
- **Named-suffix buses** — buses whose members use non-numeric names (`USART2_TX`, `SPI5_MOSI`, `I2C1_SDA`, `BT_TXD`, etc.) are not cross-sheet merged. These appear as single-pin nets. They are real named signals, just not merged across the hierarchy because positional matching along the bus wire is not yet implemented.
- Bus connections through net-tie components or zero-ohm resistors are not traced.

## Step 4: Interpret and Present Findings

### Summary line (always show first)
```
Schematic: <name>
Sheets: N  |  Components: M  |  Named nets: K
Total findings: N  (critical: X, warnings: Y)
```

### Per-sheet sections
```
## SheetName.kicad_sch  (N findings)
```

### Check-by-check guidance

---

#### 🔴 Floating Inputs
**What it means:** A pin with `pin_type = "input"` has no wire connected. In digital circuits this causes undefined logic levels — may indicate a missing pull resistor, broken hierarchical connection, or symbol with wrong pin type annotations.

**Report:**
- `ref`, `pin`, `pin_name`, and marker: `__NC__` (unwired) or `__FNC__` (explicit no-connect by designer)
- `__FNC__` findings may be intentional (unused peripheral pins) but are worth auditing
- Check datasheet for required tie-off: pull to VDD, pull to GND, or connect 10 Ω to GND

**Example:**
```
U10 pin 36 (XTAL_IN) [__NC__]
  ↳ Ethernet PHY crystal input floating. Connect 25 MHz crystal or tie through 10 Ω to GND.
```

---

#### 🔴 Undriven Nets
**What it means:** A named net has only input-type pins — no component drives it. The signal will float.

**Report:**
- Net name and all pins on it with their types
- Common real causes: missing pull resistor, open-collector not modelled, power rail with no `power_out` source visible
- False positive pattern: named-suffix bus signals (USART2_TX, SPI5_SCK, etc.) where the driver is on another sheet connected through a named-suffix bus that isn't fully resolved — check if the net name looks like a peripheral signal

---

#### 🟡 Single-Pin Nets
**What it means:** A named net with exactly one pin — the label goes nowhere. Usually a dangling label or a missing connection.

**Report:**
- Net name, component, pin, pin_name, sheet
- After reporting, classify each finding as **likely real** or **likely bus artifact**:

| Likely real issue | Likely bus artifact (named-suffix bus) |
|---|---|
| `BOOT_0` with only a pull resistor | `USART2_TX`, `SPI5_SCK`, `I2C1_SDA` |
| `PWR_ON` with only one component | `BT_TXD`, `SDIO_CMD`, `WL_HOST_WAKE` |
| Signal that should reach a second chip | Peripheral signal names matching known interface patterns |

Numeric-suffix bus signals (`DDR_DQ0`, `DDR_A3`, `DDR_CTRL2`, `ETH0_0`) are now **correctly resolved** — if one appears as single-pin, it is a genuine disconnection, not a parser artifact.

---

#### 🟡 Missing Footprint
**What it means:** A BOM component has no footprint. Cannot be placed in PCB layout.

**Report:** `ref`, `value`, `description`
- If intentionally footprint-free (simulation element, net tie), mark `dnp` or `exclude_from_board` in the schematic
- Otherwise assign a footprint before layout

---

#### 🟡 Duplicate References — auto-renamed
**What it means:** The same reference designator appeared on components in different sheets. The parser has already **auto-renamed** them by prepending the sheet name so both appear in the netlist. This finding reports what was renamed.

**Report:** original ref → `SheetName_Ref` in each affected sheet (e.g. `C1` → `wifi_C1`, `DDR3_RAM_C1`, `SD_CARD_C1`)

**This is an annotation error in the schematic.** The BOM will list `wifi_C1` and `DDR3_RAM_C1` as separate components but the original schematic has them as `C1` in two places — which one goes to the assembler?

**Fix:** Re-annotate in KiCad (Tools → Annotate Schematic → Reset existing annotations → Annotate). This assigns globally unique references.

---

## Step 5: Severity and Prioritization

| Priority | Check | Why |
|---|---|---|
| 🔴 Critical | Floating inputs | ESD damage or logic faults at runtime |
| 🔴 Critical | Duplicate references | BOM and assembly corruption |
| 🔴 High | Undriven nets (non-bus) | Signal floats — undefined behaviour |
| 🟡 Medium | Single-pin nets (non-bus) | Missing connection — verify before layout |
| 🟡 Medium | Missing footprint | Blocks PCB layout |
| ℹ️ Info | Undriven/single-pin nets matching named-suffix bus patterns | Parser limitation — verify bus connection manually |

## Step 6: Bus False-Positive Classification

Use this table to quickly classify single-pin or undriven findings:

| Net name pattern | Status | Action |
|---|---|---|
| `DDR_DQ*`, `DDR_A*`, `DDR_BA*`, `DDR_CTRL*` | ✅ Resolved | Real issue if single-pin — investigate |
| `DDR_CKE`, `DDR_ODT`, `DDR_~{RAS}`, `DDR_~{CAS}` | ✅ Resolved | Real issue if single-pin |
| `ETH0_*`, `JTAG*`, `SDMMC1_*` (numeric suffix) | ✅ Resolved | Real issue if single-pin |
| `USART*`, `SPI*`, `I2C*`, `BT_*`, `WL_*`, `SDIO_*` | ⚠️ Not resolved | Likely artifact — check bus wiring manually |
| `USB*_N`, `USB*_P` | ⚠️ Not resolved | Likely artifact — check differential pair wiring |
| `PD_*`, `PA_*`, `PH_*` (GPIO banks) | ⚠️ Not resolved | Likely artifact — verify GPIO bus connection |

## Step 7: Output Format

```
Schematic: STM32MP1.kicad_sch
Sheets: 10  |  Components: 291  |  Named nets: 276
Total findings: 146  (critical: 41, warnings: 105)

## DDR3_RAM.kicad_sch  (8 findings)

### Undriven Nets (6)
...

## Ethernet.kicad_sch  (13 findings)

### Floating Inputs (9)
...

## Action Items

1. 🔴 [Critical] Fix 41 duplicate references — re-annotate schematic
2. 🔴 [Critical] U10 XTAL_IN floating — connect crystal or tie through 10 Ω to GND
3. 🔴 [High]     U10 Ethernet TX pins (TXD0–TXD3, TXCTL, TXC, MDC) unconnected
4. 🟡 [Medium]   Review single-pin nets on Processor sheet — check named-suffix bus connections
5. 🟡 [Medium]   J5 missing footprint — assign before layout
```

## Step 8: DDR Interface Analysis

When the user specifically asks about DDR connections, query the netlist directly after running the reviewer:

```python
from parse_schematic import parse_kicad_sch
netlist = parse_kicad_sch('<path>')
nets = netlist['nets']

# Check a specific DDR net
for p in nets.get('DDR_CKE', []):
    print(f"  {p['ref']} pin {p['pin']} ({p['pin_name']}) [{p['pin_type']}]")

# Find nets with only one chip (disconnected DDR signals)
for net, pins in nets.items():
    if 'DDR' not in net: continue
    refs = set(p['ref'] for p in pins)
    if len(refs) == 1 and '__' not in net:
        print(f"Single-ended: {net} → {refs}")
```

Key DDR signals to verify (all should show both the processor and the DDR3 chip):
- Clock: `DDR_CTRL0` (CK+), `DDR_CTRL1` (CK−)
- Commands: `DDR_CTRL2` (CKE), `DDR_CTRL3` (~RESET), `DDR_CTRL4` (~CS), `DDR_CTRL5` (~RAS), `DDR_CTRL6` (~CAS), `DDR_CTRL7` (~WE)
- Termination: `DDR_CTRL8` (ODT)
- Data mask/strobe: `DDR_CTRL9` (LDM), `DDR_CTRL10` (UDM), `DDR_CTRL11` (LDQS+), `DDR_CTRL12` (LDQS−), `DDR_CTRL13` (UDQS+), `DDR_CTRL14` (UDQS−)
- Data: `DDR_DQ0`–`DDR_DQ15`
- Address: `DDR_A0`–`DDR_A15`, `DDR_BA0`–`DDR_BA2`

A net with only one ref in this list indicates a broken connection — check for label name mismatch between the memory and processor sheets (the most common cause is a wire carrying both a functional label and a bus-indexed label where the parser picks the wrong canonical name).

## Example Session

**User:** Review STM32MP1 schematic and check DDR connections

**You:**
```bash
python review_schematic.py STM32MP1/ --format text
```

Present findings:

> **Schematic: STM32MP1.kicad_sch**
> 10 sheets · 291 components · 276 named nets
> **146 findings** across 9 sheets
>
> ### Critical Issues
>
> **Duplicate References** (41 refs) — EMMC, wifi, SD_CARD, DDR3_RAM sheets share reference designators. Auto-renamed in netlist (e.g. `C1` → `wifi_C1`, `DDR3_RAM_C1`).
> - **Action:** Tools → Annotate Schematic in KiCad to assign unique global references.
>
> **Floating Inputs** — Ethernet.kicad_sch
> - U10 (RTL8211F) pins 15–19 (TXD0–TXD3, TXCTL) and pin 20 (TXC), pin 13 (MDC): RGMII Tx signals not driven — check hierarchical connection from processor
> - U10 pin 36 (XTAL_IN): no crystal — tie through 10 Ω to GND or connect 25 MHz crystal
>
> ### DDR3 Interface (DDR3_RAM.kicad_sch)
>
> **Undriven nets** — 6 DDR control signals appear on the DDR3 chip with no driver:
> - `DDR_CTRL10` (UDM), `DDR_~{RAS}`, `DDR_CTRL8` (ODT), `DDR_~{CAS}`, `DDR_CKE`, `ZQ`
> - These are driven from the processor — confirm the `DDR_CTRL[0..14]` bus wire connects both sub-sheets in the root schematic. Dual-label wires (functional + CTRL index on same wire) are now resolved correctly; if still appearing, check the root schematic bus wire continuity.
>
> ### Warnings
>
> **Single-pin nets** on Processor sheet (35): majority are named-suffix peripheral bus signals (USART2, SPI5, I2C, USB, GPIO banks) — not yet resolved across hierarchy. Verify these bus connections manually in the root schematic.
>
> ### Action Items
> 1. 🔴 Re-annotate schematic — 41 duplicate references
> 2. 🔴 Connect RTL8211F RGMII Tx to STM32 RGMII output
> 3. 🔴 Connect or terminate U10 XTAL_IN (25 MHz)
> 4. 🟡 Confirm DDR_CTRL bus wiring in root schematic
> 5. 🟡 Verify peripheral bus connections (USART2, SPI5, I2C, USB) manually
