import streamlit as st 
import PyPDF2
from PIL import Image
from io import BytesIO
import fitz  # PyMuPDF
import io

def convert_to_grayscale(input_pdf_path, output_pdf_path):
    # PDFファイルを読み込む
    pdf_document = fitz.open(input_pdf_path)
    pdf_writer = fitz.open()

    for page_num in range(pdf_document.page_count):
        # ページを取得
        page = pdf_document.load_page(page_num)
        
        # ページを画像として保存
        pix = page.get_pixmap(dpi=350)
        img = Image.open(io.BytesIO(pix.tobytes()))
        
        # グレースケールに変換
        grayscale_img = img.convert('L')
        
        # 画像を一時バッファに保存
        img_buffer = io.BytesIO()
        grayscale_img.save(img_buffer, format='PNG')
        
        # グレースケール画像を新しいPDFページとして追加
        new_page = pdf_writer.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(page.rect, stream=img_buffer.getvalue())
    
    # グレースケールに変換したPDFを保存
    pdf_writer.save(output_pdf_path)

# # 変換するPDFファイルのパス
# input_pdf = "input.pdf"
# output_pdf = "output_grayscale.pdf"

# convert_to_grayscale(input_pdf, output_pdf)

def convert_to_grayscale2(input_pdf_stream, dpi=300):
    pdf_document = fitz.open("pdf", input_pdf_stream.read())
    pdf_writer = fitz.open()
    
    for page_num in range(pdf_document.page_count):
        page = pdf_document.load_page(page_num)
        
        pix = page.get_pixmap(dpi=dpi)
        img = Image.open(io.BytesIO(pix.tobytes()))
        
        grayscale_img = img.convert('L')
        
        img_buffer = io.BytesIO()
        grayscale_img.save(img_buffer, format='PNG')
        
        new_page = pdf_writer.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(page.rect, stream=img_buffer.getvalue())
    
    output_pdf_stream = io.BytesIO()
    pdf_writer.save(output_pdf_stream)
    pdf_writer.close()
    output_pdf_stream.seek(0)
    
    return output_pdf_stream

st.title("PDF to Grayscale Converter")
uploaded_file = st.file_uploader("Choose a PDF file", type="pdf")
if uploaded_file is not None:
    st.write("File uploaded successfully.")
    
    dpi = st.slider("Select DPI (Resolution)", 100, 600, 300)
    
    if st.button("Convert to Grayscale"):
        with st.spinner("Converting..."):
            output_pdf_stream = convert_to_grayscale2(uploaded_file, dpi=dpi)
        
        st.success("Conversion successful!")
        
        st.download_button(
            label="Download Grayscale PDF",
            data=output_pdf_stream,
            file_name="grayscale.pdf",
            mime="application/pdf"
        )