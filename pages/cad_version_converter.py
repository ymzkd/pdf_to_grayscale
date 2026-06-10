from __future__ import annotations

import io
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import ezdxf
import streamlit as st
from ezdxf import path, recover

try:
    import rhino3dm
except ImportError:  # pragma: no cover - handled in UI when dependency is missing
    rhino3dm = None


HEADER_SCAN_BYTES = 65536
DXF_MIME_TYPE = "application/dxf"
THREEDM_MIME_TYPE = "application/octet-stream"
TEXT_ENTITY_TYPES = {"TEXT", "ATTRIB", "ATTDEF"}

DXF_VERSION_CHOICES = {
    "R2018": "AC1032",
    "R2013": "AC1027",
    "R2010": "AC1024",
    "R2007": "AC1021",
    "R2004": "AC1018",
    "R2000": "AC1015",
    "R12": "AC1009",
}
DXF_VERSION_LABELS = {value: label for label, value in DXF_VERSION_CHOICES.items()}

THREEDM_VERSION_CHOICES = {
    "Rhino 8": 8,
    "Rhino 7": 7,
    "Rhino 6": 6,
    "Rhino 5": 5,
    "Rhino 4": 4,
    "Rhino 3": 3,
    "Rhino 2": 2,
}
THREEDM_VERSION_LABELS = {
    value: label for label, value in THREEDM_VERSION_CHOICES.items()
}

DXF_ENCODING_CHOICES = {
    "自動判定": None,
    "Shift_JIS (cp932)": "cp932",
    "UTF-8": "utf-8",
}

DXF_CODEPAGE_TO_ENCODING_LABEL = {
    "ANSI_932": "Shift_JIS (cp932)",
    "DOS932": "Shift_JIS (cp932)",
    "UTF-8": "UTF-8",
    "UTF8": "UTF-8",
}

LEGACY_DXF_VERSIONS = {"AC1009", "AC1012", "AC1014", "AC1015", "AC1018"}

DXF_EXPLODE_NONE = "そのまま(分解しない)"
DXF_EXPLODE_EXPLODE = "ポリライン/ブロックを分解(LINE・ARC)"
DXF_EXPLODE_FLATTEN = "すべて線分化(LINE のみ)"
DXF_EXPLODE_CHOICES = [DXF_EXPLODE_NONE, DXF_EXPLODE_EXPLODE, DXF_EXPLODE_FLATTEN]

DEFAULT_FLATTEN_DISTANCE = 0.5


@dataclass
class DxfHeaderInfo:
    version: str | None
    codepage: str | None


@dataclass
class DxfLoadResult:
    doc: ezdxf.document.Drawing
    auditor: object
    decoded_unicode_count: int
    selected_encoding: str | None


@dataclass
class DxfConversionResult:
    outputs: dict[str, bytes]
    source_version: str
    warnings: list[str]
    warning_count: int
    decoded_unicode_count: int
    selected_encoding: str | None
    explode_mode: str
    explode_count: int


@dataclass
class ThreeDmConversionResult:
    outputs: dict[str, bytes]
    source_version: int


def format_size(size_in_bytes: int) -> str:
    if size_in_bytes < 1024:
        return f"{size_in_bytes} B"
    if size_in_bytes < 1024 * 1024:
        return f"{size_in_bytes / 1024:.2f} KB"
    return f"{size_in_bytes / (1024 * 1024):.2f} MB"


def detect_format(filename: str) -> str | None:
    extension = Path(filename).suffix.lower()
    if extension == ".dxf":
        return "DXF"
    if extension == ".3dm":
        return "3DM"
    return None


def write_bytes_to_tempfile(data: bytes, suffix: str) -> str:
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        return tmp.name


def delete_file_if_exists(path: str | None) -> None:
    if path and os.path.exists(path):
        os.unlink(path)


def inspect_dxf_header(file_bytes: bytes) -> DxfHeaderInfo:
    header_text = file_bytes[:HEADER_SCAN_BYTES].decode("latin1", errors="ignore")

    version_match = re.search(
        r"\$ACADVER\s*[\r\n]+\s*1\s*[\r\n]+\s*(AC\d+)",
        header_text,
        flags=re.IGNORECASE,
    )
    codepage_match = re.search(
        r"\$DWGCODEPAGE\s*[\r\n]+\s*3\s*[\r\n]+\s*([A-Z0-9_]+)",
        header_text,
        flags=re.IGNORECASE,
    )

    version = version_match.group(1).upper() if version_match else None
    codepage = codepage_match.group(1).upper() if codepage_match else None
    return DxfHeaderInfo(version=version, codepage=codepage)


def default_encoding_label_from_codepage(codepage: str | None) -> str:
    if not codepage:
        return "UTF-8"
    return DXF_CODEPAGE_TO_ENCODING_LABEL.get(codepage, "自動判定")


def decode_dxf_unicode_document(doc: ezdxf.document.Drawing) -> int:
    decoded_count = 0

    for key in doc.header.varnames():
        value = doc.header.get(key)
        if isinstance(value, str) and ezdxf.has_dxf_unicode(value):
            doc.header[key] = ezdxf.decode_dxf_unicode(value)
            decoded_count += 1

    for entity in doc.entitydb.values():
        for key, value in entity.dxf.all_existing_dxf_attribs().items():
            if isinstance(value, str) and ezdxf.has_dxf_unicode(value):
                entity.dxf.set(key, ezdxf.decode_dxf_unicode(value))
                decoded_count += 1

    return decoded_count


def load_dxf_document(file_bytes: bytes, encoding: str | None) -> DxfLoadResult:
    if encoding is None:
        doc, auditor = recover.read(io.BytesIO(file_bytes))
        return DxfLoadResult(
            doc=doc,
            auditor=auditor,
            decoded_unicode_count=0,
            selected_encoding=None,
        )

    input_path = write_bytes_to_tempfile(file_bytes, ".dxf")
    try:
        doc = ezdxf.readfile(input_path, encoding=encoding, errors="surrogateescape")
        decoded_count = decode_dxf_unicode_document(doc)
        auditor = doc.audit()
    finally:
        delete_file_if_exists(input_path)

    return DxfLoadResult(
        doc=doc,
        auditor=auditor,
        decoded_unicode_count=decoded_count,
        selected_encoding=encoding,
    )


def _explode_inserts(msp) -> None:
    """ブロック参照(INSERT)を構成要素へ展開する。ネストにも対応。"""
    for _ in range(10):
        inserts = list(msp.query("INSERT"))
        if not inserts:
            return
        for insert in inserts:
            try:
                insert.explode()
            except Exception:
                msp.delete_entity(insert)


def explode_dxf_entities(
    doc: ezdxf.document.Drawing, mode: str, flatten_distance: float
) -> int:
    """互換性向上のため、ポリライン等を分解または線分化する。処理件数を返す。"""
    if mode == DXF_EXPLODE_NONE:
        return 0

    msp = doc.modelspace()
    count = 0

    if mode == DXF_EXPLODE_EXPLODE:
        for entity in list(msp.query("LWPOLYLINE POLYLINE INSERT")):
            try:
                entity.explode()
                count += 1
            except Exception:
                continue
        return count

    # DXF_EXPLODE_FLATTEN: すべてを LINE 線分へ近似する
    _explode_inserts(msp)
    targets = list(msp.query("LWPOLYLINE POLYLINE ARC CIRCLE ELLIPSE SPLINE"))
    for entity in targets:
        try:
            entity_path = path.make_path(entity)
            points = list(entity_path.flattening(flatten_distance))
        except Exception:
            continue
        if len(points) < 2:
            continue

        attribs = entity.graphic_properties()
        for start, end in zip(points, points[1:]):
            msp.add_line(start, end, dxfattribs=dict(attribs))
        msp.delete_entity(entity)
        count += 1

    return count


def convert_dxf_versions(
    file_bytes: bytes,
    target_versions: list[str],
    encoding: str | None,
    explode_mode: str = DXF_EXPLODE_NONE,
    flatten_distance: float = DEFAULT_FLATTEN_DISTANCE,
) -> DxfConversionResult:
    load_result = load_dxf_document(file_bytes, encoding=encoding)
    doc = load_result.doc
    source_version = doc.dxfversion

    explode_count = explode_dxf_entities(doc, explode_mode, flatten_distance)

    outputs: dict[str, bytes] = {}
    for target_version in target_versions:
        doc.dxfversion = target_version

        output_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".dxf") as tmp:
                output_path = tmp.name

            if load_result.selected_encoding:
                doc.saveas(output_path, encoding=load_result.selected_encoding)
            else:
                doc.saveas(output_path)

            with open(output_path, "rb") as output_file:
                output_bytes = output_file.read()
        finally:
            delete_file_if_exists(output_path)

        label = DXF_VERSION_LABELS.get(target_version, target_version)
        outputs[label] = output_bytes

    warnings = [str(error) for error in load_result.auditor.errors[:50]]
    return DxfConversionResult(
        outputs=outputs,
        source_version=source_version,
        warnings=warnings,
        warning_count=len(load_result.auditor.errors),
        decoded_unicode_count=load_result.decoded_unicode_count,
        selected_encoding=load_result.selected_encoding,
        explode_mode=explode_mode,
        explode_count=explode_count,
    )


def convert_3dm_versions(
    file_bytes: bytes, target_versions: list[int]
) -> ThreeDmConversionResult:
    if rhino3dm is None:
        raise RuntimeError("rhino3dm is not installed.")

    input_path = write_bytes_to_tempfile(file_bytes, ".3dm")

    try:
        source_version = rhino3dm.File3dm.ReadArchiveVersion(input_path)
        model = rhino3dm.File3dm.Read(input_path)
        if model is None:
            raise ValueError("Failed to read the 3DM file.")

        outputs: dict[str, bytes] = {}
        for target_version in target_versions:
            output_path = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".3dm") as tmp:
                    output_path = tmp.name

                if not model.Write(output_path, target_version):
                    raise ValueError("Failed to write the converted 3DM file.")

                with open(output_path, "rb") as output_file:
                    output_bytes = output_file.read()
            finally:
                delete_file_if_exists(output_path)

            outputs[f"Rhino {target_version}"] = output_bytes
    finally:
        delete_file_if_exists(input_path)

    return ThreeDmConversionResult(
        outputs=outputs,
        source_version=source_version,
    )


def build_single_download_name(stem: str, label: str, suffix: str) -> str:
    safe_label = label.replace(" ", "")
    return f"{stem}_{safe_label}{suffix}"


def build_versions_zip(stem: str, suffix: str, outputs: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for label, data in outputs.items():
            safe_label = label.replace(" ", "")
            archive.writestr(f"{stem}_{safe_label}{suffix}", data)
    return buffer.getvalue()


def get_dxf_text_encoding(
    header_info: DxfHeaderInfo, selected_label: str
) -> str | None:
    return DXF_ENCODING_CHOICES[selected_label]


def render_common_metrics(
    filename: str,
    file_bytes: bytes,
    detected_format: str,
    source_version_label: str,
) -> None:
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("形式", detected_format)
    with col2:
        st.metric("元バージョン", source_version_label)
    with col3:
        st.metric("ファイルサイズ", format_size(len(file_bytes)))
    st.caption(f"アップロードファイル: {filename}")


def render_dxf_encoding_ui(
    header_info: DxfHeaderInfo, source_version: str
) -> str | None:
    encoding_labels = list(DXF_ENCODING_CHOICES.keys())
    default_label = default_encoding_label_from_codepage(header_info.codepage)

    selected_label = st.selectbox(
        "文字コード",
        options=encoding_labels,
        index=encoding_labels.index(default_label),
        help=(
            "古い DXF で文字化けする場合のみ変更してください。"
            "日本語の古い DXF では Shift_JIS (cp932) が正しいことがよくあります。"
        ),
    )

    if header_info.codepage:
        st.caption(f"ヘッダ $DWGCODEPAGE: {header_info.codepage}")
        resolved_label = DXF_CODEPAGE_TO_ENCODING_LABEL.get(header_info.codepage)
        if resolved_label:
            st.caption(f"ヘッダから判定した文字コード: {resolved_label}")
    else:
        st.warning(
            "この DXF には文字コードの明示がありません。"
            "既定値は UTF-8 ですが、実際の文字コードが異なる可能性があります。"
        )

    if source_version in LEGACY_DXF_VERSIONS:
        st.info(
            "古い DXF バージョンです。日本語が文字化けする場合は "
            "`Shift_JIS (cp932)` に切り替えて再変換してください。"
        )

    return get_dxf_text_encoding(header_info, selected_label)


def render_dxf_result(
    result: DxfConversionResult, source_label: str, uploaded_filename: str
) -> None:
    target_labels = list(result.outputs.keys())
    st.success("DXF 変換が完了しました。")
    st.caption(f"{source_label} -> {', '.join(target_labels)}")

    if result.warning_count > 0:
        st.warning(f"DXF 読み込み時に {result.warning_count} 件の警告がありました。")
        with st.expander("DXF 警告を表示"):
            for warning in result.warnings:
                st.text(warning)

    if result.decoded_unicode_count > 0:
        st.caption(
            f"DXF Unicode 文字列を {result.decoded_unicode_count} 件デコードしました。"
        )

    if result.selected_encoding:
        st.caption(f"使用した文字コード: {result.selected_encoding}")

    if result.explode_mode != DXF_EXPLODE_NONE:
        st.caption(
            f"図形分解({result.explode_mode}): {result.explode_count} 個の図形を処理しました。"
        )

    st.info(
        "別の DXF バージョンへ保存すると、一部の要素やメタデータが簡略化される場合があります。"
    )

    stem = Path(uploaded_filename).stem
    if len(result.outputs) == 1:
        label, data = next(iter(result.outputs.items()))
        st.download_button(
            label="変換後の DXF をダウンロード",
            data=data,
            file_name=build_single_download_name(stem, label, ".dxf"),
            mime=DXF_MIME_TYPE,
            type="primary",
        )
    else:
        zip_bytes = build_versions_zip(stem, ".dxf", result.outputs)
        st.download_button(
            label=f"{len(result.outputs)} バージョンをまとめて ZIP でダウンロード",
            data=zip_bytes,
            file_name=f"{stem}_dxf_versions.zip",
            mime="application/zip",
            type="primary",
        )


def render_dxf_converter(uploaded_file, file_bytes: bytes) -> None:
    header_info = inspect_dxf_header(file_bytes)

    try:
        load_result = load_dxf_document(file_bytes, encoding=None)
    except ezdxf.DXFStructureError as error:
        st.error(f"DXF ファイルの読み込みに失敗しました: {error}")
        st.stop()
    except Exception as error:
        st.error(f"DXF ファイルの読み込み中にエラーが発生しました: {error}")
        st.stop()

    source_version = load_result.doc.dxfversion
    source_label = ezdxf.const.acad_release.get(source_version, source_version)
    render_common_metrics(uploaded_file.name, file_bytes, "DXF", source_label)

    if header_info.version and header_info.version != source_version:
        st.warning(
            f"ヘッダ上のバージョン ({header_info.version}) と読み込み結果 ({source_version}) が一致しません。"
        )

    dxf_labels = list(DXF_VERSION_CHOICES.keys())
    default_label = DXF_VERSION_LABELS.get(source_version, "R2010")
    selected_labels = st.multiselect(
        "出力 DXF バージョン(複数選択可)",
        options=dxf_labels,
        default=[default_label],
        help="複数選択すると、すべてのバージョンをまとめて ZIP でダウンロードできます。",
    )

    encoding = render_dxf_encoding_ui(header_info, source_version)

    explode_mode = st.selectbox(
        "互換性のための図形分解",
        options=DXF_EXPLODE_CHOICES,
        index=0,
        help=(
            "受け取り側のソフトで矩形やポリライン、ブロックが正しく開けない場合に使用します。"
            "「線分化」はすべての図形を LINE のみに変換するため互換性が最も高くなります。"
        ),
    )
    flatten_distance = DEFAULT_FLATTEN_DISTANCE
    if explode_mode == DXF_EXPLODE_FLATTEN:
        flatten_distance = st.number_input(
            "線分化の許容誤差(図面単位)",
            min_value=0.001,
            value=DEFAULT_FLATTEN_DISTANCE,
            step=0.1,
            format="%.3f",
            help="小さいほど曲線を細かい線分に分割します(ファイルサイズは増加します)。",
        )

    if st.button("DXF を変換", type="primary"):
        if not selected_labels:
            st.warning("出力するバージョンを 1 つ以上選択してください。")
        else:
            target_versions = [DXF_VERSION_CHOICES[label] for label in selected_labels]
            with st.spinner("DXF を変換中..."):
                try:
                    result = convert_dxf_versions(
                        file_bytes=file_bytes,
                        target_versions=target_versions,
                        encoding=encoding,
                        explode_mode=explode_mode,
                        flatten_distance=flatten_distance,
                    )
                except Exception as error:
                    st.error(f"DXF 変換に失敗しました: {error}")
                else:
                    render_dxf_result(result, source_label, uploaded_file.name)


def render_3dm_result(
    result: ThreeDmConversionResult, uploaded_filename: str
) -> None:
    target_labels = list(result.outputs.keys())
    st.success("3DM 変換が完了しました。")
    st.caption(f"Rhino {result.source_version} -> {', '.join(target_labels)}")

    has_downgrade = any(
        version < result.source_version
        for version in THREEDM_VERSION_CHOICES.values()
        if f"Rhino {version}" in result.outputs
    )
    if has_downgrade:
        st.warning(
            "古い Rhino バージョンへ保存すると、未対応の機能やプラグインデータが失われる場合があります。"
        )

    stem = Path(uploaded_filename).stem
    if len(result.outputs) == 1:
        label, data = next(iter(result.outputs.items()))
        st.download_button(
            label="変換後の 3DM をダウンロード",
            data=data,
            file_name=build_single_download_name(stem, label, ".3dm"),
            mime=THREEDM_MIME_TYPE,
            type="primary",
        )
    else:
        zip_bytes = build_versions_zip(stem, ".3dm", result.outputs)
        st.download_button(
            label=f"{len(result.outputs)} バージョンをまとめて ZIP でダウンロード",
            data=zip_bytes,
            file_name=f"{stem}_3dm_versions.zip",
            mime="application/zip",
            type="primary",
        )


def render_3dm_converter(uploaded_file, file_bytes: bytes) -> None:
    if rhino3dm is None:
        st.error("rhino3dm がインストールされていません。先にデプロイ環境へ追加してください。")
        st.stop()

    input_path = None
    try:
        input_path = write_bytes_to_tempfile(file_bytes, ".3dm")
        source_version = rhino3dm.File3dm.ReadArchiveVersion(input_path)
    except Exception as error:
        st.error(f"3DM ファイルの読み込みに失敗しました: {error}")
        st.stop()
    finally:
        delete_file_if_exists(input_path)

    source_label = f"Rhino {source_version}"
    render_common_metrics(uploaded_file.name, file_bytes, "3DM", source_label)

    version_labels = list(THREEDM_VERSION_CHOICES.keys())
    default_label = THREEDM_VERSION_LABELS.get(source_version, "Rhino 8")
    selected_labels = st.multiselect(
        "出力 3DM バージョン(複数選択可)",
        options=version_labels,
        default=[default_label],
        help="複数選択すると、すべてのバージョンをまとめて ZIP でダウンロードできます。",
    )

    if st.button("3DM を変換", type="primary"):
        if not selected_labels:
            st.warning("出力するバージョンを 1 つ以上選択してください。")
        else:
            target_versions = [
                THREEDM_VERSION_CHOICES[label] for label in selected_labels
            ]
            with st.spinner("3DM を変換中..."):
                try:
                    result = convert_3dm_versions(
                        file_bytes=file_bytes,
                        target_versions=target_versions,
                    )
                except Exception as error:
                    st.error(f"3DM 変換に失敗しました: {error}")
                else:
                    render_3dm_result(result, uploaded_file.name)


def render_page() -> None:
    st.title("CAD バージョン変換")
    st.write(
        "DXF と Rhino 3DM ファイルをアップロードし、別バージョンへ変換して"
        "そのままダウンロードできます。"
    )

    with st.expander("対応形式と注意事項"):
        st.markdown(
            """
            - `DXF` は `ezdxf` で変換します。
            - `3DM` は `rhino3dm` で変換します。
            - 古いバージョンへの変換にも対応しますが、完全な互換性は保証できません。
            - 元ファイルにアプリ固有データが含まれる場合、変換後も警告が出ることがあります。
            """
        )

    uploaded_file = st.file_uploader(
        "DXF または 3DM ファイルをアップロード",
        type=["dxf", "3dm"],
    )

    if uploaded_file is None:
        st.info("DXF または 3DM ファイルをアップロードすると、バージョン変換を開始できます。")
        return

    detected_format = detect_format(uploaded_file.name)
    if detected_format is None:
        st.error("対応しているのは DXF と 3DM のみです。")
        return

    file_bytes = uploaded_file.getvalue()
    if detected_format == "DXF":
        render_dxf_converter(uploaded_file, file_bytes)
    else:
        render_3dm_converter(uploaded_file, file_bytes)


render_page()
