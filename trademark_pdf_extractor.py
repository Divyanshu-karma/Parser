#backend- trademark_pdf_extractor.py

"""
Ultimate Trademark PDF Analysis System (Improved)
"""
import asyncio
import os
import re
import time
import json
import tempfile
from datetime import date
from dateutil import parser as dateparser
from typing import List, Dict, Any, Tuple
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi import BackgroundTasks
import requests
import pdfplumber
import fitz

TM_OUTPUT_DIR = os.getenv("TM_OUTPUT_DIR", "TM")

os.makedirs(TM_OUTPUT_DIR, exist_ok=True)

try:
    import camelot
    _HAS_CAMELOT = True
except Exception:
    camelot = None
    _HAS_CAMELOT = False

import spacy
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse


# -----------------------------
# Regex Rules
# -----------------------------

SERIAL_RE = re.compile(r'\bSerial(?:\s+Number)?[:\s]*([0-9]{8})\b', re.I)

DATE_RE = re.compile(
    r'\b(Filing Date|Filed|Date of Filing|Date)[:\s]*([A-Za-z0-9,./\-\s]+)',
    re.I
)

CLASS_RE = re.compile(
    r'\b(Class(?:es)?|International Class)[:\s]*([0-9]{1,3}(?:\s*,\s*[0-9]{1,3})*)',
    re.I
)

BASIS_RE = re.compile(
    r'\b(FILING\s+BASIS(?:\s+INFORMATION)?|BASIS\s+OF\s+FILING|Filing\s+Basis)'
    r'[:\s\-]*(1\(a\)|1\(b\)|44\(d\)|44\(e\)|66\(a\))',
    re.I
)

SECTION_RE = re.compile(
    r'\bSection\s+(1\(a\)|1\(b\)|44\(d\)|44\(e\)|66\(a\))',
    re.I
)

APPLICANT_RE = re.compile(
    r'^\s*(Applicant|Owner|Registrant|Owner name)[:\s\-]+([^\n]+)',
    re.I | re.M
)

MARK_RE = re.compile(
    r'^\s*\*?MARK\s+(?!INFORMATION|STATEMENT)([^\n]+)',
    re.I | re.M
)

IDENT_PLACEHOLDER = re.compile(
    r'\b(n/?a|tbd|to be determined|unknown|unspecified)\b',
    re.I
)


LABEL_MAP = {
    'filing basis': 'filing_basis',
    'filing basis information': 'filing_basis',
    'basis of filing': 'filing_basis',
    'serial number': 'serial_number',
    'applicant': 'applicant_name',
    'owner': 'applicant_name',
    'international class': 'classes',
    'class': 'classes',
    'identification of goods and services': 'identification_text',
}


# -----------------------------
# Font helpers
# -----------------------------

def normalize_fontname(fontname: str) -> str:
    if not fontname:
        return ''
    if '+' in fontname:
        fontname = fontname.split('+')[-1]
    return fontname


def is_bold_fontname(fontname: str) -> bool:
    fn = normalize_fontname(fontname).lower()
    return "bold" in fn or "black" in fn


# -----------------------------
# Processor
# -----------------------------

class PDFProcessor:

    def __init__(self):
        self.nlp = spacy.blank("en")

        ruler = self.nlp.add_pipe("entity_ruler")

        patterns = [
            {"label": "FILING_BASIS", "pattern": [{"LOWER": "filing"}, {"LOWER": "basis"}]},
            {"label": "SERIAL_NUMBER", "pattern": [{"LOWER": "serial"}, {"LOWER": "number"}]},
            {"label": "APPLICANT", "pattern": [{"LOWER": "applicant"}]},
        ]

        ruler.add_patterns(patterns)

    # -----------------------------
    # PDF Text Extraction
    # -----------------------------

    def extract_with_pdfplumber(self, path):

        pages = []
        full_text = []

        with pdfplumber.open(path) as pdf:

            for pno, page in enumerate(pdf.pages, start=1):

                text = page.extract_text() or ""
                full_text.append(text)

                pages.append({
                    "page": pno,
                    "width": page.width,
                    "height": page.height
                })

        return {
            "pages": pages,
            "raw_text": "\n\n".join(full_text)
        }

    # -----------------------------
    # Table extraction
    # -----------------------------

    def extract_tables(self, path):

        tables = []

        with pdfplumber.open(path) as pdf:

            for pno, page in enumerate(pdf.pages, start=1):

                try:
                    tbls = page.find_tables()

                    for t in tbls:

                        table = t.extract()

                        if table:
                            tables.append({
                                "page": pno,
                                "rows": table
                            })

                except Exception:
                    pass

        if _HAS_CAMELOT and os.environ.get("ENABLE_CAMELOT") == "1":

            try:

                camelot_tables = camelot.read_pdf(
                    path,
                    pages="all",
                    flavor="stream"
                )

                for t in camelot_tables:

                    tables.append({
                        "page": int(t.page),
                        "rows": t.df.values.tolist()
                    })

            except Exception:
                pass

        return tables

    # -----------------------------
    # Identification block
    # -----------------------------

    def extract_identification_block(self, text):

        
        pattern = re.compile(
            r'Identification of goods and services(.*?)(?:Additional statements|Translation|Correspondence information)',
            re.I | re.S
        )

        m = pattern.search(text)

        if not m:
            return ""

        block = m.group(1)

        block = re.sub(
            r'International Class\s+\d+\s*',
            '',
            block,
            flags=re.I
        )

        return block.strip()

    # -----------------------------
    # Text field detection
    # -----------------------------

    def find_fields_in_text(self, text):

        out = {}

        m = SERIAL_RE.search(text)
        if m:
            out["serial_number"] = m.group(1)

        m = DATE_RE.search(text)
        if m:
            out["filing_date"] = self.safe_parse_date(m.group(2))

        m = CLASS_RE.search(text)
        if m:
            nums = re.findall(r'\b\d{1,3}\b', m.group(2))
            classes = []
            for n in nums:
                val = int(n)
                if 1 <= val <= 45:
                    classes.append(val)
            out["classes"] =  list(set(classes))

        m = BASIS_RE.search(text)
        if m:
            out["filing_basis"] = m.group(2)

        if "filing_basis" not in out:
            m = SECTION_RE.search(text)
            if m:
                out["filing_basis"] = m.group(1)

        m = APPLICANT_RE.search(text)
        if m:
            out["applicant_name"] = m.group(2).strip()

        m = MARK_RE.search(text)
        if m:
            out["mark_text"] = m.group(1).strip()

        return out

    # -----------------------------
    # Date Parsing
    # -----------------------------

    def safe_parse_date(self, txt):

        try:

            txt = re.sub(r'\bET\b', '', txt)
            txt = re.sub(r'\bat\b', '', txt)

            d = dateparser.parse(txt, fuzzy=True)

            if d:
                return d.date().isoformat()

        except:
            pass

        return None

    # -----------------------------
    # Validation
    # -----------------------------

    def validate(self, extracted):

        errors = []
        warnings = []

        sn = extracted.get("serial_number")

        if not sn or not re.fullmatch(r"\d{8}", str(sn)):
            errors.append("serial_number_format")

        fd = extracted.get("filing_date")

        if not fd:
            errors.append("filing_date_missing")

        classes = extracted.get("classes", [])

        if classes:
            try:
                if any(int(c) < 1 or int(c) > 45 for c in classes):
                    errors.append("class_number_invalid")
            except:
                errors.append("class_number_invalid")
            
        else:
            warnings.append("no_classes_found")

        ident = extracted.get("identification_text")

        if not ident:
            errors.append("identification_missing")

        elif IDENT_PLACEHOLDER.search(ident):
            errors.append("identification_placeholder")

        return errors, warnings

    # -----------------------------
    # Main pipeline
    # -----------------------------

    def process_pdf(self, path):

        plumb = self.extract_with_pdfplumber(path)

        text = plumb["raw_text"]

        tables = self.extract_tables(path)

        extracted = {}

        # text fields
        extracted.update(self.find_fields_in_text(text))

        # identification block
        ident = self.extract_identification_block(text)

        if ident:
            extracted["identification_text"] = ident

        extracted["tables"] = tables

        extracted["raw_text_snippet"] = text[:3000]

        errors, warnings = self.validate(extracted)

        extracted["errors"] = errors
        extracted["warnings"] = warnings

        extracted["confidence"] = self.simple_confidence(extracted)

        return extracted

    def simple_confidence(self, extracted):

        score = 0.5

        for k in [
            "serial_number",
            "filing_date",
            "filing_basis",
            "applicant_name",
            "identification_text"
        ]:
            if extracted.get(k):
                score += 0.1

        score -= 0.2 * len(extracted.get("errors", []))

        return max(0.0, min(0.99, round(score, 2)))


# -----------------------------
# FastAPI
# -----------------------------

app = FastAPI(title="Trademark PDF Extractor")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
processor = PDFProcessor()
def forward_json_to_server(data: dict):
    """
    Sends extracted JSON to the Hugging Face classification backend
    and returns the classifier response.
    """

    TARGET_API = "https://divya-nshu99-pk.hf.space/classify"

    try:
        response = requests.post(
            TARGET_API,
            json=data,
            timeout=30
        )

        response.raise_for_status()

        return response.json()

    except requests.exceptions.RequestException as e:
        return {
            "status": "failed",
            "error": str(e)
        }

def forward_mark_text_to_server(mark_text: str):
    """
    Sends trademark mark text to HuggingFace mark conflict backend
    and returns the response exactly as received.
    """

    MARK_API = "https://divya-nshu99-tmconflict.hf.space/search"

    try:
        response = requests.post(
            MARK_API,
            json={"mark_text": mark_text, "filing_status": "active"},
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        # Ensure we return exactly the list returned by HF backend
        if isinstance(data, list):
            return data

        # fallback safety
        return []

    except requests.exceptions.RequestException as e:
        return [{
            "status": "failed",
            "error": str(e)
        }]

@app.post("/extract")

async def extract_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...)
):

    if not file.filename.lower().endswith(".pdf") or file.content_type != "application/pdf":
        return JSONResponse(
            {"error": "only pdf allowed"},
            status_code=400
        )

    contents = await file.read()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tf:

        tf.write(contents)
        tmp_path = tf.name

    try:
        
        result = processor.process_pdf(tmp_path)
        mark_text = result.get("mark_text", "")
        classes = result.get("classes", [])
        class_number = classes[0] if classes else None

        payload = {
            "class_number": class_number,
            "identification": result.get("identification_text", "").replace("\n", " ")
        }

        
      
        # -----------------------------
# Run BOTH backend calls concurrently
# -----------------------------

        classification_task = asyncio.create_task(
            call_classification_backend(payload)
        )

        if mark_text.strip():
            mark_task = asyncio.create_task(call_mark_backend(mark_text))
        else:
            mark_task = asyncio.create_task(asyncio.sleep(0))

# first wait for classification
        classification = await classification_task
        result["classification_result"] = classification

# then wait for mark analysis
        mark_response= await mark_task or []

# -----------------------------
# Save mark analysis separately
# -----------------------------

        serial = result.get("serial_number", f"tm_{int(time.time())}")

        mark_filename = f"{serial}_mark_analysis.json"
        mark_output_path = os.path.join(TM_OUTPUT_DIR, mark_filename)

        with open(mark_output_path, "w", encoding="utf-8") as f:
            json.dump(mark_response, f, indent=2, ensure_ascii=False)
        result["mark_analysis"] = mark_response


       
# -----------------------------
# Save JSON output
# -----------------------------

        serial = result.get("serial_number")

        if serial:
            filename = f"{serial}.json"
        else:
            filename = f"tm_result_{int(time.time())}.json"

        output_path = os.path.join(TM_OUTPUT_DIR, filename)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        result["stored_json_path"] = output_path

    except Exception as e:

        return JSONResponse({"error": str(e)}, status_code=500)

    finally:

        try:
            os.unlink(tmp_path)
        except Exception:
            pass


    return JSONResponse(result)
async def call_classification_backend(payload):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, forward_json_to_server, payload)


async def call_mark_backend(mark_text):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, forward_mark_text_to_server, mark_text)    

@app.get("/")
def serve_frontend():
    return FileResponse(
        os.path.join(os.getcwd(), "taraai.html")
    )
@app.get("/health")
def health():
    return {"status": "ok"}
@app.get("/version")
def version():
    return {
        "service": "trademark_pdf_extractor",
        "version": "1.0"
    }










