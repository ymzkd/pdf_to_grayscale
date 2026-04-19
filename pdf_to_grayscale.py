import io
import zlib

import fitz
import pikepdf
import streamlit as st
from PIL import Image
from pikepdf import Name, Operator, Pdf, PdfImage, parse_content_stream, unparse_content_stream

LUMA_COEFFS = (0.2126, 0.7152, 0.0722)  # BT.709


def _format_size(bytes_size: int) -> str:
    if bytes_size < 1024:
        return f"{bytes_size} B"
    if bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.2f} KB"
    return f"{bytes_size / (1024 * 1024):.2f} MB"


def convert_to_grayscale_raster(pdf_bytes: bytes, dpi: int = 300) -> bytes:
    pdf_document = fitz.open("pdf", pdf_bytes)
    pdf_writer = fitz.open()

    for page_num in range(pdf_document.page_count):
        page = pdf_document.load_page(page_num)
        pix = page.get_pixmap(dpi=dpi)
        img = Image.open(io.BytesIO(pix.tobytes()))
        grayscale_img = img.convert("L")

        img_buffer = io.BytesIO()
        grayscale_img.save(img_buffer, format="PNG")

        new_page = pdf_writer.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(page.rect, stream=img_buffer.getvalue())

    output = io.BytesIO()
    pdf_writer.save(
        output,
        garbage=4,
        clean=True,
        deflate=True,
        deflate_images=True,
        deflate_fonts=True,
    )
    pdf_writer.close()
    pdf_document.close()
    return output.getvalue()


def _luma_rgb(r, g, b):
    wr, wg, wb = LUMA_COEFFS
    return max(0.0, min(1.0, wr * r + wg * g + wb * b))


def _luma_cmyk(c, m, y, k):
    r = (1 - c) * (1 - k)
    g = (1 - m) * (1 - k)
    b = (1 - y) * (1 - k)
    return _luma_rgb(r, g, b)


def _color_to_gray(values):
    nums = []
    for v in values:
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            return None
    if len(nums) == 1:
        return max(0.0, min(1.0, nums[0]))
    if len(nums) == 3:
        return _luma_rgb(*nums)
    if len(nums) == 4:
        return _luma_cmyk(*nums)
    return None


def _rewrite_instructions(instructions):
    out = []
    for operands, operator in instructions:
        op = str(operator)
        if op == "rg":
            y = _luma_rgb(float(operands[0]), float(operands[1]), float(operands[2]))
            out.append(([y], Operator("g")))
        elif op == "RG":
            y = _luma_rgb(float(operands[0]), float(operands[1]), float(operands[2]))
            out.append(([y], Operator("G")))
        elif op == "k":
            y = _luma_cmyk(
                float(operands[0]), float(operands[1]), float(operands[2]), float(operands[3])
            )
            out.append(([y], Operator("g")))
        elif op == "K":
            y = _luma_cmyk(
                float(operands[0]), float(operands[1]), float(operands[2]), float(operands[3])
            )
            out.append(([y], Operator("G")))
        elif op in ("sc", "scn"):
            y = _color_to_gray(operands)
            out.append(([y], Operator("g")) if y is not None else (operands, operator))
        elif op in ("SC", "SCN"):
            y = _color_to_gray(operands)
            out.append(([y], Operator("G")) if y is not None else (operands, operator))
        else:
            out.append((operands, operator))
    return out


def _rewrite_content_stream(owner):
    try:
        instructions = parse_content_stream(owner)
    except pikepdf.PdfError:
        return
    data = unparse_content_stream(_rewrite_instructions(instructions))
    owner.write(data)


def _rewrite_page_content(page, pdf):
    try:
        instructions = parse_content_stream(page)
    except pikepdf.PdfError:
        return
    data = unparse_content_stream(_rewrite_instructions(instructions))
    page.Contents = pdf.make_stream(data)


def _convert_image_stream(image_obj):
    try:
        pdf_image = PdfImage(image_obj)
        pil = pdf_image.as_pil_image()
    except Exception:
        return
    gray = pil.convert("L")
    image_obj.write(zlib.compress(gray.tobytes()), filter=Name.FlateDecode)
    image_obj.ColorSpace = Name.DeviceGray
    image_obj.BitsPerComponent = 8
    image_obj.Width = gray.width
    image_obj.Height = gray.height
    for key in (Name.Decode, Name.DecodeParms):
        if key in image_obj:
            del image_obj[key]


def _walk_resources(resources, visited):
    if resources is None or Name.XObject not in resources:
        return
    for _name, xobj in resources.XObject.items():
        objgen = getattr(xobj, "objgen", None)
        if objgen is not None:
            if objgen in visited:
                continue
            visited.add(objgen)
        subtype = xobj.get(Name.Subtype)
        if subtype == Name.Form:
            _rewrite_content_stream(xobj)
            sub_resources = xobj.get(Name.Resources)
            if sub_resources is not None:
                _walk_resources(sub_resources, visited)
        elif subtype == Name.Image:
            _convert_image_stream(xobj)


def _rewrite_annotation(annot, visited):
    for key in (Name.C, Name.IC):
        if key in annot:
            y = _color_to_gray(list(annot[key]))
            if y is not None:
                annot[key] = pikepdf.Array([y])
    ap = annot.get(Name.AP)
    if ap is None:
        return
    normal = ap.get(Name.N)
    if normal is None:
        return
    streams = [normal] if Name.Subtype in normal else list(normal.values())
    for stream in streams:
        if stream.get(Name.Subtype) != Name.Form:
            continue
        objgen = getattr(stream, "objgen", None)
        if objgen is not None:
            if objgen in visited:
                continue
            visited.add(objgen)
        _rewrite_content_stream(stream)
        sub_resources = stream.get(Name.Resources)
        if sub_resources is not None:
            _walk_resources(sub_resources, visited)


def convert_to_grayscale_objects(pdf_bytes: bytes) -> bytes:
    pdf = Pdf.open(io.BytesIO(pdf_bytes))
    visited: set = set()
    for page in pdf.pages:
        _rewrite_page_content(page, pdf)
        resources = page.get(Name.Resources)
        if resources is not None:
            _walk_resources(resources, visited)
        annots = page.get(Name.Annots)
        if annots is not None:
            for annot in annots:
                _rewrite_annotation(annot, visited)
    pdf.remove_unreferenced_resources()
    buf = io.BytesIO()
    pdf.save(
        buf,
        object_stream_mode=pikepdf.ObjectStreamMode.generate,
        recompress_flate=True,
    )
    pdf.close()
    return buf.getvalue()


st.title("PDF to Grayscale Converter")

uploaded_file = st.file_uploader("Choose a PDF file", type="pdf")

if uploaded_file is not None:
    st.write("File uploaded successfully.")

    mode = st.radio(
        "変換方式",
        ("オブジェクト単位", "ラスタライズ"),
        captions=(
            "content stream の色演算子と画像の色空間を書き換え。テキスト・ベクターはそのまま保持。",
            "各ページを画像として描画してからグレースケール化。互換性が高く結果が安定。",
        ),
    )

    dpi = 300
    if mode == "ラスタライズ":
        dpi = st.slider("Select DPI (Resolution)", 100, 600, 300)

    if st.button("Convert to Grayscale", type="primary"):
        pdf_bytes = uploaded_file.getvalue()
        with st.spinner("Converting..."):
            try:
                if mode == "ラスタライズ":
                    output_bytes = convert_to_grayscale_raster(pdf_bytes, dpi=dpi)
                    suffix = "_GR.pdf"
                else:
                    output_bytes = convert_to_grayscale_objects(pdf_bytes)
                    suffix = "_GR_obj.pdf"
            except pikepdf.PasswordError:
                st.error("パスワード保護された PDF には対応していません。")
            except Exception as exc:
                st.error(f"変換に失敗しました: {exc}")
            else:
                st.success("Conversion successful!")
                input_size = len(pdf_bytes)
                output_size = len(output_bytes)
                ratio = output_size / input_size if input_size else 0
                sign = "-" if output_size < input_size else "+"
                col1, col2 = st.columns(2)
                col1.metric("変換前", _format_size(input_size))
                col2.metric(
                    "変換後",
                    _format_size(output_size),
                    delta=f"{sign}{_format_size(abs(output_size - input_size))} ({ratio:.0%})",
                    delta_color="inverse",
                )
                output_filename = uploaded_file.name.rsplit(".", 1)[0] + suffix
                st.download_button(
                    label="Download Grayscale PDF",
                    data=output_bytes,
                    file_name=output_filename,
                    mime="application/pdf",
                    type="primary",
                )
