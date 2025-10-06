import streamlit as st
from PyPDF2 import PdfReader, PdfWriter
import io
import zipfile

def get_pdf_info(pdf_file):
    """PDFファイルの基本情報を取得"""
    # ファイルサイズ
    pdf_file.seek(0, 2)  # ファイルの末尾に移動
    file_size = pdf_file.tell()  # 現在位置=ファイルサイズ
    pdf_file.seek(0)  # 先頭に戻す

    # PDFを読み込み
    reader = PdfReader(pdf_file)
    page_count = len(reader.pages)

    # 文字数カウント
    total_chars = 0
    for page in reader.pages:
        text = page.extract_text() or ""
        total_chars += len(text)

    return {
        'file_size': file_size,
        'page_count': page_count,
        'char_count': total_chars,
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

def calculate_split_info(reader, num_splits, file_size):
    """分割情報を計算してプレビュー用のデータを返す"""
    total_pages = len(reader.pages)
    pages_per_split = total_pages // num_splits
    remainder = total_pages % num_splits

    splits_info = []
    start_page = 0

    for i in range(num_splits):
        # 余りを最初の分割に振り分け
        current_pages = pages_per_split + (1 if i < remainder else 0)
        end_page = start_page + current_pages - 1

        # この範囲の文字数をカウント
        char_count = 0
        for page_num in range(start_page, end_page + 1):
            text = reader.pages[page_num].extract_text() or ""
            char_count += len(text)

        # 推定ファイルサイズ（ページ数の比率から概算）
        estimated_size = int(file_size * current_pages / total_pages)

        splits_info.append({
            'part_num': i + 1,
            'start_page': start_page + 1,  # ユーザー向けは1始まり
            'end_page': end_page + 1,
            'page_count': current_pages,
            'char_count': char_count,
            'estimated_size': estimated_size
        })

        start_page = end_page + 1

    return splits_info

def split_pdf(reader, splits_info, original_filename):
    """PDFを分割してバイトストリームのリストを返す"""
    split_files = []
    base_name = original_filename.rsplit('.', 1)[0]

    for info in splits_info:
        writer = PdfWriter()

        # ページを追加（内部的には0始まり）
        for page_num in range(info['start_page'] - 1, info['end_page']):
            writer.add_page(reader.pages[page_num])

        # メモリ上に書き込み
        output = io.BytesIO()
        writer.write(output)
        output.seek(0)

        filename = f"{base_name}_part{info['part_num']}.pdf"
        split_files.append({
            'filename': filename,
            'data': output
        })

    return split_files

def create_zip(split_files, original_filename):
    """分割されたPDFファイルをZIPにまとめる"""
    zip_buffer = io.BytesIO()
    base_name = original_filename.rsplit('.', 1)[0]

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for file_info in split_files:
            file_info['data'].seek(0)
            zip_file.writestr(file_info['filename'], file_info['data'].read())

    zip_buffer.seek(0)
    return zip_buffer, f"{base_name}_split.zip"

# ========== UI ==========

st.title("PDF Splitter")

# ファイルアップロード
uploaded_file = st.file_uploader("PDFファイルを選択してください", type="pdf")

if uploaded_file is not None:
    # PDFの基本情報を取得
    with st.spinner("PDFを解析中..."):
        pdf_info = get_pdf_info(uploaded_file)

    # 基本情報を表示
    st.success("PDFの読み込みが完了しました")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("ファイルサイズ", format_size(pdf_info['file_size']))
    with col2:
        st.metric("総ページ数", f"{pdf_info['page_count']} ページ")
    with col3:
        st.metric("総文字数", f"{pdf_info['char_count']:,} 文字")

    st.divider()

    # 分割数の入力
    num_splits = st.number_input(
        "分割数を指定してください",
        min_value=2,
        max_value=pdf_info['page_count'],
        value=min(2, pdf_info['page_count']),
        step=1
    )

    # 分割プレビューを計算
    splits_info = calculate_split_info(
        pdf_info['reader'],
        num_splits,
        pdf_info['file_size']
    )

    # プレビューテーブルを表示
    st.subheader("分割プレビュー")

    preview_data = []
    for info in splits_info:
        preview_data.append({
            "分割ファイル": f"Part {info['part_num']}",
            "ページ範囲": f"{info['start_page']}-{info['end_page']}",
            "ページ数": info['page_count'],
            "推定サイズ": format_size(info['estimated_size']),
            "文字数": f"{info['char_count']:,}"
        })

    st.dataframe(preview_data, use_container_width=True, hide_index=True)

    st.divider()

    # 自動的に分割処理を実行してZIPを生成
    with st.spinner("PDFを分割中..."):
        # PDFを分割
        split_files = split_pdf(
            pdf_info['reader'],
            splits_info,
            uploaded_file.name
        )

        # ZIPにまとめる
        zip_data, zip_filename = create_zip(split_files, uploaded_file.name)

    st.success(f"✅ 分割準備完了（{num_splits}個のファイル）")

    # ZIPダウンロードボタン
    st.download_button(
        label="📦 分割PDFをZIPでダウンロード",
        data=zip_data,
        file_name=zip_filename,
        mime="application/zip",
        type="primary"
    )
