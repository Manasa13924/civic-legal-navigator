"""RAG pipeline for the Civic & Legal Navigator.

Flow: PDF -> pages -> chunks -> embeddings -> ChromaDB -> retrieve -> grounded answer.
The output JSON has the same shape as the old agent.py, so the UI logic is unchanged.
"""
import hashlib
import io
import json
import os
import re
import time
import uuid
from urllib.parse import urlparse

import chromadb
import requests
from google.genai import types
from pypdf import PdfReader, PdfWriter

EMBED_MODEL = "gemini-embedding-001"   # verify in Google's docs, names change
LLM_MODEL = "gemini-3.8-flash"         # main model; change here if Google renames it
CACHE_DIR = ".cache"                   # OCR results are saved here so retries are fast


def _retry(fn, tries=6):
    """Retry temporary Google errors (503 overloaded, 429 rate limit) with waiting."""
    delay = 3
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:
            text = str(e)
            if "PerDay" in text:
                raise RuntimeError(
                    "Daily free quota for this Gemini model is used up. It resets once a day "
                    "(around 12:30 PM IST). You can also change LLM_MODEL at the top of rag.py "
                    "to another model, which has its own quota."
                ) from e
            temporary = getattr(e, "code", None) in (429, 500, 503, 504) or any(
                w in text for w in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED")
            )
            if not temporary or attempt == tries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)


# ---------- Model helper: automatic fallback ----------
# If the main model is busy (503), out of quota (429) or unknown (404), try these next.
FALLBACK_MODELS = ["gemini-3.6-flash", "gemini-2.5-flash-lite"]


def _generate(client, **kwargs):
    """generate_content that automatically falls back to other models."""
    last_error = None
    for model in [LLM_MODEL] + [m for m in FALLBACK_MODELS if m != LLM_MODEL]:
        try:
            response = _retry(
                lambda: client.models.generate_content(model=model, **kwargs), tries=3
            )
            print(f"RAG: answered with model {model}")
            return response
        except Exception as e:
            last_error = e
            text = str(e)
            can_skip = isinstance(e, RuntimeError) or any(
                w in text for w in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                                    "404", "NOT_FOUND")
            )
            print(f"RAG: model {model} failed, trying next. ({text[:90]})")
            if not can_skip:
                raise
    raise last_error


# ---------- 1. LOAD: text per page (local OCR for scanned PDFs) ----------
_OCR_ENGINE = None


def _local_ocr_page(pdf_bytes, page_index):
    """Read one scanned page ON THIS COMPUTER (free, uses no Gemini quota)."""
    global _OCR_ENGINE
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    if _OCR_ENGINE is None:
        _OCR_ENGINE = RapidOCR()
    page = PdfReader(io.BytesIO(pdf_bytes)).pages[page_index]
    image = page.images[0].image.convert("RGB")
    result, _ = _OCR_ENGINE(np.array(image))
    return "\n".join(line[1] for line in (result or []))


def _ocr_page(client, pdf_bytes, page_index):
    """Send ONE page to Gemini and get its text back (used for scanned PDFs)."""
    writer = PdfWriter()
    writer.add_page(PdfReader(io.BytesIO(pdf_bytes)).pages[page_index])
    buf = io.BytesIO()
    writer.write(buf)
    response = _generate(
        client,
        contents=[
            types.Part.from_bytes(data=buf.getvalue(), mime_type="application/pdf"),
            "Transcribe all readable text on this page exactly as written. "
            "Return plain text only, no commentary.",
        ],
    )
    return (response.text or "").strip()


def extract_pages(pdf_bytes, client, progress=None):
    """Return [(pdf_page_number, text), ...]. Page numbers start at 1."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = [(i + 1, (p.extract_text() or "").strip()) for i, p in enumerate(reader.pages)]
    total_chars = sum(len(t) for _, t in pages)
    if total_chars >= 100 * len(pages):
        return pages  # normal text PDF, no OCR needed

    # Scanned PDF: OCR each page once and save it, so a failed run can resume.
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = hashlib.sha256(pdf_bytes).hexdigest()[:16]
    path = os.path.join(CACHE_DIR, f"ocr_{key}.json")
    cache = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    result = []
    for i in range(len(reader.pages)):
        k = str(i + 1)
        if k not in cache:
            try:
                cache[k] = _local_ocr_page(pdf_bytes, i)      # free, local
            except Exception:
                cache[k] = _ocr_page(client, pdf_bytes, i)    # fallback: Gemini
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
        result.append((i + 1, cache[k]))
        if progress:
            progress(i + 1, len(reader.pages))
    return result


# ---------- 2. CHUNK ----------
def chunk_pages(pages, size=250, overlap=50):
    """Split each page into overlapping word chunks and keep the page number."""
    chunks = []
    step = size - overlap
    for page, text in pages:
        words = text.split()
        for start in range(0, len(words), step):
            chunks.append({"page": page, "text": " ".join(words[start:start + size])})
            if start + size >= len(words):
                break
    return chunks


# ---------- Web addresses written inside the document ----------
URL_RE = re.compile(r"(?:https?://|www\.)[^\s\"'<>\[\]()]+", re.I)


# Government addresses written WITHOUT https://, e.g. "ssp.karnataka.gov.in"
BARE_RE = re.compile(
    r"(?<![@\w./-])(?:[a-z0-9-]+\.)+(?:gov\.in|nic\.in|edu\.in|ac\.in)"
    r"(?:/[^\s\"'<>\[\]()]*)?(?![\w@-])", re.I)


def extract_link_annotations(pdf_bytes):
    """Real clickable links inside the PDF, e.g. a 'Click here to apply' button whose
    address is NOT written in the text."""
    urls = []
    try:
        for page in PdfReader(io.BytesIO(pdf_bytes)).pages:
            annots = page.get("/Annots")
            if not annots:
                continue
            for annot in annots.get_object():
                action = annot.get_object().get("/A")
                if action is None:
                    continue
                uri = action.get_object().get("/URI")
                if uri and str(uri).lower().startswith(("http://", "https://")):
                    urls.append(str(uri).strip())
    except Exception:
        pass  # a damaged link table must never stop the app
    return urls


def extract_urls(pages, extra=()):
    """Collect real web addresses printed in the PDF (so we never invent a link)."""
    urls = []
    candidates = []
    for _, text in pages:
        candidates += URL_RE.findall(text) + BARE_RE.findall(text)
    candidates += list(extra)
    for u in candidates:
        u = u.rstrip(".,;:!?")
        if not u.lower().startswith("http"):
            u = "https://" + u
        if u.rstrip("/").lower() not in [x.rstrip("/").lower() for x in urls]:
            urls.append(u)
    return urls


# Verified official portals, used ONLY when the document itself gives no usable link.
# Each entry: (name, words that must ALL appear in the document, official address)
KNOWN_PORTALS = [
    ("State Scholarship Portal (Karnataka)",
     [r"\bssp\b|state scholarship portal", r"karnataka"], "https://ssp.karnataka.gov.in"),
    ("National Scholarship Portal",
     [r"national scholarship portal|\bnsp\b"], "https://scholarships.gov.in"),
    ("PM-KISAN", [r"pm[- ]?kisan"], "https://pmkisan.gov.in"),
    ("Soil Health Card", [r"soil health card"], "https://soilhealth.dac.gov.in"),
]


# ---------- Look up the official site of ANY scheme (Google Search through Gemini) ----------
def _is_gov_url(url):
    """Accept only official Indian government domains."""
    host = (urlparse(url).hostname or "").lower()
    return host.endswith(".gov.in") or host.endswith(".nic.in")


def _url_looks_alive(url):
    """Reject only clear failures (site does not exist / page not found)."""
    try:
        r = requests.get(url, timeout=6, stream=True, headers={"User-Agent": "Mozilla/5.0"})
        r.close()
        return r.status_code not in (404, 410)
    except requests.exceptions.SSLError:
        return True            # many gov sites have certificate quirks, but they exist
    except requests.exceptions.ConnectionError:
        return False           # name does not exist or refused: likely a made-up address
    except Exception:
        return True            # slow server or timeout: we cannot tell, so do not reject


def find_official_apply_link(client, scheme_name, state):
    """Ask Gemini (with Google Search) for the scheme's official website, then verify it."""
    if not scheme_name:
        return ""
    prompt = (
        f"Find the official government website where a citizen can apply or register for "
        f"this scheme: '{scheme_name}' (state: {state}). Reply with ONLY the web address on "
        f"one line, no explanation. It must be an official government website ending in "
        f".gov.in or .nic.in. If you cannot find it, reply NONE."
    )
    try:
        response = _generate(
            client, contents=prompt,
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
        )
    except Exception as e:
        print(f"RAG: official-site lookup failed ({str(e)[:80]})")
        return ""
    text = response.text or ""
    for u in URL_RE.findall(text) + BARE_RE.findall(text):
        u = u.rstrip(".,;:!?*_`")
        if not u.lower().startswith("http"):
            u = "https://" + u
        if _is_gov_url(u) and _url_looks_alive(u):
            print(f"RAG: official site found by search: {u}")
            return u
    return ""


def find_known_portal(doc_text):
    for _name, needs, url in KNOWN_PORTALS:
        if all(re.search(p, doc_text, re.I) for p in needs):
            return url
    return ""


# ---------- 3. EMBED + STORE ----------
def embed(client, texts, task_type):
    vectors = []
    for i in range(0, len(texts), 50):
        batch = texts[i:i + 50]
        result = _retry(lambda: client.models.embed_content(
            model=EMBED_MODEL,
            contents=batch,
            config=types.EmbedContentConfig(task_type=task_type),
        ))
        vectors.extend(e.values for e in result.embeddings)
    return vectors


def build_index(pdf_bytes, client, progress=None):
    pages = extract_pages(pdf_bytes, client, progress)
    chunks = chunk_pages(pages)
    if not chunks:
        raise ValueError("No text could be read from this PDF.")
    collection = chromadb.Client().create_collection(
        name=f"doc_{uuid.uuid4().hex[:10]}", metadata={"hnsw:space": "cosine"}
    )
    collection.add(
        ids=[str(i) for i in range(len(chunks))],
        documents=[c["text"] for c in chunks],
        metadatas=[{"page": c["page"]} for c in chunks],
        embeddings=embed(client, [c["text"] for c in chunks], "RETRIEVAL_DOCUMENT"),
    )
    return {"collection": collection, "n_chunks": len(chunks), "n_pages": len(pages),
            "urls": extract_urls(pages, extract_link_annotations(pdf_bytes)),
            "doc_text": " ".join(t for _, t in pages)}


# ---------- Follow-up questions (when the document needs facts we do not have) ----------
def _clean_questions(raw):
    """Keep at most 4 well-formed questions, whatever the model returned."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("question", "")).strip()
        if not text:
            continue
        qtype = item.get("type") if item.get("type") in ("choice", "yes_no", "number", "text") else "text"
        options = [str(o).strip() for o in (item.get("options") or []) if str(o).strip()][:6]
        if qtype == "choice" and len(options) < 2:
            qtype = "text"
        out.append({"id": f"q{len(out) + 1}", "question": text, "type": qtype, "options": options})
        if len(out) == 4:
            break
    return out


# ---------- 4. RETRIEVE ----------
def retrieve(index, client, user, extra=None, per_query=4, max_chunks=8):
    queries = [
        "Who is eligible for this scheme? Beneficiary and eligibility criteria",
        f"Eligibility conditions for a {user['occupation']} in {user['state']}",
        "Income limit, age limit, land holding or documents required",
        "How to apply, application process, official portal or website",
    ]
    if extra:
        answers_text = "; ".join(f"{p['q']} {p['a']}" for p in extra)[:300]
        queries.append(f"Eligibility rules, income limit and conditions for: {answers_text}")
    found = {}
    for query, vec in zip(queries, embed(client, queries, "RETRIEVAL_QUERY")):
        res = index["collection"].query(
            query_embeddings=[vec], n_results=min(per_query, index["n_chunks"])
        )
        for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
            key = (meta["page"], doc[:80])
            if key not in found or dist < found[key]["distance"]:
                found[key] = {"page": meta["page"], "text": doc, "distance": dist}
    return sorted(found.values(), key=lambda c: c["distance"])[:max_chunks]


# ---------- 5. GENERATE (grounded in retrieved chunks only) ----------
def evaluate_with_rag(index, client, user, language, extra=None, final_round=False):
    chunks = retrieve(index, client, user, extra)
    print(f"RAG: retrieved {len(chunks)} chunks from pages {sorted({c['page'] for c in chunks})}")
    urls = index.get("urls", [])
    urls_text = "\n".join(urls) if urls else "(none found)"
    context = "\n\n".join(f"[PDF page {c['page']}]\n{c['text']}" for c in chunks)
    user_info = (
        f"Name: {user['name']}, Age: {user['age']}, Occupation: {user['occupation']}, "
        f"State: {user['state']}, Annual Income: Rs {user['annual_income']}"
    )
    extra_text = "\n".join(f"- {p['q']}: {p['a']}" for p in (extra or [])) or "(none)"
    if final_round:
        round_rule = (
            '7. This is the LAST round. Do NOT use MORE_INFO_NEEDED and do not ask questions. '
            'Decide ELIGIBLE or INELIGIBLE using the best reading of the excerpts and the answers, '
            'and say in simple words what the decision depends on. "questions" must be [].'
        )
    else:
        round_rule = (
            f'7. If the status is MORE_INFO_NEEDED, ask up to 4 short questions in {language} about '
            'facts that decide eligibility in the excerpts and that are NOT already in the profile '
            'or the extra answers. Use type "choice" (with 2 to 6 options) when the document lists '
            'categories such as caste category or course type, "yes_no", "number", or "text". '
            'If the status is not MORE_INFO_NEEDED, "questions" must be [].'
        )
    prompt = f"""
You are a helpful assistant for rural citizens.
Decide eligibility using ONLY the document excerpts below.

User Profile:
{user_info}

Extra answers from the person (trust these):
{extra_text}

Document excerpts:
{context}

Rules:
1. Use extremely simple words a common person can understand.
2. Use only rules that appear in the excerpts. Do NOT invent rules. For example, do not
   assume a person is a small or marginal farmer from income alone.
3. If the person belongs to the group the scheme is made for (for example a farmer for a
   scheme for farmers) and nothing in the excerpts excludes them, use ELIGIBLE. Mention any
   priority group or extra condition as a note, for example "small and marginal farmers get
   priority, so keep your land size ready". Use INELIGIBLE if the scheme is clearly made for
   a different group than this person (for example a scholarship for students and the person
   is a farmer who is not studying), or if the person clearly breaks a rule such as an age or
   income limit. Judge the person in the profile, not their family: if the scheme is for
   students and the profile occupation is not student, use INELIGIBLE, and add a short note in
   the report if the document has schemes for their children or family. Do NOT use
   MORE_INFO_NEEDED just because the person does not match. Use MORE_INFO_NEEDED only if the
   excerpts make eligibility depend on something we do not know, and say what is missing.
4. After each rule, cite the page like (page 3). Use the "PDF page" numbers shown above.
5. Write report_markdown entirely in {language}, as short bullet points:
   **Why you qualify or don't**, **Key rules in simple words**, **Next simple steps**.
6. Web addresses found in the document:
{urls_text}
   For apply_url choose the ONE address where the person can apply or register, copied
   exactly from this list. If none fits, use an empty string. Never make up an address.
{round_rule}

Return JSON only:
{{
  "scheme_name": "Official Scheme Name",
  "apply_url": "one address from the list above, or empty string",
  "status": "ELIGIBLE" or "INELIGIBLE" or "MORE_INFO_NEEDED",
  "report_markdown": "Simple breakdown in {language} with page citations.",
  "questions": [{{"id": "q1", "question": "text in {language}", "type": "choice", "options": ["A", "B"]}}]
}}
"""
    response = _generate(
        client,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    result = json.loads(text)
    needs_info = str(result.get("status", "")).upper() == "MORE_INFO_NEEDED"
    result["questions"] = _clean_questions(result.get("questions")) if (needs_info and not final_round) else []
    # Safety check: accept the link only if it really appears in the document
    chosen = (result.get("apply_url") or "").strip().rstrip("/").lower()
    result["apply_url"] = next((u for u in urls if u.rstrip("/").lower() == chosen), "")
    result["apply_source"] = "document" if result["apply_url"] else ""
    if not result["apply_url"]:
        known = find_known_portal(index.get("doc_text", ""))
        if known:
            result["apply_url"], result["apply_source"] = known, "known"
    # Still no link and the person is eligible: look up the official site of this scheme
    if not result["apply_url"] and str(result.get("status", "")).upper() in ("ELIGIBLE", "MORE_INFO_NEEDED"):
        web = find_official_apply_link(client, result.get("scheme_name", ""), user["state"])
        if web:
            result["apply_url"], result["apply_source"] = web, "web"
    result["doc_urls"] = urls
    result["sources"] = chunks  # shown to the user so answers can be checked
    return result