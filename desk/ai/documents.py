import io
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile
from pypdf import PdfReader


def extract_voice(upload, *, max_chars=16000):
    data = upload.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024: raise ValueError('Документ должен быть не больше 5 МБ.')
    extension = Path(upload.name).suffix.lower()
    try:
        if extension in {'.txt', '.md'}:
            text = data.decode('utf-8-sig')
        elif extension == '.docx':
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if sum(x.file_size for x in archive.infolist()) > 20 * 1024 * 1024:
                    raise ValueError('Распакованный документ превышает 20 МБ.')
                root = ET.fromstring(archive.read('word/document.xml'))
                ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                text = '\n'.join(''.join(t.text or '' for t in p.findall('.//w:t', ns)) for p in root.findall('.//w:p', ns))
        elif extension == '.pdf':
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted or len(reader.pages) > 100: raise ValueError('PDF должен быть без пароля и не более 100 страниц.')
            pieces, size = [], 0
            for page in reader.pages:
                part = page.extract_text() or ''
                size += len(part)
                if size > max_chars: raise ValueError(f'Текст документа не должен превышать {max_chars} символов.')
                pieces.append(part)
            text = '\n'.join(pieces)
        else: raise ValueError('Поддерживаются TXT, Markdown, PDF и DOCX.')
    except ValueError: raise
    except Exception as e: raise ValueError('Документ не удалось прочитать. Для сканов PDF нужен предварительный OCR.') from e
    text = text.strip()
    if not text or len(text) > max_chars: raise ValueError(f'Нужен текст от 1 до {max_chars} символов. Для сканов PDF нужен OCR.')
    return text
