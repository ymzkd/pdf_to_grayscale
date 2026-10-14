"""Regression tests use generated drawings; no private CAD files are committed."""
import ast
import io
from pathlib import Path
import sys
import types

import ezdxf
import pytest
from ezdxf import recover

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_page_functions(filename):
    """Load page logic without executing its top-level Streamlit UI."""
    tree = ast.parse((ROOT / "pages" / filename).read_text())
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            if any(alias.name == "streamlit" for alias in node.names):
                continue
            nodes.append(node)
        elif isinstance(node, (ast.ImportFrom, ast.FunctionDef, ast.ClassDef)):
            nodes.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            # All module-level assignments before the first UI call are constants.
            nodes.append(node)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            break
    module = types.ModuleType("test_page_" + Path(filename).stem)
    sys.modules[module.__name__] = module
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), module.__dict__)
    return module


def drawing_bytes(fmt="bin", version="R2018"):
    doc = ezdxf.new(version)
    msp = doc.modelspace()
    msp.add_mtext("梅が丘\\P寸法")
    dimension = msp.add_linear_dim(base=(0, 5), p1=(0, 0), p2=(300, 0))
    dimension.render()
    dimension.dimension.dxf.text = "300<><>"
    stream = io.BytesIO() if fmt == "bin" else io.StringIO()
    doc.write(stream, fmt=fmt)
    data = stream.getvalue()
    return data if isinstance(data, bytes) else data.encode(doc.output_encoding)


@pytest.mark.parametrize("fmt", ["bin", "asc"])
def test_vw_page_accepts_dxf(fmt):
    page = load_page_functions("VW出力DXF校正.py")
    output, stats, audit, source, target = page.clean_dxf_dimension_text(
        io.BytesIO(drawing_bytes(fmt))
    )
    doc, output_audit = recover.read(io.BytesIO(output))
    assert source == target == "AC1032"
    assert stats["text_fixed"] == 1
    assert doc.modelspace().query("DIMENSION")[0].dxf.text == "300"
    assert doc.modelspace().query("MTEXT")[0].plain_text() == "梅が丘\n寸法"
    assert not audit.has_errors
    assert not output_audit.has_errors


@pytest.mark.parametrize("fmt", ["bin", "asc"])
def test_mtext_page_accepts_dxf(fmt):
    page = load_page_functions("dxf_mtext_to_text.py")
    output, mtext_count, text_count, audit, source, target = (
        page.convert_dxf_mtext_to_text(io.BytesIO(drawing_bytes(fmt)))
    )
    doc, output_audit = recover.read(io.BytesIO(output))
    assert source == target == "AC1032"
    assert mtext_count > 0 and text_count >= mtext_count
    assert not doc.modelspace().query("MTEXT")
    assert "梅が丘" in [e.dxf.text for e in doc.modelspace().query("TEXT")]
    assert not audit.has_errors
    assert not output_audit.has_errors


@pytest.mark.parametrize("fmt", ["bin", "asc"])
def test_version_page_accepts_dxf(fmt):
    page = load_page_functions("cad_version_converter.py")
    result = page.convert_dxf_version(drawing_bytes(fmt), "AC1024", encoding=None)
    doc, audit = recover.read(io.BytesIO(result.output_bytes))
    assert result.source_version == "AC1032"
    assert doc.dxfversion == result.target_version == "AC1024"
    assert doc.modelspace().query("MTEXT")[0].plain_text() == "梅が丘\n寸法"
    assert result.warning_count == 0
    assert not audit.has_errors


@pytest.mark.parametrize("fmt", ["bin", "asc"])
@pytest.mark.parametrize("encoding", [None, "cp932"])
def test_legacy_japanese_dxf(fmt, encoding):
    from dxf_io import load_dxf_bytes

    doc = ezdxf.new("R2000")
    doc.encoding = "cp932"
    doc.modelspace().add_text("梅が丘 寸法")
    stream = io.BytesIO() if fmt == "bin" else io.StringIO()
    doc.write(stream, fmt=fmt)
    data = stream.getvalue()
    if isinstance(data, str):
        data = data.encode("cp932")
    loaded, audit = load_dxf_bytes(data, encoding=encoding)
    assert loaded.modelspace().query("TEXT")[0].dxf.text == "梅が丘 寸法"
    assert not audit.has_errors


def test_ascii_recovery_fallback():
    from dxf_io import load_dxf_bytes

    data = drawing_bytes("asc")
    assert b" 10\n0.0\n" in data
    damaged = data.replace(b" 10\n0.0\n", b"10xyz\n0.0\n", 1)
    loaded, audit = load_dxf_bytes(damaged)
    assert loaded.modelspace().query("MTEXT")[0].plain_text() == "梅が丘\n寸法"
    assert not audit.has_errors


@pytest.mark.parametrize("fmt,encoding", [("bin", None), ("asc", "cp932")])
def test_no_ascii_recovery_for_binary_or_explicit_encoding(monkeypatch, tmp_path, fmt, encoding):
    import dxf_io

    original_tempfile = dxf_io.tempfile.NamedTemporaryFile
    paths = []

    def tracked_tempfile(**kwargs):
        tmp = original_tempfile(dir=tmp_path, **kwargs)
        paths.append(Path(tmp.name))
        return tmp

    def fail_read(*args, **kwargs):
        raise ezdxf.DXFStructureError("original reader failure")

    def forbidden_recovery(*args, **kwargs):
        pytest.fail("ASCII recovery must not be called")

    monkeypatch.setattr(dxf_io.tempfile, "NamedTemporaryFile", tracked_tempfile)
    monkeypatch.setattr(dxf_io.ezdxf, "readfile", fail_read)
    monkeypatch.setattr(dxf_io.recover, "readfile", forbidden_recovery)
    with pytest.raises(ezdxf.DXFStructureError, match="original reader failure"):
        dxf_io.load_dxf_bytes(drawing_bytes(fmt), encoding=encoding)
    assert paths and all(not path.exists() for path in paths)


def test_temporary_input_removed_after_success(monkeypatch, tmp_path):
    import dxf_io

    original_tempfile = dxf_io.tempfile.NamedTemporaryFile
    paths = []

    def tracked_tempfile(**kwargs):
        tmp = original_tempfile(dir=tmp_path, **kwargs)
        paths.append(Path(tmp.name))
        return tmp

    monkeypatch.setattr(dxf_io.tempfile, "NamedTemporaryFile", tracked_tempfile)
    dxf_io.load_dxf_bytes(drawing_bytes())
    assert paths and all(not path.exists() for path in paths)
