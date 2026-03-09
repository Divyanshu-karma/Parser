# trademark_pdf_extractor.py
"""
Ultimate Trademark PDF Analysis System
"""
import asyncio
import os
import re
import time
import json
import tempfile
from datetime import date
from dateutil import parser as dateparser
from typing import List, Dict, Any, Tuple, Optional 
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi import BackgroundTasks
import requests
import pdfplumber
import fitz
import functools

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


# ─────────────────────────────────────────────────────────────────────────────
# Regex Rules
# ─────────────────────────────────────────────────────────────────────────────

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
    r'[:\s\-]*(?:SECTION\s*)?(1\(a\)|1\(b\)|44\(d\)|44\(e\)|66\(a\))',
    re.I
)

SECTION_RE = re.compile(
    r'\bSection\s+(1\(a\)|1\(b\)|44\(d\)|44\(e\)|66\(a\))',
    re.I
)

# Fixed: added "Owner(?:\s+of\s+Mark)?" to catch USPTO label "*OWNER OF MARK"
APPLICANT_RE = re.compile(
    r'^\s*\*?(?:Applicant|Owner(?:\s+of\s+Mark)?|Registrant|Owner\s*name)[:\s\-]+([^\n]+)',
    re.I | re.M
)

# Fixed: parenthetical suffix stripped in find_fields_in_text below
MARK_RE = re.compile(
    r'^\s*\*?MARK\b(?:\s+(?!INFORMATION|STATEMENT|CONSISTS))([^\n]+)',
    re.I | re.M
)


IDENT_PLACEHOLDER = re.compile(
    r'\b(n/?a|tbd|to be determined|unknown|unspecified)\b',
    re.I
)

LABEL_MAP = {
    # Mark
    'mark':                             'mark_text',
    'literal element':                  'literal_element',
    'mark statement':                   'mark_statement',
    'standard characters':              'standard_characters',
    'uspto-generated image':            'uspto_generated_image',
    'register':                         'register',
    # Serial / Filing
    'serial number':                    'serial_number',
    # Applicant
    'owner of mark':                    'applicant_name',
    'applicant':                        'applicant_name',
    'owner':                            'applicant_name',
    'registrant':                       'applicant_name',
    'mailing address':                  'mailing_address',
    # Legal entity
    'type':                             'legal_entity_type',
    'state/country/region/jurisdiction/u.s. territory where legally organized':
                                        'state_of_organization',
    # Goods / Basis
    'international class':              'classes',
    'class':                            'classes',
    'identification':                   'identification_text',
    'identification of goods and services': 'identification_text',
    'filing basis':                     'filing_basis',
    'filing basis information':         'filing_basis',
    'basis of filing':                  'filing_basis',
    # Attorney
    'firm name':                        'attorney_firm',
    'attorney docket number':           'attorney_docket',
    'phone':                            'attorney_phone',
    'fax':                              'attorney_fax',
    # Correspondence
    'primary email address for correspondence':             'correspondence_email',
    'secondary email address(es) (courtesy copies)':       'secondary_email',
    # Fees
    'application filing option':        'filing_option',
    'number of classes':                'number_of_classes',
    'application for registration per class': 'fee_per_class',
    'total fees due':                   'total_fees_due',
    'total fees paid':                  'total_fees_paid',
    # Signature
    'signature':                        'signature',
    "signatory's name":                 'signatory_name',
    "signatory's position":             'signatory_position',
    'date signed':                      'date_signed',
    'signature method':                 'signature_method',
}

# Section-header rows in the USPTO PTO-1478 two-column table
_SECTION_HEADERS = {
    'mark information':             'mark',
    'applicant information':        'applicant',
    'legal entity information':     'legal_entity',
    'owner domicile address(new)':  'domicile',
    'goods and/or services and basis information': 'goods',
    'attorney information':         'attorney',
    'correspondence information':   'correspondence',
    'fee information':              'fee',
    'signature information':        'signature',
}

# Context-sensitive labels: same label appears in multiple sections
_CONTEXT_LABEL_MAP = {
    ('applicant', 'city'):          'applicant_city',
    ('applicant', 'state'):         'applicant_state',
    ('applicant', 'country/region/jurisdiction/u.s. territory'): 'applicant_country',
    ('applicant', 'zip/postal code'): 'applicant_zip',
    ('applicant', 'email address'): 'applicant_email',
    ('domicile',  'city'):          'domicile_city',
    ('domicile',  'state'):         'domicile_state',
    ('domicile',  'country/region/jurisdiction/u.s. territory'): 'domicile_country',
    ('domicile',  'zip/postal code'): 'domicile_zip',
    ('attorney',  'city'):          'attorney_city',
    ('attorney',  'state'):         'attorney_state',
    ('attorney',  'country/region/jurisdiction/u.s. territory'): 'attorney_country',
    ('attorney',  'zip/postal code'): 'attorney_zip',
    ('attorney',  'email address'): 'attorney_email',
    ('attorney',  'name'):          'attorney_name',
    ('correspondence', 'name'):     'correspondence_name',
}
# ─────────────────────────────────────────────────────────────────────────────
# TEAS Plus format — constants (new format, used alongside PTO-1478 constants)
# ─────────────────────────────────────────────────────────────────────────────

# Summary-block regexes: TEAS Plus page 1 has a merged single-cell block
# "Serial number: XXXXXXXX\nMark: XXX\nFiling date: ..."
TEAS_SERIAL_RE = re.compile(r'Serial\s+number[:\s]+(\d{8})', re.I)
TEAS_DATE_RE   = re.compile(r'Filing\s+date[:\s]+([^\n]+)', re.I)
TEAS_OWNER_RE  = re.compile(r'Owner\s+name[:\s]+([^\n]+)', re.I)
TEAS_DOCKET_RE = re.compile(r'Docket\s+number[:\s]+([^\n]+)', re.I)
TEAS_AMOUNT_RE = re.compile(r'Amount\s+paid[:\s]+([^\n]+)', re.I)
# Exact "Mark:" only — must not match "Mark format:" or "Mark Format"
TEAS_MARK_RE   = re.compile(r'(?m)^Mark:\s*([^\n]+)', re.I)

# Section-header rows for TEAS Plus two-column tables.
# These rows have (label=section_name, value=None).
_TEAS_SECTION_HEADERS = {
    'owner information':                    'owner',
    'mailing address information':          'mailing',
    'domicile address information':         'domicile',
    'goods and services':                   'goods',
    'filing basis information':             'filing_basis_section',
    'identification of goods and services': 'identification',
    'additional statements':                'additional',
    'attorney information':                 'attorney',
    'attorney name and address':            'attorney',
    'attorney registration information':    'attorney_reg',
    'correspondence information':           'correspondence',
    'fee information':                      'fee',
    'declaration and signature':            'signature',
    'declaration':                          'signature',
}

# Context-sensitive label → canonical field for TEAS Plus format.
# Same label ('name', 'city', etc.) appears under multiple sections.
_TEAS_CONTEXT_LABEL_MAP = {
    # ── Owner ──────────────────────────────────────────────────────────────
    ('owner',    'name'):                                       'applicant_name',
    ('owner',    'entity type'):                                'legal_entity_type',
    ('owner',    'place of organization/citizenship'):          'state_of_organization',
    # ── Mailing address ────────────────────────────────────────────────────
    ('mailing',  'address line 1'):                            'mailing_address',
    ('mailing',  'city'):                                       'applicant_city',
    ('mailing',  'state/territory'):                            'applicant_state',
    ('mailing',  'zip/postal code'):                            'applicant_zip',
    ('mailing',  'country/region/jurisdiction/territory'):      'applicant_country',
    ('mailing',  'email address'):                              'applicant_email',
    # ── Domicile address ───────────────────────────────────────────────────
    ('domicile', 'address line 1'):                             'domicile_address',
    ('domicile', 'city'):                                       'domicile_city',
    ('domicile', 'state/territory'):                            'domicile_state',
    ('domicile', 'zip/postal code'):                            'domicile_zip',
    ('domicile', 'country/region/jurisdiction/territory'):      'domicile_country',
    # ── Attorney ───────────────────────────────────────────────────────────
    ('attorney', 'name'):                                       'attorney_name',
    ('attorney', 'law firm'):                                   'attorney_firm',
    ('attorney', 'address line 1'):                             'attorney_address',
    ('attorney', 'city'):                                       'attorney_city',
    ('attorney', 'state/territory'):                            'attorney_state',
    ('attorney', 'zip/postal code'):                            'attorney_zip',
    ('attorney', 'country/region/jurisdiction/territory'):      'attorney_country',
    ('attorney', 'email address'):                              'attorney_email',
    ('attorney', 'primary telephone number'):                   'attorney_phone',
    ('attorney', 'docket or reference number'):                 'attorney_docket',
    # ── Correspondence ─────────────────────────────────────────────────────
    ('correspondence', 'correspondence name'):                  'correspondence_name',
    ('correspondence', 'primary correspondence email address'): 'correspondence_email',
    ('correspondence', 'docket or reference number'):           'attorney_docket',
    ('correspondence', 'courtesy copy email addresses'):        'secondary_email',
    # ── Fee ────────────────────────────────────────────────────────────────
    ('fee', 'application filing option'):                       'filing_option',
    ('fee', 'number of classes'):                               'number_of_classes',
    ('fee', 'applications for registration, per class'):        'fee_per_class',
    ('fee', 'total fees due'):                                  'total_fees_due',
    ('fee', 'total fees paid'):                                 'total_fees_paid',
    # ── Signature / Declaration ────────────────────────────────────────────
    ('signature', 'electronic signature'):                      'signature',
    ('signature', "signatory's name"):                          'signatory_name',
    ('signature', "signatory's position"):                      'signatory_position',
    ('signature', 'date signed'):                               'date_signed',
    ('signature', 'signature method'):                          'signature_method',
}


# ─────────────────────────────────────────────────────────────────────────────
# Font helpers
# ─────────────────────────────────────────────────────────────────────────────

def normalize_fontname(fontname: str) -> str:
    if not fontname:
        return ''
    if '+' in fontname:
        fontname = fontname.split('+')[-1]
    return fontname


def is_bold_fontname(fontname: str) -> bool:
    fn = normalize_fontname(fontname).lower()
    return "bold" in fn or "black" in fn


# ─────────────────────────────────────────────────────────────────────────────
# PDF Processor
# ─────────────────────────────────────────────────────────────────────────────

class PDFProcessor:

    def __init__(self):
        self.nlp = spacy.blank("en")
        ruler = self.nlp.add_pipe("entity_ruler")
        patterns = [
            {"label": "FILING_BASIS",  "pattern": [{"LOWER": "filing"}, {"LOWER": "basis"}]},
            {"label": "SERIAL_NUMBER", "pattern": [{"LOWER": "serial"}, {"LOWER": "number"}]},
            {"label": "APPLICANT",     "pattern": [{"LOWER": "applicant"}]},
        ]
        ruler.add_patterns(patterns)

    def extract_with_pdfplumber(self, path):
        pages = []
        full_text = []
        with pdfplumber.open(path) as pdf:
            for pno, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                full_text.append(text)
                pages.append({"page": pno, "width": page.width, "height": page.height})
        return {"pages": pages, "raw_text": "\n\n".join(full_text)}

    def extract_tables(self, path):
        tables = []
        with pdfplumber.open(path) as pdf:
            for pno, page in enumerate(pdf.pages, start=1):
                try:
                    tbls = page.find_tables()
                    for t in tbls:
                        table = t.extract()
                        if table:
                            tables.append({"page": pno, "rows": table})
                            safe_rows = [
                                [str(c) if c is not None else None for c in row]
                                for row in table
                            ]
                            tables.append({"page": pno, "rows": safe_rows})
                except Exception:
                    pass

        if _HAS_CAMELOT and os.environ.get("ENABLE_CAMELOT") == "1":
            try:
                camelot_tables = camelot.read_pdf(path, pages="all", flavor="stream")
                for t in camelot_tables:
                    tables.append({"page": int(t.page), "rows": t.df.values.tolist()})
            except Exception:
                pass

        return tables

    def extract_identification_block(self, text):
        # Three patterns tried in priority order:
        # A — table-flattened text: "*IDENTIFICATION   Milk and milk products…"
        # B — narrative page:       "International Class 029: Milk and …"
        # C — original prose layout (kept as last resort)
        patterns = [
            re.compile(
                r'^\s*\*?IDENTIFICATION\s{2,}([^\n]+(?:\n(?![\s]*(?:FILING BASIS|INTERNATIONAL CLASS|ATTORNEY|CORRESPONDENCE|\*[A-Z]))[^\n]+)*)',
                re.I | re.M
            ),
            re.compile(
                r'International Class\s+\d+:\s*([^\n]+(?:\n(?!\n)[^\n]+)*)',
                re.I
            ),
            re.compile(
                r'Identification of goods and services(.*?)(?:Additional statements|Translation|Correspondence information|Filing Basis)',
                re.I | re.S
            ),
        ]
        for pat in patterns:
            m = pat.search(text)
            if m:
                block = m.group(1)
                block = re.sub(r'International Class\s+\d+\s*:?\s*', '', block, flags=re.I)
                block = block.strip()
                if block:
                    return block
        return ""

    def find_fields_in_text(self, text):
        # FALLBACK ONLY — called after extract_fields_from_tables() to fill gaps
        out = {}

        m = SERIAL_RE.search(text)
        if m:
            out["serial_number"] = m.group(1)

        m = DATE_RE.search(text)
        if m:
            date_raw = m.group(2).split('\n')[0].strip()
            out["filing_date"] = self.safe_parse_date(m.group(2))

        m = CLASS_RE.search(text)
        if m:
            nums = re.findall(r'\b\d{1,3}\b', m.group(2))
            seen, int_list, str_list = set(), [], []
            for n in nums:
                val = int(n)
                if 1 <= val <= 45 and val not in seen:
                    int_list.append(val)
                    str_list.append(str(val).zfill(3))
                    seen.add(val)
            if int_list:
                out["classes"]        = int_list
                out["class_strings"]  = str_list

        m = BASIS_RE.search(text)
        if m:
            out["filing_basis"] = m.group(2)
        elif "filing_basis" not in out:
            m = SECTION_RE.search(text)
            if m:
                out["filing_basis"] = m.group(1)

        # APPLICANT_RE now matches "*OWNER OF MARK" label
        m = APPLICANT_RE.search(text)
        if m:
            out["applicant_name"] = m.group(1).strip()

        m = MARK_RE.search(text)
        if m:
            raw_mark = m.group(1).strip()
            # Strip parenthetical suffix e.g. "(Standard Characters, see mark)"
            out["mark_text"] = re.sub(r'\s*\([^)]*\)\s*$', '', raw_mark).strip() or raw_mark

        return out

    def extract_fields_from_tables(self, path: str) -> dict:
        """
        PRIMARY extraction method for USPTO PTO-1478 two-column table PDFs.
        Reads [Label | Value] rows, tracks section headers for context,
        and resolves ambiguous labels (NAME, CITY, STATE, EMAIL) correctly.
        """
        out = {}
        current_section = None
        _REDACTED = {'xxxx', 'xxx', 'xx', 'x', 'n/a', '', 'not provided'}

        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                try:
                    found_tables = page.find_tables()
                except Exception:
                    continue
                for tbl in found_tables:
                    rows = tbl.extract()
                    if not rows:
                        continue
                    for row in rows:
                        if not row:
                            continue
                        label_raw = row[0] if len(row) > 0 else None
                        value_raw = row[1] if len(row) > 1 else None
                        if label_raw is None:
                            continue

                        label_str = ' '.join(str(label_raw).split()).strip()
                        label_key = label_str.lower().lstrip('*').strip()
                        # Strip parenthetical qualifiers USPTO appends:
                        # "State (Required for U.S. applicants)" → "state"
                        label_key = re.sub(r'\s*\(.*?\)\s*$', '', label_key).strip()

                        value_str = ' '.join(str(value_raw).split()).strip() if value_raw else ''

                        # Detect section-header rows
                        if label_key in _SECTION_HEADERS:
                            current_section = _SECTION_HEADERS[label_key]
                            continue

                        # Skip redacted / empty values
                        if value_str.lower() in _REDACTED:
                            continue

                        # Resolve canonical field name
                        canonical = _CONTEXT_LABEL_MAP.get((current_section, label_key))
                        if canonical is None:
                            canonical = LABEL_MAP.get(label_key)
                        if canonical is None or canonical in out:
                            continue

                        out[canonical] = self._coerce_field(canonical, value_str)

        return out

    def _coerce_field(self, canonical: str, raw: str):
        """Type-coerces a raw table cell string to the correct Python type."""
        if canonical == 'serial_number':
            m = re.search(r'\b(\d{8})\b', raw)
            return m.group(1) if m else raw

        if canonical in ('filing_date', 'date_signed'):
            return self.safe_parse_date(raw)

        if canonical == 'classes':
            nums = re.findall(r'\b(\d{1,3})\b', raw)
            seen, il, sl = set(), [], []
            for n in nums:
                v = int(n)
                if 1 <= v <= 45 and v not in seen:
                    il.append(v); sl.append(str(v).zfill(3)); seen.add(v)
            return {'int': il, 'str': sl}

        if canonical == 'filing_basis':
            # handles both "1(a)" and "SECTION 1(a)" cell values
            m = re.search(r'(1\(a\)|1\(b\)|44\(d\)|44\(e\)|66\(a\))', raw, re.I)
            if m:
                return m.group(1)
            m = re.search(r'SECTION\s+(1\(a\)|1\(b\)|44\(d\)|44\(e\)|66\(a\))', raw, re.I)
            return m.group(1) if m else raw

        if canonical in ('standard_characters', 'uspto_generated_image'):
            return raw.strip().upper() == 'YES'

        if canonical in ('total_fees_due', 'total_fees_paid', 'fee_per_class'):
            m = re.search(r'[\d,]+', raw.replace('$', ''))
            return m.group(0) if m else raw

        if canonical == 'mark_text':
            return re.sub(r'\s*\([^)]*\)\s*$', '', raw).strip() or raw

        return raw

    # =========================================================================
    # FIX — Format detector
    # Checks raw text for 'TEAS Plus' title (new format) vs
    # 'Input Field' table header (old PTO-1478 format).
    
    def _detect_pdf_format(self, text: str) -> str:
    
    if re.search(r'Input\s+Field', text, re.I):   
        return 'pto_1478'
    if re.search(r'TEAS\s+Plus', text, re.I):      
        return 'teas_plus'
    if TEAS_SERIAL_RE.search(text):
        return 'teas_plus'
    return 'pto_1478'

    # =========================================================================
    # FIX — TEAS Plus summary block extractor
    # Page 1 has a single merged table cell containing all key:value pairs
    # ("Serial number: ...\nMark: ...\nFiling date: ...").
    # Regex on raw text is the correct approach here.
    # =========================================================================
    def _extract_teas_plus_summary(self, text: str) -> dict:
        """Extracts fields from the TEAS Plus inline summary block (top of page 1)."""
        out = {}

        m = TEAS_SERIAL_RE.search(text)
        if m:
            out['serial_number'] = m.group(1).strip()

        # TEAS_MARK_RE matches "^Mark:" exactly — won't match "Mark format:"
        m = TEAS_MARK_RE.search(text)
        if m:
            out['mark_text'] = m.group(1).strip()

        m = TEAS_DATE_RE.search(text)
        if m:
            out['filing_date'] = self.safe_parse_date(m.group(1).strip())

        m = TEAS_OWNER_RE.search(text)
        if m:
            out['applicant_name'] = m.group(1).strip()

        m = TEAS_DOCKET_RE.search(text)
        if m:
            out['attorney_docket'] = m.group(1).strip()

        m = TEAS_AMOUNT_RE.search(text)
        if m:
            raw_amt = m.group(1).strip()
            amt_m = re.search(r'[\d,]+', raw_amt.replace('$', ''))
            out['total_fees_paid'] = amt_m.group(0) if amt_m else raw_amt
        return out

    # =========================================================================
    # FIX — TEAS Plus table extractor (primary for new format)
    #
    # TEAS Plus tables have THREE types of rows:
    #   1. (label=section_name, value=None) → section header
    #   2. (label=long_text,    value=None) → single-cell content row
    #      - "International Class 020"     → extract class number
    #      - "Filing basis: Section 1(b)"  → extract filing basis
    #      - Long goods text (>100 chars)  → identification text
    #   3. (label=field_label,  value=data) → normal two-column row
    # =========================================================================
    def _extract_teas_plus_tables(self, path: str) -> dict:
        """Primary table extractor for TEAS Plus format PDFs."""
        out = {}
        current_section = None
        # TEAS Plus redacts with pure asterisk strings like '*******'
        _TEAS_REDACTED_RE = re.compile(r'^\*+$')
        _TEAS_SKIP_VALUES = {'none specified', '', 'n/a', 'not provided'}

        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                try:
                    found_tables = page.find_tables()
                except Exception:
                    continue
                for tbl in found_tables:
                    rows = tbl.extract()
                    if not rows:
                        continue
                    for row in rows:
                        if not row:
                            continue
                        cell0 = row[0] if len(row) > 0 else None
                        cell1 = row[1] if len(row) > 1 else None
                        if cell0 is None:
                            continue

                        label_str = ' '.join(str(cell0).split()).strip()
                        label_key = label_str.lower().lstrip('*').strip()
                        label_key = re.sub(r'\s*\(.*?\)\s*$', '', label_key).strip()
                        value_str = ' '.join(str(cell1).split()).strip() if cell1 else ''

                        # ── TYPE 1 & 2: Single-cell row (value is empty) ───────
                        if not value_str:
                            # Section header?
                            if label_key in _TEAS_SECTION_HEADERS:
                                current_section = _TEAS_SECTION_HEADERS[label_key]
                                continue
                            # "International Class NNN" → class number + enter goods section
                            m_cls = re.match(r'international class\s+(\d{1,3})$', label_key)
                            if m_cls:
                                current_section = 'goods'
                                cls_val = int(m_cls.group(1))
                                if 1 <= cls_val <= 45 and 'classes' not in out:
                                    out['classes'] = [cls_val]
                                    out['class_strings'] = [str(cls_val).zfill(3)]
                                continue
                            # "Filing basis: Section 1(b)" single-cell row
                            if 'filing basis' in label_key:
                                m_basis = re.search(
                                    r'(1\(a\)|1\(b\)|44\(d\)|44\(e\)|66\(a\))',
                                    label_str, re.I
                                )
                                if m_basis and 'filing_basis' not in out:
                                    out['filing_basis'] = m_basis.group(1)
                                continue
                            # Long text in goods section → identification text
                            if current_section == 'goods' and len(label_str) > 100:
                                if 'identification_text' not in out:
                                    out['identification_text'] = label_str
                                continue
                            continue

                        # ── TYPE 3: Two-cell row ───────────────────────────────
                        if _TEAS_REDACTED_RE.match(value_str):
                            continue
                        if value_str.lower() in _TEAS_SKIP_VALUES:
                            continue

                        canonical = _TEAS_CONTEXT_LABEL_MAP.get((current_section, label_key))
                        if canonical is None:
                            canonical = LABEL_MAP.get(label_key)
                        if canonical is None or canonical in out:
                            continue

                        out[canonical] = self._coerce_field(canonical, value_str)

        return out

    
    
    def safe_parse_date(self, txt):
        try:
            txt = re.sub(r'\bET\b', '', txt)
            txt = re.sub(r'\bat\b', '', txt)
            d = dateparser.parse(txt, fuzzy=True)
            if d:
                return d.date().isoformat()
        except Exception:
            pass
        return None

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
            except Exception:
                errors.append("class_number_invalid")
        else:
            warnings.append("no_classes_found")

        ident = extracted.get("identification_text")
        if not ident:
            errors.append("identification_missing")
        elif IDENT_PLACEHOLDER.search(ident):
            errors.append("identification_placeholder")

        return errors, warnings

    def process_pdf(self, path):
        plumb  = self.extract_with_pdfplumber(path)
        text   = plumb["raw_text"]
        tables = self.extract_tables(path)   # unchanged — consumed by backends
        extracted = {}

        # ── Detect which USPTO form layout this PDF uses ──────────────────────
        fmt = self._detect_pdf_format(text)

        if fmt == 'teas_plus':
            # ── TEAS Plus pipeline ────────────────────────────────────────────
            # Step 1: Summary block (inline key:value at top of page 1)
            summary_fields = self._extract_teas_plus_summary(text)
            extracted.update(summary_fields)

            # Step 2: Two-column tables (owner, attorney, fee, signature)
            table_fields = self._extract_teas_plus_tables(path)

            # 'classes' comes back as plain list from TEAS Plus extractor
            if 'classes' in table_fields and 'classes' not in extracted:
                extracted['classes']       = table_fields.pop('classes')
                extracted['class_strings'] = table_fields.pop('class_strings', [])
            else:
                table_fields.pop('classes', None)
                table_fields.pop('class_strings', None)

            for key, val in table_fields.items():
                if key not in extracted or not extracted[key]:
                    extracted[key] = val

            # Step 3: Filing basis gap-fill from raw text
            if not extracted.get('filing_basis'):
                m = BASIS_RE.search(text)
                if m:
                    extracted['filing_basis'] = m.group(2)
                else:
                    m = SECTION_RE.search(text)
                    if m:
                        extracted['filing_basis'] = m.group(1)

            # Step 4: Class gap-fill from raw text
            if not extracted.get('classes'):
                m = CLASS_RE.search(text)
                if m:
                    nums = re.findall(r'\b\d{1,3}\b', m.group(2))
                    seen, il, sl = set(), [], []
                    for n in nums:
                        v = int(n)
                        if 1 <= v <= 45 and v not in seen:
                            il.append(v); sl.append(str(v).zfill(3)); seen.add(v)
                    if il:
                        extracted['classes']       = il
                        extracted['class_strings'] = sl

        else:
            # ── PTO-1478 pipeline (original format) ───────────────────────────
            # Step 1: Table-first extraction (primary)
            table_fields = self.extract_fields_from_tables(path)

            classes_payload = table_fields.pop('classes', None)
            if classes_payload:
                extracted['classes']       = classes_payload['int']
                extracted['class_strings'] = classes_payload['str']

            extracted.update(table_fields)

            # Step 2: Regex fallback — fill only fields still missing
            regex_fields  = self.find_fields_in_text(text)
            regex_classes = regex_fields.pop('classes', None)
            if regex_classes and 'classes' not in extracted:
                extracted['classes']       = regex_classes
                extracted['class_strings'] = regex_fields.pop('class_strings', [])

            for key, val in regex_fields.items():
                if key not in extracted or not extracted[key]:
                    extracted[key] = val

            # Step 3: Identification block gap-fill
            if not extracted.get('identification_text'):
                ident = self.extract_identification_block(text)
                if ident:
                    extracted['identification_text'] = ident

        # ── Common tail (both formats) ────────────────────────────────────────
        extracted["tables"]           = tables
        extracted["raw_text_snippet"] = text[:3000]

        errors, warnings = self.validate(extracted)
        extracted["errors"]     = errors
        extracted["warnings"]   = warnings
        extracted["confidence"] = self.simple_confidence(extracted)

        return extracted

    def simple_confidence(self, extracted):
        score = 0.5
        for k in ["serial_number", "filing_date", "filing_basis",
                  "applicant_name", "identification_text"]:
            if extracted.get(k):
                score += 0.1
        score -= 0.2 * len(extracted.get("errors", []))
        return max(0.0, min(0.99, round(score, 2)))


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Trademark PDF Extractor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

processor = PDFProcessor()


# ─────────────────────────────────────────────────────────────────────────────
# Backend 1 — Classification (divya-nshu99-pk.hf.space)
# Receives: { class_number, identification }
# Returns:  classification result dict
# ─────────────────────────────────────────────────────────────────────────────

def forward_json_to_server(data: dict) -> dict:
    """
    Sends class_number + identification text to the HF classification backend.
    Called concurrently with forward_mark_text_to_server.
    Result is displayed FIRST in the frontend chatbox (Section 4).
    """
    TARGET_API = "https://divya-nshu99-pk.hf.space/classify"

    try:
        response = requests.post(TARGET_API, json=data, timeout=30)
        response.raise_for_status()
        return response.json()

    except requests.exceptions.RequestException as e:
        return {"status": "failed", "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Backend 2 — Mark Conflict (divya-nshu99-nevergiveup.hf.space)
# Receives: { mark_text, filing_status: "active" }
# Returns:  list of conflict records sorted by composite score
# ─────────────────────────────────────────────────────────────────────────────

def forward_mark_text_to_server(mark_text: str) -> list:
    """
    Sends extracted mark_text to the HF 'nevergiveup' Space REST API (POST /search).

    filing_status is always "active" — auto-assigned here, not sent from frontend.

    Called concurrently with forward_json_to_server.
    Result is displayed SECOND in the frontend chatbox (Section 7),
    AFTER the classification result — even though both run at the same time.

    Returns list of:
    [
      {
        "applied_mark":     str,
        "conflicting_mark": str,
        "serial":           str,
        "score":            float,   # 0.0 – 1.0
        "risk":             str,     # "HIGH" | "MEDIUM" | "LOW"
        "explanation":      str,
        "visual_score":     float,
        "phonetic_score":   float,
        "meaning_score":    float,
      }, ...
    ]
    """
    MARK_API = "https://divya-nshu99-nevergiveup.hf.space/search"

    try:
        response = requests.post(
            MARK_API,
            json={
                "mark_text":     mark_text,
                "filing_status": "active"    # always active for PDF conflict check
            },
            timeout=60    # HF cold-start can take up to ~30s — give generous timeout
        )

        response.raise_for_status()

        data = response.json()

        # HF /search always returns a list — safety check
        if isinstance(data, list):
            return data

        return []

    except requests.exceptions.RequestException as e:
        return [{"status": "failed", "error": str(e)}]


# ─────────────────────────────────────────────────────────────────────────────
# Async wrappers
# (run_in_executor lets blocking requests.post run in a thread
#  without blocking the async FastAPI event loop)
# ─────────────────────────────────────────────────────────────────────────────

async def call_classification_backend(payload: dict) -> dict:
    """Async wrapper for forward_json_to_server."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, forward_json_to_server, payload)


async def call_mark_backend(mark_text: str) -> list:
    """Async wrapper for forward_mark_text_to_server."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, forward_mark_text_to_server, mark_text)

def analyze_trademark(
    mark: str,
    goods: str,
    goods_class: Optional[str] = None,
    backend_url: str = "https://divya-nshu99-disk.hf.space/analyze"
) -> Dict[str, Any]:
    payload = {
        "mark": mark,
        "goods": goods,
        "goods_class": goods_class
    }
    timeout = 60
    try:
        response = requests.post(backend_url, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        expected_keys = {"descriptive_score", "generic_score", "reasons", "explanation", "details"}
        if not expected_keys.issubset(data.keys()):
            raise ValueError("Unexpected response format from backend")
        return data
    except requests.exceptions.Timeout:
        raise TimeoutError("Backend request timed out. The model may still be loading.")
    except requests.exceptions.RequestException as e:
        error_detail = ""
        if e.response is not None:
            try:
                error_detail = e.response.json().get("detail", e.response.text)
            except:
                error_detail = e.response.text
        raise requests.RequestException(f"Backend request failed: {e}. Detail: {error_detail}")

async def call_analyze_backend(mark: str, goods: str, goods_class: Optional[str]) -> dict:
    """Async wrapper for analyze_trademark."""
    loop = asyncio.get_running_loop()
    fn = functools.partial(analyze_trademark, mark=mark, goods=goods, goods_class=goods_class)
    return await loop.run_in_executor(None, fn)
# ─────────────────────────────────────────────────────────────────────────────
# /extract endpoint
# ─────────────────────────────────────────────────────────────────────────────

def _safe_json(data: dict) -> JSONResponse:
    """Serializes via json.dumps first so non-standard types never crash FastAPI."""
    return JSONResponse(json.loads(json.dumps(data, ensure_ascii=False, default=str)))



@app.post("/extract")
async def extract_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...)
):
    if not file.filename.lower().endswith(".pdf"):
        return JSONResponse({"error": "only pdf allowed"}, status_code=400)

    contents = await file.read()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tf:
        tf.write(contents)
        tmp_path = tf.name

    try:
        # ── Step 1: Extract fields from PDF ──────────────────────────────────
        result       = processor.process_pdf(tmp_path)
        mark_text    = result.get("mark_text", "")
        classes      = result.get("classes", [])
        class_number = classes[0] if classes else None

        print(f"EXTRACTED MARK: '{mark_text}'")

        # ── Step 2: Build payloads ────────────────────────────────────────────
        identification_clean = result.get("identification_text", "").replace("\n", " ")

        classification_payload = {
            "class_number":   class_number,
            "identification": identification_clean
        }

        # ── Step 3: Fire ALL THREE backend calls at the same time ────────────
        classification_task = asyncio.create_task(
            call_classification_backend(classification_payload)
        )

        has_mark = bool(mark_text.strip())

        if has_mark:
            goods_class_str = str(class_number).zfill(3) if class_number is not None else None
            mark_task    = asyncio.create_task(call_mark_backend(mark_text))
            analyze_task = asyncio.create_task(
                call_analyze_backend(
                    mark        = mark_text,
                    goods       = identification_clean,
                    goods_class = goods_class_str
                )
            )
        else:
            mark_task    = None
            analyze_task = None

        # ── Step 4: Collect results IN SEQUENCE (1st → 2nd → 3rd) ───────────

        # 1st: classification result
        classification = await classification_task
        result["classification_result"] = classification

        # 2nd: mark conflict result
        if mark_task is not None:
            mark_response = await mark_task
            if not isinstance(mark_response, list):
                mark_response = []
        else:
            mark_response = []
            print("MARK TEXT empty — skipping conflict analysis")
        result["mark_analysis"] = mark_response

        # 3rd: trademark analysis result
        if analyze_task is not None:
            try:
                result["trademark_analysis"] = await analyze_task
            except Exception as e:
                result["trademark_analysis"] = {"status": "failed", "error": str(e)}
        else:
            result["trademark_analysis"] = {}

        # ── Compute serial ONCE — reused by all three file saves ─────────────
        serial = result.get("serial_number", f"tm_{int(time.time())}")

        # ── Save trademark_analysis to its own file ───────────────────────────
        tm_analysis_path = os.path.join(TM_OUTPUT_DIR, f"{serial}_trademark_analysis.json")
        with open(tm_analysis_path, "w", encoding="utf-8") as f:
            json.dump(result.get("trademark_analysis", {}), f, indent=2, ensure_ascii=False)

        # ── Step 5: Save mark analysis to its own file ────────────────────────
        mark_output_path = os.path.join(TM_OUTPUT_DIR, f"{serial}_mark_analysis.json")
        with open(mark_output_path, "w", encoding="utf-8") as f:
            json.dump(mark_response, f, indent=2, ensure_ascii=False)

        # ── Step 6: Save full result JSON ─────────────────────────────────────
        output_path = os.path.join(TM_OUTPUT_DIR, f"{serial}.json")
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

    return _safe_json(result)
# ─────────────────────────────────────────────────────────────────────────────
# Static routes
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def serve_frontend():
    return FileResponse(os.path.join(os.getcwd(), "taraai.html"))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/version")
def version():
    return {"service": "trademark_pdf_extractor", "version": "1.0"}




