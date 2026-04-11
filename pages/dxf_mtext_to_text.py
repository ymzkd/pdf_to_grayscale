import streamlit as st
import ezdxf
from ezdxf import recover
import io
import math
import os
import tempfile


def convert_mtext_entity_to_text(mtext):
    """1つのMTEXTエンティティを、改行ごとに複数のTEXTエンティティへ変換する。

    戻り値: 生成されたTEXTエンティティの数。
    """
    layout = mtext.get_layout()
    if layout is None:
        return 0

    # フォーマット記号を除去したプレーンテキストを行単位で取得
    try:
        lines = mtext.plain_text(split=True)
    except Exception:
        lines = mtext.plain_text().split("\n")

    if not lines:
        lines = [""]

    insert = mtext.dxf.insert
    height = mtext.dxf.get("char_height", 2.5) or 2.5
    rotation = mtext.dxf.get("rotation", 0.0) or 0.0
    layer = mtext.dxf.get("layer", "0")
    style = mtext.dxf.get("style", "Standard")
    color = mtext.dxf.get("color", 256)
    line_spacing_factor = mtext.dxf.get("line_spacing_factor", 1.0) or 1.0

    # AutoCADのデフォルトMTEXT行送りは文字高さの1.6667倍
    line_height = height * 1.6667 * line_spacing_factor

    angle_rad = math.radians(rotation)
    # 回転を考慮した「下方向」への行送りベクトル
    dx = math.sin(angle_rad) * line_height
    dy = -math.cos(angle_rad) * line_height

    created = 0
    for i, line in enumerate(lines):
        if line is None:
            line = ""
        x = insert.x + dx * i
        y = insert.y + dy * i
        z = getattr(insert, "z", 0.0)

        text = layout.add_text(
            line,
            height=height,
            rotation=rotation,
            dxfattribs={
                "layer": layer,
                "style": style,
                "color": color,
            },
        )
        text.dxf.insert = (x, y, z)
        created += 1

    # 元のMTEXTを削除
    layout.delete_entity(mtext)
    return created


def convert_dxf_mtext_to_text(uploaded_file):
    """アップロードされたDXFのMTEXTをすべてTEXTに変換し、バイト列を返す。"""
    uploaded_file.seek(0)
    doc, auditor = recover.read(uploaded_file)

    mtext_count = 0
    text_count = 0

    # doc.blocks はモデル空間・ペーパー空間・通常ブロックをすべて含むため、
    # これだけを走査すれば全MTEXTを拾える。
    for block in doc.blocks:
        for mtext in list(block.query("MTEXT")):
            text_count += convert_mtext_entity_to_text(mtext)
            mtext_count += 1

    # ezdxfのwrite/saveasはテキストモードで書き出すため、
    # 一時ファイル経由でバイト列を取得する。
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
            tmp_path = tmp.name
        doc.saveas(tmp_path)
        with open(tmp_path, "rb") as f:
            output_bytes = f.read()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    return output_bytes, mtext_count, text_count, auditor


# ========== UI ==========

st.title("DXF MTEXT → TEXT コンバーター")

st.write(
    "DXFファイル内のマルチテキスト（MTEXT）を通常のテキスト（TEXT）に変換します。"
    "ezdxfライブラリを使用して、モデル空間・ペーパー空間・ブロック内のMTEXTをすべて処理します。"
)

with st.expander("変換仕様について"):
    st.markdown(
        """
        - **対象**: モデル空間・全ペーパー空間・全ブロック内のMTEXTエンティティ
        - **テキスト内容**: MTEXTのフォーマット記号（`\\P`, `\\L`, `{\\f...;...}` など）を除去したプレーンテキストに変換します
        - **改行**: MTEXT内の改行（`\\P`）ごとに別々のTEXTエンティティを生成します
        - **位置**: MTEXTの挿入点を1行目の配置点とし、`char_height × 1.6667 × line_spacing_factor` で下方向にオフセットします
        - **引き継ぐ属性**: レイヤー、文字スタイル、色、文字高さ、回転角
        - **注意**: アタッチメントポイント（左上/中央など）は完全には再現されません。配置位置はMTEXTの挿入点を基準とします
        """
    )

uploaded_file = st.file_uploader("DXFファイルを選択してください", type=["dxf"])

if uploaded_file is not None:
    st.success(f"ファイルを読み込みました: **{uploaded_file.name}**")

    if st.button("MTEXTをTEXTに変換", type="primary"):
        with st.spinner("変換中..."):
            try:
                output_bytes, mtext_count, text_count, auditor = (
                    convert_dxf_mtext_to_text(uploaded_file)
                )
            except ezdxf.DXFStructureError as e:
                st.error(f"DXFファイルの読み込みに失敗しました: {e}")
            except Exception as e:
                st.error(f"変換処理中にエラーが発生しました: {e}")
            else:
                if mtext_count == 0:
                    st.info("MTEXTエンティティは見つかりませんでした。")
                else:
                    col1, col2 = st.columns(2)
                    with col1:
                        st.metric("変換したMTEXT", f"{mtext_count} 個")
                    with col2:
                        st.metric("生成したTEXT", f"{text_count} 個")
                    st.success("変換が完了しました。")

                if auditor.has_errors:
                    with st.expander(
                        f"読み込み時の警告 ({len(auditor.errors)} 件)"
                    ):
                        for err in auditor.errors[:50]:
                            st.text(str(err))

                base_name = uploaded_file.name.rsplit(".", 1)[0]
                output_filename = f"{base_name}_text.dxf"

                st.download_button(
                    label="変換後のDXFをダウンロード",
                    data=output_bytes,
                    file_name=output_filename,
                    mime="application/dxf",
                    type="primary",
                )
else:
    st.info("DXFファイルをアップロードすると変換できます。")
