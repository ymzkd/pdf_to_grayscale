import streamlit as st
import ezdxf
from ezdxf import recover
import os
import re
import tempfile


# ezdxfが書き出しに対応しているDXFリリース → AC内部コード。
DXF_VERSION_CHOICES = {
    "R2018": "AC1032",
    "R2013": "AC1027",
    "R2010": "AC1024",
    "R2007": "AC1021",
    "R2004": "AC1018",
    "R2000": "AC1015",
    "R12":   "AC1009",
}

# 末尾に連続する "<>"（実測値プレースホルダ）
TRAILING_PLACEHOLDERS = re.compile(r"^(?P<body>.*?)(?P<marks>(?:<>)+)\s*$", re.DOTALL)
# 数値だけの文字列（例: "300", "1,200", "2.5"）
NUMERIC_TEXT = re.compile(r"^\s*[-+]?[\d.,]+\s*$")
# MTEXT先頭のフォーマット指定（例: "\A1;"）
MTEXT_LEADING_FORMAT = re.compile(r"^((?:\\[A-Za-z][^;\\]*;)*)(.*)$", re.DOTALL)


def strip_placeholders(text):
    """寸法の上書きテキストから、Vectorworks由来の余分な "<>" を取り除く。

    - "300<><>" のように "<>" が2つ以上続く場合は、末尾の "<>" をすべて除く
    - "300<>" のように数値の直後に "<>" が1つの場合も除く
    - "<>" だけ、"W=<>mm" のような正規の前後注記は変更しない

    戻り値: 修正後のテキスト。変更しない場合は None。
    """
    if not text:
        return None
    m = TRAILING_PLACEHOLDERS.match(text)
    if not m:
        return None
    body = m.group("body")
    n_marks = len(m.group("marks")) // 2
    if not body.strip() or "<>" in body:
        return None
    if n_marks >= 2 or NUMERIC_TEXT.match(body):
        return body
    return None


def fix_dimension_block_text(doc, dim, display_text):
    """寸法の見た目用匿名ブロック（*D...）内の文字を display_text に置き換える。

    ブロック内には "\\A1;5050<>" のように数値が重複し "<>" が残った文字が
    入っているため、"<>" を含むTEXT/MTEXTのみ書き換える。
    戻り値: 書き換えた文字エンティティ数。
    """
    block_name = dim.dxf.get("geometry")
    if not block_name:
        return 0
    block = doc.blocks.get(block_name)
    if block is None:
        return 0

    fixed = 0
    for entity in block.query("MTEXT TEXT"):
        if entity.dxftype() == "MTEXT":
            if "<>" not in entity.text:
                continue
            fmt = MTEXT_LEADING_FORMAT.match(entity.text).group(1)
            entity.text = fmt + display_text
        else:
            if "<>" not in entity.dxf.text:
                continue
            entity.dxf.text = display_text
        fixed += 1
    return fixed


def iter_dimensions(doc):
    """モデル空間・ペーパー空間・通常ブロック内のDIMENSIONを列挙する。

    寸法の見た目用匿名ブロック（*D...）自体は走査対象から除く。
    """
    for block in doc.blocks:
        if block.name.upper().startswith("*D"):
            continue
        yield from block.query("DIMENSION")


def clean_dxf_dimension_text(uploaded_file, keep_number=True, target_version=None):
    """アップロードされたDXFの寸法テキストから余分な "<>" を除去し、バイト列を返す。

    keep_number=True: "300<><>" → "300"（表示中の数値を文字として残す）
    keep_number=False: "300<><>" → ""（上書きを解除し、CAD側の実測値表示に戻す）
    """
    uploaded_file.seek(0)
    doc, auditor = recover.read(uploaded_file)
    source_version = doc.dxfversion

    stats = {
        "dimensions": 0,
        "text_fixed": 0,
        "block_text_fixed": 0,
        "dimpost_fixed": 0,
        "dimstyle_fixed": 0,
    }

    # 寸法スタイルの接尾辞/接頭辞（DIMPOST）が "<>" だけの場合は空にする
    for dimstyle in doc.dimstyles:
        if dimstyle.dxf.get("dimpost", "").strip() == "<>":
            dimstyle.dxf.dimpost = ""
            stats["dimstyle_fixed"] += 1

    for dim in iter_dimensions(doc):
        stats["dimensions"] += 1

        # 寸法ごとのスタイル上書き（XDATA）に入った DIMPOST "<>"
        override = dim.override()
        if (override.get("dimpost") or "").strip() == "<>":
            override["dimpost"] = ""
            override.commit()
            stats["dimpost_fixed"] += 1

        new_text = strip_placeholders(dim.dxf.get("text", ""))
        if new_text is None:
            continue
        dim.dxf.text = new_text if keep_number else ""
        stats["text_fixed"] += 1
        stats["block_text_fixed"] += fix_dimension_block_text(doc, dim, new_text)

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

    return output_bytes, stats, auditor, source_version, doc.dxfversion


# ========== UI ==========

st.title("VW出力DXF校正")

st.write(
    "Vectorworksから書き出したDXFをRhinoなどで開くと、寸法値の末尾に「<>」が表示される問題を修正します。"
)

with st.expander("修正内容について"):
    st.markdown(
        """
        - **原因**: Vectorworksの書き出すDXFでは、寸法の上書きテキストが `300<><>` のように
          「表示中の数値 + `<>` 2つ」になっています。`<>` は実測値のプレースホルダのため、
          Rhinoでは余った `<>` がそのまま文字として表示されます
        - **寸法の上書きテキスト**: 末尾の余分な `<>` を除去します
            - `<>` が2つ以上続く場合、または数値の直後に `<>` が1つの場合が対象です
            - `<>` だけのもの、`W=<>mm` のような前後注記は変更しません
        - **寸法の見た目用ブロック（`*D...`）**: 中の文字（`\\A1;5050<>` など）も修正後の数値に置き換えます
        - **寸法スタイルの接尾辞（DIMPOST）**: `<>` だけが入っている場合は空にします（寸法スタイル・寸法ごとの上書きの両方）
        - **数値の扱い**
            - *数値を残す（推奨）*: `300<><>` → `300`。Vectorworksで表示していた数値をそのまま文字として残します。
              Rhinoは寸法の尺度（DIMLFAC）を読み込まないため、縮尺付きの図面ではこちらを選んでください
            - *実測値表示に戻す*: 上書きテキストを空にし、CAD側で計測値を表示させます。AutoCADなど尺度を解釈するCAD向けです
        """
    )

uploaded_file = st.file_uploader("DXFファイルを選択してください", type=["dxf"])

if uploaded_file is not None:
    st.success(f"ファイルを読み込みました: **{uploaded_file.name}**")

    mode = st.radio(
        "寸法値の扱い",
        options=["数値を残す（推奨）", "実測値表示に戻す"],
        help="Rhinoで使う場合は「数値を残す」を選んでください。",
    )
    keep_number = mode == "数値を残す（推奨）"

    version_options = ["入力ファイルと同じ"] + list(DXF_VERSION_CHOICES.keys())
    selected_version = st.selectbox(
        "出力DXFバージョン",
        options=version_options,
        index=0,
        help="取り込み先ソフトが新しいDXFを読めない場合は古いバージョンを選択してください。",
    )
    target_version = (
        None if selected_version == "入力ファイルと同じ"
        else DXF_VERSION_CHOICES[selected_version]
    )

    if st.button("<> を除去", type="primary"):
        with st.spinner("処理中..."):
            try:
                (
                    output_bytes,
                    stats,
                    auditor,
                    source_version,
                    output_version,
                ) = clean_dxf_dimension_text(
                    uploaded_file, keep_number=keep_number, target_version=target_version
                )
            except ezdxf.DXFStructureError as e:
                st.error(f"DXFファイルの読み込みに失敗しました: {e}")
            except Exception as e:
                st.error(f"処理中にエラーが発生しました: {e}")
            else:
                fixed_total = (
                    stats["text_fixed"] + stats["dimpost_fixed"] + stats["dimstyle_fixed"]
                )
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("寸法の数", f"{stats['dimensions']} 個")
                with col2:
                    st.metric("修正した寸法テキスト", f"{stats['text_fixed']} 個")
                with col3:
                    st.metric(
                        "修正した接尾辞（DIMPOST）",
                        f"{stats['dimstyle_fixed'] + stats['dimpost_fixed']} 件",
                    )

                if fixed_total == 0:
                    st.info("修正が必要な寸法は見つかりませんでした。")
                else:
                    st.success("修正が完了しました。")

                src_label = ezdxf.const.acad_release.get(source_version, source_version)
                out_label = ezdxf.const.acad_release.get(output_version, output_version)
                st.caption(
                    f"入力DXFバージョン: {src_label} → 出力: {out_label}"
                    f"　／　見た目用ブロック内の文字を {stats['block_text_fixed']} 個修正"
                )

                if auditor.has_errors:
                    with st.expander(
                        f"読み込み時の警告 ({len(auditor.errors)} 件)"
                    ):
                        for err in auditor.errors[:50]:
                            st.text(str(err))

                base_name = uploaded_file.name.rsplit(".", 1)[0]
                output_filename = f"{base_name}_dimfix.dxf"

                st.download_button(
                    label="修正後のDXFをダウンロード",
                    data=output_bytes,
                    file_name=output_filename,
                    mime="application/dxf",
                    type="primary",
                )
else:
    st.info("DXFファイルをアップロードすると修正できます。")
