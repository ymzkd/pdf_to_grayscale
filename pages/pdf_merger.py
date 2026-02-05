import streamlit as st
from PyPDF2 import PdfReader, PdfWriter
import io

def get_pdf_info(pdf_file):
    """PDFファイルの基本情報を取得"""
    # ファイルサイズ
    pdf_file.seek(0, 2)
    file_size = pdf_file.tell()
    pdf_file.seek(0)

    # PDFを読み込み
    reader = PdfReader(pdf_file)
    page_count = len(reader.pages)

    return {
        'file_size': file_size,
        'page_count': page_count,
        'reader': reader
    }

def format_size(bytes_size):
    """バイトサイズを読みやすい形式に変換"""
    if bytes_size < 1024:
        return f"{bytes_size} B"
    elif bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.2f} KB"
    else:
        return f"{bytes_size / (1024 * 1024):.2f} MB"

def merge_pdfs(pdf_files, file_order):
    """複数のPDFを指定順序で結合"""
    writer = PdfWriter()

    for idx in file_order:
        pdf_file = pdf_files[idx]
        pdf_file.seek(0)
        reader = PdfReader(pdf_file)
        for page in reader.pages:
            writer.add_page(page)

    output = io.BytesIO()
    writer.write(output)
    output.seek(0)
    return output

# ========== UI ==========

st.title("PDF Merger")

st.write("複数のPDFファイルを1つに結合します。")

# 複数ファイルアップロード
uploaded_files = st.file_uploader(
    "PDFファイルを選択してください（複数選択可）",
    type="pdf",
    accept_multiple_files=True
)

if uploaded_files and len(uploaded_files) >= 2:
    # 各ファイルの情報を取得
    files_info = []
    total_pages = 0
    total_size = 0

    with st.spinner("PDFを解析中..."):
        for i, pdf_file in enumerate(uploaded_files):
            info = get_pdf_info(pdf_file)
            files_info.append({
                'index': i,
                'name': pdf_file.name,
                'page_count': info['page_count'],
                'file_size': info['file_size']
            })
            total_pages += info['page_count']
            total_size += info['file_size']

    st.success(f"{len(uploaded_files)} 個のPDFを読み込みました")

    # 全体の情報を表示
    col1, col2 = st.columns(2)
    with col1:
        st.metric("合計ファイルサイズ", format_size(total_size))
    with col2:
        st.metric("合計ページ数", f"{total_pages} ページ")

    st.divider()

    # ファイルの順序設定
    st.subheader("結合順序の設定")
    st.write("各ファイルの結合順序を指定してください（1から開始）")

    # セッションステートで順序を管理
    if 'file_order' not in st.session_state or len(st.session_state.file_order) != len(uploaded_files):
        st.session_state.file_order = list(range(len(uploaded_files)))

    # 順序設定のUI
    order_inputs = []
    preview_data = []

    for i, info in enumerate(files_info):
        col1, col2 = st.columns([1, 3])
        with col1:
            order = st.number_input(
                f"順序",
                min_value=1,
                max_value=len(uploaded_files),
                value=i + 1,
                key=f"order_{i}",
                label_visibility="collapsed"
            )
            order_inputs.append((order, i))
        with col2:
            st.write(f"**{info['name']}** ({info['page_count']}ページ, {format_size(info['file_size'])})")

    # 順序を解決（重複がある場合は元のインデックス順でソート）
    order_inputs.sort(key=lambda x: (x[0], x[1]))
    file_order = [x[1] for x in order_inputs]

    st.divider()

    # 結合プレビュー
    st.subheader("結合プレビュー")

    preview_data = []
    cumulative_pages = 0
    for idx in file_order:
        info = files_info[idx]
        start_page = cumulative_pages + 1
        end_page = cumulative_pages + info['page_count']
        preview_data.append({
            "順序": len(preview_data) + 1,
            "ファイル名": info['name'],
            "ページ数": info['page_count'],
            "結合後のページ範囲": f"{start_page}-{end_page}",
            "サイズ": format_size(info['file_size'])
        })
        cumulative_pages = end_page

    st.dataframe(preview_data, use_container_width=True, hide_index=True)

    st.divider()

    # 結合処理
    with st.spinner("PDFを結合中..."):
        merged_pdf = merge_pdfs(uploaded_files, file_order)

        # 出力ファイル名を生成
        first_file_name = uploaded_files[file_order[0]].name.rsplit('.', 1)[0]
        output_filename = f"{first_file_name}_merged.pdf"

    st.success(f"結合完了（{total_pages}ページ）")

    # ダウンロードボタン
    st.download_button(
        label="結合したPDFをダウンロード",
        data=merged_pdf,
        file_name=output_filename,
        mime="application/pdf",
        type="primary"
    )

elif uploaded_files and len(uploaded_files) == 1:
    st.warning("2つ以上のPDFファイルをアップロードしてください。")
else:
    st.info("複数のPDFファイルをアップロードすると、結合オプションが表示されます。")
