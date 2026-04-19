import io
import zlib

import pikepdf
import streamlit as st
from PIL import Image
from pikepdf import Name, Operator, Pdf, PdfImage, parse_content_stream, unparse_content_stream

LUMA_COEFFS = {
    "BT.709": (0.2126, 0.7152, 0.0722),
    "BT.601": (0.299, 0.587, 0.114),
}


def _luma_rgb(r, g, b, coeffs):
    wr, wg, wb = coeffs
    return max(0.0, min(1.0, wr * r + wg * g + wb * b))


def _luma_cmyk(c, m, y, k, coeffs):
    r = (1 - c) * (1 - k)
    g = (1 - m) * (1 - k)
    b = (1 - y) * (1 - k)
    return _luma_rgb(r, g, b, coeffs)


def _color_to_gray(values, coeffs):
    """Map a 1/3/4-component numeric color to a gray value in [0,1], or None if unsupported."""
    nums = []
    for v in values:
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            return None
    if len(nums) == 1:
        return max(0.0, min(1.0, nums[0]))
    if len(nums) == 3:
        return _luma_rgb(nums[0], nums[1], nums[2], coeffs)
    if len(nums) == 4:
        return _luma_cmyk(nums[0], nums[1], nums[2], nums[3], coeffs)
    return None


def _rewrite_instructions(instructions, coeffs):
    out = []
    for operands, operator in instructions:
        op = str(operator)
        if op == "rg":
            y = _luma_rgb(float(operands[0]), float(operands[1]), float(operands[2]), coeffs)
            out.append(([y], Operator("g")))
        elif op == "RG":
            y = _luma_rgb(float(operands[0]), float(operands[1]), float(operands[2]), coeffs)
            out.append(([y], Operator("G")))
        elif op == "k":
            y = _luma_cmyk(
                float(operands[0]), float(operands[1]), float(operands[2]), float(operands[3]), coeffs
            )
            out.append(([y], Operator("g")))
        elif op == "K":
            y = _luma_cmyk(
                float(operands[0]), float(operands[1]), float(operands[2]), float(operands[3]), coeffs
            )
            out.append(([y], Operator("G")))
        elif op in ("sc", "scn"):
            y = _color_to_gray(operands, coeffs)
            if y is None:
                out.append((operands, operator))
            else:
                out.append(([y], Operator("g")))
        elif op in ("SC", "SCN"):
            y = _color_to_gray(operands, coeffs)
            if y is None:
                out.append((operands, operator))
            else:
                out.append(([y], Operator("G")))
        else:
            out.append((operands, operator))
    return out


def _rewrite_content_stream(owner, coeffs):
    try:
        instructions = parse_content_stream(owner)
    except pikepdf.PdfError:
        return False
    new_instructions = _rewrite_instructions(instructions, coeffs)
    data = unparse_content_stream(new_instructions)
    owner.write(data)
    return True


def _rewrite_page_content(page, pdf, coeffs):
    try:
        instructions = parse_content_stream(page)
    except pikepdf.PdfError:
        return
    new_instructions = _rewrite_instructions(instructions, coeffs)
    data = unparse_content_stream(new_instructions)
    page.Contents = pdf.make_stream(data)


def _convert_image_stream(image_obj):
    try:
        pdf_image = PdfImage(image_obj)
        pil = pdf_image.as_pil_image()
    except Exception:
        return False
    gray = pil.convert("L")
    raw = gray.tobytes()
    image_obj.write(zlib.compress(raw), filter=Name.FlateDecode)
    image_obj.ColorSpace = Name.DeviceGray
    image_obj.BitsPerComponent = 8
    image_obj.Width = gray.width
    image_obj.Height = gray.height
    for key in (Name.Decode, Name.DecodeParms):
        if key in image_obj:
            del image_obj[key]
    return True


def _walk_resources(resources, coeffs, convert_images, visited):
    if resources is None or Name.XObject not in resources:
        return
    xobjects = resources.XObject
    for _name, xobj in xobjects.items():
        objgen = getattr(xobj, "objgen", None)
        if objgen is not None:
            if objgen in visited:
                continue
            visited.add(objgen)
        subtype = xobj.get(Name.Subtype)
        if subtype == Name.Form:
            _rewrite_content_stream(xobj, coeffs)
            sub_resources = xobj.get(Name.Resources)
            if sub_resources is not None:
                _walk_resources(sub_resources, coeffs, convert_images, visited)
        elif subtype == Name.Image and convert_images:
            _convert_image_stream(xobj)


def _rewrite_annotation(annot, coeffs, convert_images, visited):
    for key in (Name.C, Name.IC):
        if key in annot:
            color = list(annot[key])
            y = _color_to_gray(color, coeffs)
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
        if stream.get(Name.Subtype) == Name.Form:
            objgen = getattr(stream, "objgen", None)
            if objgen is not None:
                if objgen in visited:
                    continue
                visited.add(objgen)
            _rewrite_content_stream(stream, coeffs)
            sub_resources = stream.get(Name.Resources)
            if sub_resources is not None:
                _walk_resources(sub_resources, coeffs, convert_images, visited)


def convert_objects_to_grayscale(pdf_bytes: bytes, *, luma: str = "BT.709", convert_images: bool = True) -> bytes:
    coeffs = LUMA_COEFFS[luma]
    pdf = Pdf.open(io.BytesIO(pdf_bytes))
    visited: set = set()
    for page in pdf.pages:
        _rewrite_page_content(page, pdf, coeffs)
        resources = page.get(Name.Resources)
        if resources is not None:
            _walk_resources(resources, coeffs, convert_images, visited)
        annots = page.get(Name.Annots)
        if annots is not None:
            for annot in annots:
                _rewrite_annotation(annot, coeffs, convert_images, visited)
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    buf.seek(0)
    return buf.getvalue()


st.title("PDF to Grayscale (オブジェクト単位)")

st.info(
    "content stream の色演算子を書き換えてグレースケール化します。"
    "テキストはテキストのまま、ベクターはベクターのまま残るため、選択・検索・拡大が可能です。"
    "Pattern / Shading / ICCBased / DeviceN などの特殊カラースペースや inline image、"
    "透明度グループを含む PDF では見た目が変わる場合があります。"
)

uploaded_file = st.file_uploader("PDFファイルを選択", type="pdf")

if uploaded_file is not None:
    st.write(f"アップロード成功: {uploaded_file.name}")

    luma = st.selectbox("輝度式", list(LUMA_COEFFS.keys()), index=0)
    convert_images = st.checkbox("画像もグレースケール化する", value=True)

    if st.button("オブジェクト単位でグレースケール変換", type="primary"):
        with st.spinner("変換中..."):
            try:
                pdf_bytes = uploaded_file.getvalue()
                output_bytes = convert_objects_to_grayscale(
                    pdf_bytes, luma=luma, convert_images=convert_images
                )
            except pikepdf.PasswordError:
                st.error("パスワード保護された PDF には対応していません。")
            except Exception as exc:
                st.error(f"変換に失敗しました: {exc}")
            else:
                st.success("変換完了")
                output_filename = uploaded_file.name.rsplit(".", 1)[0] + "_GR_obj.pdf"
                st.download_button(
                    label="グレースケール PDF をダウンロード",
                    data=output_bytes,
                    file_name=output_filename,
                    mime="application/pdf",
                    type="primary",
                )
