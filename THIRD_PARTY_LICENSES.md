# Third-Party Licenses

## Bundled Software (`tools/`)

### radare2 6.1.0

- **License**: LGPL-3.0-only (GNU Lesser General Public License v3)
- **Source**: https://github.com/radareorg/radare2
- **License files**: `tools/radare2-6.1.0-w64/COPYING.LGPL3`, `tools/radare2-6.1.0-w64/COPYING.GPL3`

### r2ghidra (Ghidra decompiler plugin for radare2)

- **License**: LGPL-3.0 (plugin code)
- **Source**: https://github.com/radareorg/r2ghidra
- **License file**: `tools/radare2-6.1.0-w64/COPYING.LGPL3`

### Ghidra SLEIGH processor definitions

- **License**: Apache-2.0
- **Source**: https://github.com/NationalSecurityAgency/ghidra
- **License file**: `tools/radare2-6.1.0-w64/LICENSE.APACHE2`
- **Files**: `tools/radare2-6.1.0-w64/share/r2ghidra_sleigh/`

## Bundled Reference Data (`livetools/data/`)

### dxvk-remix RTX option reference (`rtx_options.tsv`)

- **License**: zlib/libpng
- **Source**: https://github.com/NVIDIAGameWorks/dxvk-remix (`RtxOptions.md`)
- **Contents**: option names, types, defaults, ranges and descriptions,
  reformatted as TSV. Regenerate with `python -m livetools remix options sync`.

## Python Dependencies (installed via pip, not shipped)

| Package | License | Source |
|---------|---------|--------|
| frida | wxWindows Library Licence 3.1 | https://github.com/frida/frida |
| capstone | BSD-3-Clause | https://github.com/capstone-engine/capstone |
| pefile | MIT | https://github.com/erocarrera/pefile |
| r2pipe | MIT | https://github.com/radareorg/radare2-r2pipe |
