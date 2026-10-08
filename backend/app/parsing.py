"""Local file parser contract, format selection and bounded table extraction."""
import csv
import io
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

TEXT_FORMATS = {'.txt', '.md', '.markdown', '.csv', '.tsv', '.json', '.html', '.htm'}
NATIVE_FORMATS = TEXT_FORMATS | {'.pdf', '.docx', '.xlsx', '.pptx'}


class ParserOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['auto', 'native', 'text'] = 'auto'
    text_encoding: Literal['auto', 'utf-8', 'gb18030', 'utf-16'] = 'auto'
    max_rows: int = Field(default=500, ge=1, le=20000)


def parser_catalog():
    return [
        {'id': 'auto', 'name': 'Automatic local parsing', 'formats': sorted(NATIVE_FORMATS),
         'description': 'Bounded native readers; optional configured local parser fallback.'},
        {'id': 'native', 'name': 'Native document parsing', 'formats': sorted(NATIVE_FORMATS),
         'description': 'Text PDF, Office XML, visible HTML and structured tables. No OCR or external calls.'},
        {'id': 'text', 'name': 'Plain text parsing', 'formats': sorted(TEXT_FORMATS),
         'description': 'Encoding-aware text only. HTML still removes scripts and styles.'},
    ]


def validate_selection(filename, options):
    allowed = TEXT_FORMATS if options.mode == 'text' else NATIVE_FORMATS
    if Path(filename).suffix.lower() not in allowed:
        raise ValueError('Selected parser does not support this file type')


def decode_text(data, encoding='auto'):
    if encoding != 'auto':
        return data.decode('utf-8-sig' if encoding == 'utf-8' else encoding)
    encodings = ['utf-16'] if data.startswith((b'\xff\xfe', b'\xfe\xff')) else ['utf-8-sig', 'gb18030']
    for candidate in encodings:
        try:
            return data.decode(candidate)
        except UnicodeDecodeError:
            continue
    raise ValueError('Text encoding could not be decoded')


def parse_tabular(path, extension, options):
    reader = csv.reader(io.StringIO(decode_text(Path(path).read_bytes(), options.text_encoding)),
                        delimiter='\t' if extension == '.tsv' else ',')
    rows, truncated = [], False
    for index, row in enumerate(reader):
        if index >= options.max_rows:
            truncated = True
            break
        if len(row) > 1000:
            raise ValueError('Table exceeds column processing limit')
        rows.append(' | '.join(cell.replace('\n', ' ').replace('\r', ' ').replace('|', '\\|') for cell in row))
    return '\n'.join(rows) + ('\n[Table truncated by local row limit]' if truncated else '')
