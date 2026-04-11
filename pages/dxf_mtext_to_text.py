import streamlit as st
import ezdxf
from ezdxf import recover
from ezdxf.enums import TextEntityAlignment
import io
import math
import os
import tempfile


# ezdxfが書き出しに対応しているDXFリリース → AC内部コード。
# 実用上必要な4バージョンに絞っている（R2004/R2007/R12 は省略）。
# - R2018: 最新のCAD向け
# - R2013: やや新しめのCAD向け
# - R2010: 互換性バランスが良い既定値
# - R2000: 古いCADや他社CAD互換用
DXF_VERSION_CHOICES = {
    "R2018": "AC1032",
    "R2013": "AC1027",
    "R2010": "AC1024",
    "R2000": "AC1015",
}


# MTEXTのattachment_point (1-9) を TEXT の TextEntityAlignment に対応付ける。
#   1 = Top Left,    2 = Top Center,    3 = Top Right
#   4 = Middle Left, 5 = Middle Center, 6 = Middle Right
#   7 = Bottom Left, 8 = Bottom Center, 9 = Bottom Right
ATTACHMENT_TO_ALIGN = {
    1: TextEntityAlignment.TOP_LEFT,
    2: TextEntityAlignment.TOP_CENTER,
    3: TextEntityAlignment.TOP_RIGHT,
    4: TextEntityAlignment.MIDDLE_LEFT,
    5: TextEntityAlignment.MIDDLE_CENTER,
    6: TextEntityAlignment.MIDDLE_RIGHT,
    7: TextEntityAlignment.BOTTOM_LEFT,
    8: TextEntityAlignment.BOTTOM_CENTER,
    9: TextEntityAlignment.BOTTOM_RIGHT,
}


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
    attachment_point = mtext.dxf.get("attachment_point", 1) or 1

    align = ATTACHMENT_TO_ALIGN.get(attachment_point, TextEntityAlignment.TOP_LEFT)

    # AutoCADのデフォルトMTEXT行送りは文字高さの1.6667倍
    line_height = height * 1.6667 * line_spacing_factor

    angle_rad = math.radians(rotation)
    # 回転を考慮した「下方向」への行送りベクトル
    down_dx = math.sin(angle_rad) * line_height
    down_dy = -math.cos(angle_rad) * line_height

    # 垂直アンカー(Top/Middle/Bottom)に応じて、1行目のオフセット開始位置を決める。
    # 各行 i の配置オフセット = (vertical_start + i) * 下方向ベクトル
    n_lines = len(lines)
    if attachment_point in (1, 2, 3):  # Top: 1行目を挿入点に合わせ、下方向に積む
        vertical_start = 0.0
    elif attachment_point in (4, 5, 6):  # Middle: 全体の中央が挿入点に来るよう配置
        vertical_start = -(n_lines - 1) / 2.0
    else:  # Bottom (7,8,9): 最終行を挿入点に合わせ、上方向に積む
        vertical_start = -(n_lines - 1)

    created = 0
    for i, line in enumerate(lines):
        if line is None:
            line = ""
        step = vertical_start + i
        x = insert.x + down_dx * step
        y = insert.y + down_dy * step
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
        # set_placement が halign/valign/insert/align_point をまとめて設定する
        text.set_placement((x, y, z), align=align)
        created += 1

    # 元のMTEXTを削除
    layout.delete_entity(mtext)
    return created


def convert_dxf_mtext_to_text(uploaded_file, target_version=None):
    """アップロードされたDXFのMTEXTをすべてTEXTに変換し、バイト列を返す。

    target_version に AC コード（例: "AC1024"）を渡すと、保存時にそのDXFバージョンへ
    切り替えて書き出す。None の場合は入力ファイルのバージョンを維持する。
    """
    uploaded_file.seek(0)
    doc, auditor = recover.read(uploaded_file)
    source_version = doc.dxfversion

    mtext_count = 0
    text_count = 0

    # doc.blocks はモデル空間・ペーパー空間・通常ブロックをすべて含むため、
    # これだけを走査すれば全MTEXTを拾える。
    for block in doc.blocks:
        for mtext in list(block.query("MTEXT")):
            text_count += convert_mtext_entity_to_text(mtext)
            mtext_count += 1

    # 出力バージョンを上書きする場合はここで差し替える
    if target_version:
        doc.dxfversion = target_version

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

    return output_bytes, mtext_count, text_count, auditor, source_version, doc.dxfversion


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
        - **引き継ぐ属性**: レイヤー、文字スタイル、色、文字高さ、回転角、**アタッチメントポイント（アラインメント）**
        - **アラインメント**: MTEXTの `attachment_point` (1-9) を TEXT の `halign`/`valign` にマッピングします
            - 水平: Left / Center / Right
            - 垂直: Top / Middle / Bottom
        - **行位置**: 行送りは `char_height × 1.6667 × line_spacing_factor`。垂直アンカーに応じて Top なら下方向、Middle なら中央基準、Bottom なら上方向に行を積みます
        - **出力DXFバージョン**: 入力と同じバージョンのほか、R12〜R2018から選択可能です
        """
    )

uploaded_file = st.file_uploader("DXFファイルを選択してください", type=["dxf"])

if uploaded_file is not None:
    st.success(f"ファイルを読み込みました: **{uploaded_file.name}**")

    # 出力DXFバージョン選択
    version_options = ["入力ファイルと同じ"] + list(DXF_VERSION_CHOICES.keys())
    # 古いCADで読みやすいR2010をデフォルトにする
    default_index = version_options.index("R2010")
    selected_version = st.selectbox(
        "出力DXFバージョン",
        options=version_options,
        index=default_index,
        help="取り込み先ソフトが新しいDXFを読めない場合は古いバージョンを選択してください。"
             "R2010が互換性と機能のバランスが良い選択です。",
    )
    target_version = (
        None if selected_version == "入力ファイルと同じ"
        else DXF_VERSION_CHOICES[selected_version]
    )

    if st.button("MTEXTをTEXTに変換", type="primary"):
        with st.spinner("変換中..."):
            try:
                (
                    output_bytes,
                    mtext_count,
                    text_count,
                    auditor,
                    source_version,
                    output_version,
                ) = convert_dxf_mtext_to_text(uploaded_file, target_version=target_version)
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

                src_label = ezdxf.const.acad_release.get(source_version, source_version)
                out_label = ezdxf.const.acad_release.get(output_version, output_version)
                st.caption(f"入力DXFバージョン: {src_label} → 出力: {out_label}")

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
