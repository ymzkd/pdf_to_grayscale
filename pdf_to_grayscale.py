import streamlit as st 
from PIL import Image
import fitz  # PyMuPDF
import io


def convert_to_grayscale(input_pdf_stream, dpi=300):
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
            output_pdf_stream = convert_to_grayscale(uploaded_file, dpi=dpi)
        
        st.success("Conversion successful!")
        
        # 入力ファイル名を取得し、拡張子を変更
        input_filename = uploaded_file.name
        output_filename = input_filename.rsplit('.', 1)[0] + "_GR.pdf"
        
        st.download_button(
            label="Download Grayscale PDF",
            data=output_pdf_stream,
            file_name=output_filename,  # 変更されたファイル名を使用
            mime="application/pdf"
        )