---
name: review-schematic
description: Reviews a KiCad schematic hierarchy for design errors. Parses all sheets, resolves net connectivity, and reports floating inputs, undriven nets, single-pin nets, missing footprints, and duplicate references — grouped by sheet. Use when the user wants to audit a schematic, check for connectivity issues, or review a KiCad design before layout.
---

# Review Schematic

When this skill is invoked, run a full schematic design-rule check and present the findings clearly to the user.

## Step 1: Locate the Schematic

If the user gave a path, use it directly.  If not, ask:

> Which schematic would you like to review? Provide the path to the root `.kicad_sch` file or its directory.

## Step 2: Run the Reviewer

```bash
python review_schematic.py <path> --format text
```

The script auto-detects the root sheet when given a directory and traverses the full hierarchy. It takes under a second for typical designs.

If you need the raw data for further analysis:
```bash
python review_schematic.py <path> --output /tmp/review.json
```

## Step 3: Interpret and Present Findings

### Summary line (always show this first)
```
Parsed N sheets, M components, K nets — N findings across X sheets
```

### For each sheet with findings, show a section:
```
## SheetName.kicad_sch  (N findings)
```

### Check-by-check guidance

---

#### 🔴 Floating Inputs
**What it means:** A pin declared as `input` in the schematic symbol has no wire connected to it. In digital circuits this causes undefined logic levels and usually indicates a missing pull resistor, missing connection, or a symbol with wrong pin types.

**What to tell the user:**
- Each finding gives `ref`, `pin`, `pin_name`, and whether it is `__NC__` (no wire) or `__FNC__` (designer-intentional no-connect)
- `__FNC__` findings may be intentional for unused peripheral pins — but they are still worth auditing
- Check if the component datasheet recommends a pull resistor or a specific tie-off for unconnected inputs

**Example output:**
```
U10 pin 36 (XTAL_IN) — no connection [__NC__]
  ↳ Ethernet PHY crystal input left floating. Tie to GND through 10Ω or connect a crystal.
```

---

#### 🔴 Undriven Nets
**What it means:** A named net has only input-type pins on it — no component drives it. The signal will float or be held only by parasitics.

**What to tell the user:**
- Show the net name and all pins on it
- Common causes: missing pull resistor/capacitor driver, open-collector output not modelled, or a bus signal whose driver is on a different sheet that couldn't be resolved
- Bus hierarchical connections across different-named sheets may produce false positives here — look for net names that contain bus member patterns (DDR_, SDMMC_, ETH_, etc.) and flag those as "verify bus connection in root schematic"

---

#### 🟡 Single-Pin Nets
**What it means:** A named net has exactly one pin — the wire goes nowhere. Usually a dangling label or a missing connection to a second component.

**What to tell the user:**
- Show the net name, the single component pin on it, and the sheet
- **Known false positives:** Hierarchical bus signals (DDR data/address, SDMMC, Ethernet, USB, etc.) that cross between sub-sheets with different label names will appear here. Suppress or note these — look for nets matching bus signal patterns or referencing known interface signals
- Real issues: a label that doesn't match its partner on another sheet, or a component whose second pin was never wired

---

#### 🟡 Missing Footprint
**What it means:** A component marked as in-BOM has no footprint assigned. It cannot be placed in the PCB layout.

**What to tell the user:**
- Show `ref`, `value`, and `description`
- Check if the component is intentionally footprint-free (e.g., a net tie or a simulation-only element) — these should be marked `dnp` or `exclude_from_board` in the schematic
- Otherwise, assign a footprint before starting layout

---

#### 🔴 Duplicate References
**What it means:** The same reference designator appears at the same unit number in multiple locations across sheets. This is almost always a schematic annotation error.

**What to tell the user:**
- Show the ref, unit, and all sheet+position pairs where it appears
- **Exception:** some designs deliberately reuse reference numbers in truly separate sub-systems, but this is bad practice and should be flagged
- To fix: re-annotate the schematic in KiCad (Tools → Annotate Schematic) to assign unique references

---

## Step 4: Severity and Prioritization

Present findings in this order of severity:

| Priority | Check | Why |
|----------|-------|-----|
| 🔴 Critical | Floating inputs | May cause ESD damage or logic faults |
| 🔴 Critical | Duplicate references | Breaks BOM and assembly instructions |
| 🔴 High | Undriven nets | Signal undefined at runtime |
| 🟡 Medium | Single-pin nets | Connection missing (after filtering bus false positives) |
| 🟡 Medium | Missing footprint | Blocks layout — must be fixed before PCB |

## Step 5: Suppression Heuristic for Bus False Positives

Single-pin nets and undriven nets involving these patterns are **likely** unresolved hierarchical bus signals — note them separately rather than treating as critical errors:

- Names matching: `DDR_DQ*`, `DDR_DM*`, `DDR_DQS*`, `DDR_A[0-15]*`
- Names matching: `SDMMC*`, `ETH*_[0-9]*`, `I2C*_[0-1]`, `SPI*`, `USART*`, `USB*`
- Names ending in `[0..N]` or containing `[` / `]`
- Names that are identical between two sheets except for a prefix (e.g., `DQ0` and `DDR_DQ0`)

## Step 6: Output Format

Always start with:
```
Schematic: <name>
Sheets: N  |  Components: M  |  Named nets: K
Total findings: N  (critical: X, warnings: Y)
```

Then group findings by sheet with clear headers. End with a prioritized action list:

```
## Action Items

1. 🔴 Fix duplicate references (R9, R7, R14 appear in multiple sheets) — re-annotate
2. 🔴 U10 crystal input (XTAL_IN) floating — connect or tie to GND
3. 🟡 Review 10 single-pin nets on EMMC sheet — likely hierarchical bus (verify)
4. 🟡 Assign footprints to: R_unnamed, C_test
```

## Example Session

**User:** Review the STM32MP1 schematic

**You:**
```bash
python review_schematic.py STM32MP1/ --format text
```

Parse the output and present:

> **Schematic: STM32MP1.kicad_sch**
> 10 sheets · 230 components · 298 named nets
> **162 findings** across 8 sheets
>
> ### Critical Issues
>
> **Duplicate References** (23 refs) — EMMC, wifi, SD_CARD sheets share reference designators
> - R9, R7, R8 ... appear in both EMMC.kicad_sch and wifi.kicad_sch
> - U2 appears in both EMMC.kicad_sch and SD_CARD.kicad_sch
> - **Action:** Run KiCad's annotation tool to reassign unique references
>
> **Floating Inputs** — Ethernet.kicad_sch
> - U10 (RTL8211F): 7 input pins unconnected (TXD0-TXD3, TXCTL, TXC, MDC)
>   These are RGMII Tx signals — they should come from the processor. Check hierarchical connection.
> - U10 pin 36 (XTAL_IN): No crystal connected. Tie to GND or connect 25MHz crystal.
>
> ### Warnings (review before layout)
>
> **Single-pin nets** on EMMC, wifi, SD_CARD — likely unresolved bus signals; verify connections
> in the root schematic's bus wire between the processor and peripheral sheets.
>
> ### Action Items
> 1. 🔴 Re-annotate schematic to fix 23 duplicate references
> 2. 🔴 Connect RTL8211F TX pins to STM32 RGMII interface
> 3. 🔴 Connect or terminate U10 XTAL_IN
> 4. 🟡 Verify hierarchical bus connections for EMMC/wifi/SD data lines
