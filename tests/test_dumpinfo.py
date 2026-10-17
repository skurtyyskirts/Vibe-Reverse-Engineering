import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retools.dumpinfo import cmd_diagnose

@pytest.fixture
def mock_dump():
    dump = MagicMock()
    dump.modules = MagicMock()
    dump.modules.modules = []
    dump.exception = MagicMock()
    dump.threads = MagicMock()
    dump.threads.threads = []
    return dump

def test_diagnose_no_exception(mock_dump, capsys):
    mock_dump.exception = None

    args = MagicMock()
    args.binary = None

    cmd_diagnose(mock_dump, args)

    out, err = capsys.readouterr()
    assert "=== Exception ===" in out
    assert "No exception record in dump" in out
    assert "=== Threads ===" in out

def test_diagnose_with_exception(mock_dump, capsys):
    # Mock modules
    mod = MagicMock()
    mod.baseaddress = 0x10000000
    mod.size = 0x1000
    mod.name = "test.dll"
    mock_dump.modules.modules = [mod]

    # Mock exception info
    stream = MagicMock()
    stream.ThreadId = 1234
    rec = MagicMock()
    rec.ExceptionCode_raw = 0xC0000005
    rec.ExceptionAddress = 0x10000100
    rec.NumberParameters = 0
    rec.ExceptionInformation = []
    stream.ExceptionRecord = rec
    mock_dump.exception.exception_records = [stream]

    # Mock thread ContextObject (64-bit)
    t = MagicMock()
    t.ThreadId = 1234
    ctx = MagicMock()
    ctx.Rip = 0x10000100
    ctx.Rsp = 0x20000000
    t.ContextObject = ctx
    mock_dump.threads.threads = [t]

    # Mock thread stack scan chunk reader
    reader = MagicMock()
    # Provide a chunk with some pointers
    # one valid pointer that hits test.dll
    import struct
    chunk_data = struct.pack("<Q", 0x10000110) + b"\x00" * 4088
    reader.read.return_value = chunk_data
    mock_dump.get_reader.return_value = reader

    args = MagicMock()
    args.binary = None

    cmd_diagnose(mock_dump, args)

    out, err = capsys.readouterr()
    assert "Code:    0xC0000005" in out
    assert "Address: 0x0000000010000100" in out
    assert "Thread:  1234" in out
    assert "test.dll" in out
    assert "<< EXCEPTION" in out

def test_diagnose_msvc_exception(mock_dump, capsys):
    # Mock MSVC C++ exception (code = 0xE06D7363)
    stream = MagicMock()
    stream.ThreadId = 1234
    rec = MagicMock()
    rec.ExceptionCode_raw = 0xE06D7363
    rec.ExceptionAddress = 0x10000100
    rec.NumberParameters = 2
    rec.ExceptionInformation = [0x0, 0x1000] # dummy param, obj_ptr
    stream.ExceptionRecord = rec
    mock_dump.exception.exception_records = [stream]

    # Mock _read_dump_memory for std::string SSO pattern
    # struct format is 3 QWORDs: string pointer (0), length (16), capacity (24)
    import struct
    # Build inline string (len <= 15)
    test_str = b"Hello, World!"
    # SSO std::string memory structure
    obj_data = test_str + b"\x00" * (16 - len(test_str))
    obj_data += struct.pack("<Q", len(test_str)) # length
    obj_data += struct.pack("<Q", 15) # capacity
    obj_data += b"\x00" * 24 # padding to >= 32

    reader = MagicMock()
    def mock_read(addr, size):
        if addr == 0x1000:
            return obj_data
        return b""
    reader.read.side_effect = mock_read
    mock_dump.get_reader.return_value = reader

    args = MagicMock()
    args.binary = None

    cmd_diagnose(mock_dump, args)

    out, err = capsys.readouterr()
    assert "Code:    0xE06D7363" in out
    assert 'Message (SSO): "Hello, World!"' in out

def test_diagnose_msvc_exception_non_sso(mock_dump, capsys):
    stream = MagicMock()
    stream.ThreadId = 1234
    rec = MagicMock()
    rec.ExceptionCode_raw = 0xE06D7363
    rec.ExceptionAddress = 0x10000100
    rec.NumberParameters = 2
    rec.ExceptionInformation = [0x0, 0x2000] # obj_ptr at 0x2000
    stream.ExceptionRecord = rec
    mock_dump.exception.exception_records = [stream]

    import struct
    test_str = b"This is a longer string not SSO"
    # non-SSO std::string memory structure
    obj_data = struct.pack("<Q", 0x3000) # heap ptr
    obj_data += b"\x00" * 8 # padding
    obj_data += struct.pack("<Q", len(test_str)) # length
    obj_data += struct.pack("<Q", len(test_str) + 10) # capacity
    obj_data += b"\x00" * 24 # padding to >= 32

    reader = MagicMock()
    def mock_read(addr, size):
        if addr == 0x2000:
            return obj_data
        if addr == 0x3000:
            return test_str
        return b""
    reader.read.side_effect = mock_read
    mock_dump.get_reader.return_value = reader

    args = MagicMock()
    args.binary = None

    cmd_diagnose(mock_dump, args)

    out, err = capsys.readouterr()
    assert "Code:    0xE06D7363" in out
    assert 'Message: "This is a longer string not SSO"' in out


def test_diagnose_msvc_exception_with_throw_match(mock_dump, capsys):
    # Setup mock exception matching the msvc exception handling
    stream = MagicMock()
    stream.ThreadId = 1234
    rec = MagicMock()
    rec.ExceptionCode_raw = 0xE06D7363
    rec.ExceptionAddress = 0x10000100
    rec.NumberParameters = 2
    rec.ExceptionInformation = [0x0, 0x1000] # dummy param, obj_ptr
    stream.ExceptionRecord = rec
    mock_dump.exception.exception_records = [stream]

    # Empty get_reader
    reader = MagicMock()
    reader.read.return_value = b""
    mock_dump.get_reader.return_value = reader

    # Use patch to replace _resolve_msvc_exception
    with patch("retools.dumpinfo._resolve_msvc_exception", return_value="std::runtime_error"):
        # Setup throw map matching
        args = MagicMock()
        args.binary = "test.dll"

        # Test requires retools.throwmap.build_throw_map
        # but retools.throwmap might not exist or we might not want to test it
        with patch("retools.throwmap.build_throw_map", create=True) as mock_build_throw_map:
            # Return pe, is_64, tmap
            # tmap: dict { site_rva: (insn_size, string_val) }
            mock_build_throw_map.return_value = (None, True, {0x110: (5, "test exception message")})

            # Module base for test.dll is 0x10000000
            mod = MagicMock()
            mod.baseaddress = 0x10000000
            mod.name = "test.dll"
            mod.size = 0x1000
            mock_dump.modules.modules = [mod]
            # Context and stack for matches
            # return address will be base (0x10000000) + site_rva (0x110) + insn_size (5) = 0x10000115
            t = MagicMock()
            t.ThreadId = 1234
            ctx = MagicMock()
            ctx.Rip = 0x10000100
            ctx.Rsp = 0x20000000
            t.ContextObject = ctx
            mock_dump.threads.threads = [t]

            # Reader for chunking memory
            reader2 = MagicMock()
            import struct
            chunk_data = struct.pack("<Q", 0x10000115) + b"\x00" * 4088
            reader2.read.return_value = chunk_data
            mock_dump.get_reader.return_value = reader2

            cmd_diagnose(mock_dump, args)

            out, err = capsys.readouterr()

            assert "Code:    0xE06D7363" in out
            assert "C++ type: std::runtime_error" in out
            assert "=== Throw-Site Match ===" in out
            assert 'Error: "test exception message"' in out
            assert "MATCH: throw at +0x110 (ret +0x115)" in out


def test_diagnose_msvc_exception_with_throw_match_import_error(mock_dump, capsys):
    # Setup mock exception matching the msvc exception handling
    stream = MagicMock()
    stream.ThreadId = 1234
    rec = MagicMock()
    rec.ExceptionCode_raw = 0xE06D7363
    rec.ExceptionAddress = 0x10000100
    rec.NumberParameters = 2
    rec.ExceptionInformation = [0x0, 0x1000] # dummy param, obj_ptr
    stream.ExceptionRecord = rec
    mock_dump.exception.exception_records = [stream]

    # Empty get_reader
    reader = MagicMock()
    reader.read.return_value = b""
    mock_dump.get_reader.return_value = reader

    # Use patch to replace _resolve_msvc_exception
    with patch("retools.dumpinfo._resolve_msvc_exception", return_value="std::runtime_error"):
        args = MagicMock()
        args.binary = "test.dll"

        # We need to use patch.dict on sys.modules to mock ImportError
        import sys
        with patch.dict(sys.modules, {"retools.throwmap": None}):
            # Need to reload or simply simulate the import failure
            # Instead of complex reloading, we can just patch `__import__` or similar,
            # but since cmd_diagnose uses `from retools.throwmap import build_throw_map`,
            # we can use patch to make import fail
            # Just raising ImportError is easier by patching the built-in __import__
            original_import = __import__
            def import_mock(name, *args, **kwargs):
                if name == 'retools.throwmap':
                    raise ImportError("mock error")
                return original_import(name, *args, **kwargs)

            with patch('builtins.__import__', side_effect=import_mock):
                cmd_diagnose(mock_dump, args)

            out, err = capsys.readouterr()

            assert "Code:    0xE06D7363" in out
            assert "=== Throw-Site Match ===" in out
            assert "throwmap module not available" in out


def test_diagnose_msvc_exception_with_throw_match_no_matches(mock_dump, capsys):
    # Setup mock exception matching the msvc exception handling
    stream = MagicMock()
    stream.ThreadId = 1234
    rec = MagicMock()
    rec.ExceptionCode_raw = 0xE06D7363
    rec.ExceptionAddress = 0x10000100
    rec.NumberParameters = 2
    rec.ExceptionInformation = [0x0, 0x1000] # dummy param, obj_ptr
    stream.ExceptionRecord = rec
    mock_dump.exception.exception_records = [stream]

    # Empty get_reader
    reader = MagicMock()
    reader.read.return_value = b""
    mock_dump.get_reader.return_value = reader

    # Use patch to replace _resolve_msvc_exception
    with patch("retools.dumpinfo._resolve_msvc_exception", return_value="std::runtime_error"):
        args = MagicMock()
        args.binary = "test.dll"

        with patch("retools.throwmap.build_throw_map", create=True) as mock_build_throw_map:
            # Return pe, is_64, tmap
            # tmap: dict { site_rva: (insn_size, string_val) }
            mock_build_throw_map.return_value = (None, True, {0x110: (5, "test exception message")})

            # Module base for test.dll is 0x10000000
            mod = MagicMock()
            mod.baseaddress = 0x10000000
            mod.name = "test.dll"
            mod.size = 0x1000
            mock_dump.modules.modules = [mod]

            # Context and stack for matches
            # return address will be base (0x10000000) + site_rva (0x110) + insn_size (5) = 0x10000115
            t = MagicMock()
            t.ThreadId = 1234
            ctx = MagicMock()
            ctx.Rip = 0x10000100
            ctx.Rsp = 0x20000000
            t.ContextObject = ctx
            mock_dump.threads.threads = [t]

            # Reader for chunking memory
            # provide chunk without the matching ret_rva
            reader2 = MagicMock()
            import struct
            chunk_data = struct.pack("<Q", 0x10000116) + b"\x00" * 4088
            reader2.read.return_value = chunk_data
            mock_dump.get_reader.return_value = reader2

            cmd_diagnose(mock_dump, args)

            out, err = capsys.readouterr()

            assert "=== Throw-Site Match ===" in out
            assert "No throw-site matches on exception thread stack." in out


def test_diagnose_msvc_exception_sso_decode_error(mock_dump, capsys):
    stream = MagicMock()
    stream.ThreadId = 1234
    rec = MagicMock()
    rec.ExceptionCode_raw = 0xE06D7363
    rec.ExceptionAddress = 0x10000100
    rec.NumberParameters = 2
    rec.ExceptionInformation = [0x0, 0x1000] # dummy param, obj_ptr
    stream.ExceptionRecord = rec
    mock_dump.exception.exception_records = [stream]

    import struct
    # Build inline string that raises UnicodeDecodeError
    test_str = b"\xff\xfe\xfd" + b"\x00" * 12
    obj_data = test_str
    obj_data += struct.pack("<Q", 15) # length
    obj_data += struct.pack("<Q", 15) # capacity
    obj_data += b"\x00" * 24 # padding to >= 32

    reader = MagicMock()
    def mock_read(addr, size):
        if addr == 0x1000:
            return obj_data
        return b""
    reader.read.side_effect = mock_read
    mock_dump.get_reader.return_value = reader

    args = MagicMock()
    args.binary = None

    with patch("retools.dumpinfo._resolve_msvc_exception", return_value="std::runtime_error"):
        cmd_diagnose(mock_dump, args)

    out, err = capsys.readouterr()
    assert "Code:    0xE06D7363" in out
    assert "Message (SSO)" not in out

def test_diagnose_msvc_exception_non_sso_decode_error(mock_dump, capsys):
    stream = MagicMock()
    stream.ThreadId = 1234
    rec = MagicMock()
    rec.ExceptionCode_raw = 0xE06D7363
    rec.ExceptionAddress = 0x10000100
    rec.NumberParameters = 2
    rec.ExceptionInformation = [0x0, 0x2000] # obj_ptr at 0x2000
    stream.ExceptionRecord = rec
    mock_dump.exception.exception_records = [stream]

    import struct
    test_str = b"\xff\xfe\xfd" + b"\x00" * 20
    # non-SSO std::string memory structure
    obj_data = struct.pack("<Q", 0x3000) # heap ptr
    obj_data += b"\x00" * 8 # padding
    obj_data += struct.pack("<Q", len(test_str)) # length
    obj_data += struct.pack("<Q", len(test_str) + 10) # capacity
    obj_data += b"\x00" * 24 # padding to >= 32

    reader = MagicMock()
    def mock_read(addr, size):
        if addr == 0x2000:
            return obj_data
        if addr == 0x3000:
            return test_str
        return b""
    reader.read.side_effect = mock_read
    mock_dump.get_reader.return_value = reader

    args = MagicMock()
    args.binary = None

    with patch("retools.dumpinfo._resolve_msvc_exception", return_value="std::runtime_error"):
        cmd_diagnose(mock_dump, args)

    out, err = capsys.readouterr()
    assert "Code:    0xE06D7363" in out
    assert "Message:" not in out

def test_diagnose_thread_context_none(mock_dump, capsys):
    # Test thread without context
    mock_dump.exception = None
    t = MagicMock()
    t.ContextObject = None
    mock_dump.threads.threads = [t]

    args = MagicMock()
    args.binary = None
    cmd_diagnose(mock_dump, args)
    out, err = capsys.readouterr()
    assert "=== Threads ===" in out

def test_diagnose_thread_context_no_ip(mock_dump, capsys):
    # Test thread with context but no IP (neither Rip nor Eip)
    mock_dump.exception = None
    t = MagicMock()
    t.ThreadId = 1234
    ctx = MagicMock()
    del ctx.Rip
    del ctx.Eip
    t.ContextObject = ctx
    mock_dump.threads.threads = [t]

    args = MagicMock()
    args.binary = None
    cmd_diagnose(mock_dump, args)
    out, err = capsys.readouterr()
    assert "=== Threads ===" in out
    assert "1234" not in out

def test_diagnose_stackscan_exception(mock_dump, capsys):
    # Mock exception info
    stream = MagicMock()
    stream.ThreadId = 1234
    rec = MagicMock()
    rec.ExceptionCode_raw = 0xC0000005
    rec.ExceptionAddress = 0x10000100
    rec.NumberParameters = 0
    rec.ExceptionInformation = []
    stream.ExceptionRecord = rec
    mock_dump.exception.exception_records = [stream]

    # Mock thread ContextObject (32-bit to cover Esp branch)
    t = MagicMock()
    t.ThreadId = 1234
    ctx = MagicMock()
    del ctx.Rip
    ctx.Eip = 0x10000100
    ctx.Esp = 0x20000000
    t.ContextObject = ctx
    mock_dump.threads.threads = [t]

    # Mock thread stack scan chunk reader throwing exception
    reader = MagicMock()
    reader.read.side_effect = Exception("Read Error")
    mock_dump.get_reader.return_value = reader

    args = MagicMock()
    args.binary = None

    cmd_diagnose(mock_dump, args)

    out, err = capsys.readouterr()
    assert "Code:    0xC0000005" in out

def test_diagnose_throwmap_module_missing_runtime_base(mock_dump, capsys):
    stream = MagicMock()
    stream.ThreadId = 1234
    rec = MagicMock()
    rec.ExceptionCode_raw = 0xE06D7363
    rec.ExceptionAddress = 0x10000100
    rec.NumberParameters = 2
    rec.ExceptionInformation = [0x0, 0x1000] # dummy param, obj_ptr
    stream.ExceptionRecord = rec
    mock_dump.exception.exception_records = [stream]

    # Empty get_reader
    reader = MagicMock()
    reader.read.return_value = b""
    mock_dump.get_reader.return_value = reader

    # Use patch to replace _resolve_msvc_exception
    with patch("retools.dumpinfo._resolve_msvc_exception", return_value="std::runtime_error"):
        args = MagicMock()
        args.binary = "test.dll"

        with patch("retools.throwmap.build_throw_map", create=True) as mock_build_throw_map:
            # Return pe, is_64, tmap
            # tmap: dict { site_rva: (insn_size, string_val) }
            mock_build_throw_map.return_value = (None, True, {0x110: (5, "test exception message")})

            # Module base for OTHER dll
            mod = MagicMock()
            mod.baseaddress = 0x10000000
            mod.name = "other.dll"
            mod.size = 0x1000
            mock_dump.modules.modules = [mod]

            t = MagicMock()
            t.ThreadId = 1234
            ctx = MagicMock()
            ctx.Rip = 0x10000100
            ctx.Rsp = 0x20000000
            t.ContextObject = ctx
            mock_dump.threads.threads = [t]

            cmd_diagnose(mock_dump, args)

            out, err = capsys.readouterr()

            assert "=== Throw-Site Match ===" in out
            assert "Module 'test.dll' not found in dump" in out


def test_diagnose_msvc_exception_non_sso_string_read_failed(mock_dump, capsys):
    stream = MagicMock()
    stream.ThreadId = 1234
    rec = MagicMock()
    rec.ExceptionCode_raw = 0xE06D7363
    rec.ExceptionAddress = 0x10000100
    rec.NumberParameters = 2
    rec.ExceptionInformation = [0x0, 0x2000] # obj_ptr at 0x2000
    stream.ExceptionRecord = rec
    mock_dump.exception.exception_records = [stream]

    import struct
    test_str = b"A" * 20
    # non-SSO std::string memory structure
    obj_data = struct.pack("<Q", 0x3000) # heap ptr
    obj_data += b"\x00" * 8 # padding
    obj_data += struct.pack("<Q", len(test_str)) # length
    obj_data += struct.pack("<Q", len(test_str) + 10) # capacity
    obj_data += b"\x00" * 24 # padding to >= 32

    reader = MagicMock()
    def mock_read(addr, size):
        if addr == 0x2000:
            return obj_data
        # Return none or short read to simulate read failure
        if addr == 0x3000:
            return b"short"
        return b""
    reader.read.side_effect = mock_read
    mock_dump.get_reader.return_value = reader

    args = MagicMock()
    args.binary = None

    with patch("retools.dumpinfo._resolve_msvc_exception", return_value="std::runtime_error"):
        cmd_diagnose(mock_dump, args)

    out, err = capsys.readouterr()
    assert "Code:    0xE06D7363" in out
    assert "String length=20, heap ptr=0x3000 (not in dump)" in out


def test_diagnose_stackscan_os_module(mock_dump, capsys):
    # Mock exception info
    stream = MagicMock()
    stream.ThreadId = 1234
    rec = MagicMock()
    rec.ExceptionCode_raw = 0xC0000005
    rec.ExceptionAddress = 0x10000100
    rec.NumberParameters = 0
    rec.ExceptionInformation = []
    stream.ExceptionRecord = rec
    mock_dump.exception.exception_records = [stream]

    # Mock thread ContextObject (64-bit)
    t = MagicMock()
    t.ThreadId = 1234
    ctx = MagicMock()
    ctx.Rip = 0x10000100
    ctx.Rsp = 0x20000000
    t.ContextObject = ctx
    mock_dump.threads.threads = [t]

    # Mock modules - one regular, one OS module
    mod1 = MagicMock()
    mod1.baseaddress = 0x10000000
    mod1.size = 0x1000
    mod1.name = "test.dll"

    mod2 = MagicMock()
    mod2.baseaddress = 0x7FF00000
    mod2.size = 0x1000
    mod2.name = "ntdll.dll"

    mock_dump.modules.modules = [mod1, mod2]

    # Mock thread stack scan chunk reader
    reader = MagicMock()
    # Provide a chunk with some pointers
    # one valid pointer that hits test.dll, one that hits ntdll.dll
    # and multiple hits for test.dll to trigger the "> 20" branch
    import struct
    chunk_data = bytearray()

    # 1 pointer for ntdll
    chunk_data.extend(struct.pack("<Q", 0x7FF00110))

    # 25 pointers for test.dll
    for i in range(25):
        chunk_data.extend(struct.pack("<Q", 0x10000110 + i))

    # pad remainder
    chunk_data.extend(b"\x00" * (4096 - len(chunk_data)))

    reader.read.return_value = chunk_data
    mock_dump.get_reader.return_value = reader

    args = MagicMock()
    args.binary = None

    cmd_diagnose(mock_dump, args)

    out, err = capsys.readouterr()
    assert "ntdll.dll" not in out
    assert "test.dll" in out
    assert "... and " in out
