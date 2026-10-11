"""Tests for switch/jump table resolution in cfg.py."""

import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock


from capstone import CS_ARCH_X86, CS_MODE_32, Cs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retools"))


class TestResolveSwitch:
    def test_importable(self):
        from cfg import _resolve_switch

        assert callable(_resolve_switch)

    def test_pattern_a_direct_table(self):
        """Pattern A: cmp ecx,N / ja default / jmp [table+ecx*4]."""
        from cfg import _resolve_switch
        from common import Binary

        # Build a minimal code + jump table:
        # 0x401000: cmp ecx, 3
        # 0x401003: ja 0x401020 (default)
        # 0x401005: jmp [0x401100 + ecx*4]
        # Jump table at 0x401100
        cmp_bytes = b"\x83\xf9\x03"  # cmp ecx, 3
        ja_bytes = b"\x77\x1b"  # ja +0x1B (relative)
        # FF 24 8D 00114000 = jmp dword ptr [ecx*4 + 0x401100]
        jmp_bytes = b"\xff\x24\x8d" + struct.pack("<I", 0x401100)

        code_at_1000 = cmp_bytes + ja_bytes + jmp_bytes
        # Pad to offset 0x100 for the jump table
        padding = b"\x90" * (0x100 - len(code_at_1000))
        table_entries = struct.pack("<4I", 0x401010, 0x401014, 0x401018, 0x40101C)  # noqa
        raw = code_at_1000 + padding + table_entries + b"\x00" * 0x100

        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        cs.detail = True
        insns = list(cs.disasm(code_at_1000, 0x401000))
        # Find the jmp instruction
        jmp_insn = [i for i in insns if i.mnemonic == "jmp"][0]

        # Build a mock Binary with read_va and in_exec
        b = MagicMock(spec=Binary)
        b.is_64 = False
        b.ptr_size = 4
        b.base = 0x400000

        def mock_read_va(va, size):
            off = va - 0x401000
            if 0 <= off < len(raw):
                return raw[off : off + size]  # noqa: E203
            return b""

        b.read_va = mock_read_va
        b.in_exec = lambda va: 0x401000 <= va < 0x402000

        targets = _resolve_switch(b, jmp_insn, insns)
        assert targets is not None
        assert set(targets) == {0x401010, 0x401014, 0x401018, 0x40101C}

    def test_non_switch_jmp_returns_none(self):
        """A regular jmp eax should not resolve as a switch."""
        from cfg import _resolve_switch

        # FF E0 = jmp eax (register, not memory)
        code = b"\xff\xe0"
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        cs.detail = True
        insns = list(cs.disasm(code, 0x401000))  # noqa
        b = MagicMock()
        b.is_64 = False
        targets = _resolve_switch(b, insns[0], insns)
        assert targets is None


class TestBuildCfgWithSwitch:
    def test_empty_disasm(self):
        """build_cfg with empty disassembly shouldn't crash."""
        from cfg import build_cfg
        from common import Binary

        b = MagicMock(spec=Binary)
        b.disasm.return_value = []
        blocks, edges = build_cfg(b, 0x401000)
        assert blocks == {}
        assert edges == []

    def test_basic_branching(self):
        """Test build_cfg with basic branching (cmp, je, mov, nop, ret)."""
        from cfg import build_cfg
        from common import Binary

        # 0x401000: cmp eax, 0
        # 0x401003: je 0x40100a
        # 0x401005: mov ebx, 1
        # 0x40100a: nop
        # 0x40100b: ret
        cmp_bytes = b"\x83\xf8\x00"  # cmp eax, 0
        je_bytes = b"\x74\x05"  # je +0x05 (target 0x40100a)
        mov_bytes = b"\xbb\x01\x00\x00\x00"  # mov ebx, 1
        nop_bytes = b"\x90"  # nop
        ret_bytes = b"\xc3"  # ret

        code = cmp_bytes + je_bytes + mov_bytes + nop_bytes + ret_bytes

        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        cs.detail = True
        insns = list(cs.disasm(code, 0x401000))  # noqa

        b = MagicMock(spec=Binary)
        b.disasm.return_value = insns

        blocks, edges = build_cfg(b, 0x401000)

        # Leaders: 0x401000, 0x401005 (fall-thru), 0x40100a
        assert 0x401000 in blocks
        assert 0x401005 in blocks
        assert 0x40100A in blocks
        assert len(blocks) == 3

        # Edges should be:
        # 0x401000 -> 0x40100a (je)
        # 0x401000 -> 0x401005 (fall)
        # 0x401005 -> 0x40100a (fall)
        assert (0x401000, 0x40100A, "je") in edges
        assert (0x401000, 0x401005, "fall") in edges
        assert (0x401005, 0x40100A, "fall") in edges
        assert len(edges) == 3

    def test_resolve_switch_no_index_or_bad_scale(self):
        """Test with jmp [eax] where there is no index/scale."""
        from cfg import _resolve_switch

        code = b"\xff\x20"  # jmp dword ptr [eax]
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        cs.detail = True
        insns = list(cs.disasm(code, 0x401000))  # noqa
        b = MagicMock()
        b.is_64 = False
        targets = _resolve_switch(b, insns[0], insns)
        assert targets is None

    def test_resolve_switch_no_disp(self):
        """Test with jmp [ecx*4] where there is no displacement (table base)."""
        from cfg import _resolve_switch

        # jmp dword ptr [ecx*4]
        code = b"\xff\x24\x8d\x00\x00\x00\x00"
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        cs.detail = True
        insns = list(cs.disasm(code, 0x401000))  # noqa
        b = MagicMock()
        b.is_64 = False
        targets = _resolve_switch(b, insns[0], insns)
        assert targets is None

    def test_resolve_switch_jmp_too_early(self):
        """Test when jmp is the first instruction (no room for cmp)."""
        from cfg import _resolve_switch

        # jmp dword ptr [ecx*4 + 0x401100]
        code = b"\xff\x24\x8d\x00\x11\x40\x00"
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        cs.detail = True
        insns = list(cs.disasm(code, 0x401000))  # noqa
        b = MagicMock()
        b.is_64 = False
        targets = _resolve_switch(b, insns[0], insns)
        assert targets is None

    def test_resolve_switch_no_cmp(self):
        """Test when there is no cmp instruction before jmp."""
        from cfg import _resolve_switch

        # mov ecx, 3
        # nop
        # jmp dword ptr [ecx*4 + 0x401100]
        mov_bytes = b"\xb9\x03\x00\x00\x00"
        nop_bytes = b"\x90"
        jmp_bytes = b"\xff\x24\x8d\x00\x11\x40\x00"
        code = mov_bytes + nop_bytes + jmp_bytes

        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        cs.detail = True
        insns = list(cs.disasm(code, 0x401000))  # noqa
        jmp_insn = insns[-1]

        b = MagicMock()
        b.is_64 = False
        targets = _resolve_switch(b, jmp_insn, insns)
        assert targets is None

    def test_resolve_switch_too_many_cases(self):
        """Test when case_count > 1024."""
        from cfg import _resolve_switch

        # cmp ecx, 2000
        # ja +0x1B
        # jmp dword ptr [ecx*4 + 0x401100]
        # 2000 is 0x07D0
        cmp_bytes = b"\x81\xf9\xd0\x07\x00\x00"
        ja_bytes = b"\x77\x1b"
        jmp_bytes = b"\xff\x24\x8d\x00\x11\x40\x00"
        code = cmp_bytes + ja_bytes + jmp_bytes

        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        cs.detail = True
        insns = list(cs.disasm(code, 0x401000))  # noqa
        jmp_insn = insns[-1]

        b = MagicMock()
        b.is_64 = False
        targets = _resolve_switch(b, jmp_insn, insns)
        assert targets is None

    def test_resolve_switch_short_table_read(self):
        """Test when b.read_va returns fewer bytes than ptr_size."""
        from cfg import _resolve_switch

        # cmp ecx, 1
        # ja +0x1B
        # jmp dword ptr [ecx*4 + 0x401100]
        cmp_bytes = b"\x83\xf9\x01"
        ja_bytes = b"\x77\x1b"
        jmp_bytes = b"\xff\x24\x8d\x00\x11\x40\x00"
        code = cmp_bytes + ja_bytes + jmp_bytes

        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        cs.detail = True
        insns = list(cs.disasm(code, 0x401000))  # noqa
        jmp_insn = insns[-1]

        b = MagicMock()
        b.is_64 = False
        b.ptr_size = 4
        # Return only 2 bytes instead of 4
        b.read_va.return_value = b"\x00\x00"

        targets = _resolve_switch(b, jmp_insn, insns)
        assert targets is None

    def test_resolve_switch_target_not_executable(self):
        """Test when resolved target is not in executable memory."""
        from cfg import _resolve_switch

        # cmp ecx, 1
        # ja +0x1B
        # jmp dword ptr [ecx*4 + 0x401100]
        cmp_bytes = b"\x83\xf9\x01"
        ja_bytes = b"\x77\x1b"
        jmp_bytes = b"\xff\x24\x8d\x00\x11\x40\x00"
        code = cmp_bytes + ja_bytes + jmp_bytes

        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        cs.detail = True
        insns = list(cs.disasm(code, 0x401000))  # noqa
        jmp_insn = insns[-1]

        b = MagicMock()
        b.is_64 = False
        b.ptr_size = 4
        # Return a valid 4-byte pointer
        b.read_va.return_value = struct.pack("<I", 0x401050)
        # But claim it's not executable
        b.in_exec.return_value = False

        targets = _resolve_switch(b, jmp_insn, insns)
        assert targets is None
