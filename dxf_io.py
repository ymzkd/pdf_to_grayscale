"""Format-aware DXF loading shared by the Streamlit CAD pages."""
import os
import tempfile

import ezdxf
from ezdxf import recover

BINARY_DXF_SIGNATURE = b"AutoCAD Binary DXF\r\n\x1a\x00"


def load_dxf_bytes(data: bytes, encoding: str | None = None):
    """Read ASCII or Binary DXF and return a drawing and its auditor.

    Recovery is ASCII-only. Keep explicit encoding errors visible rather than
    retrying with automatic decoding, which could corrupt Japanese text.
    """
    input_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
            input_path = tmp.name
            tmp.write(data)
        try:
            doc = ezdxf.readfile(
                input_path, encoding=encoding, errors="surrogateescape"
            )
        except ezdxf.DXFStructureError:
            if data.startswith(BINARY_DXF_SIGNATURE) or encoding is not None:
                raise
            return recover.readfile(input_path)
        return doc, doc.audit()
    finally:
        if input_path and os.path.exists(input_path):
            os.unlink(input_path)
