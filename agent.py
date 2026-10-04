import os
import tempfile
import sqlite3
import json
import html
from urllib.parse import quote_plus

import bcrypt
import streamlit as st
from dotenv import load_dotenv
from google import genai
from rag import build_index, evaluate_with_rag

load_dotenv()

MAX_FOLLOWUPS = 2   # how many rounds of questions we ask before deciding


def search_link(scheme_name):
    """Last resort: a web search for the scheme (only when no official link is known)."""
    return "https://www.google.com/search?q=" + quote_plus(f"{scheme_name} official website apply gov.in")

st.set_page_config(
    page_title="Civic & Legal Navigator",
    page_icon="📜",
    layout="centered"
)

st.markdown("""
<style>
.hero{background:linear-gradient(135deg,#1F6FEB,#0B3D91);color:#fff;padding:22px 26px;
      border-radius:16px;margin:4px 0 18px}
.hero h1{margin:0;font-size:1.8rem;color:#fff;padding:0}
.hero p{margin:6px 0 0;opacity:.92;color:#fff}
.card{border-radius:16px;padding:18px 24px;margin:12px 0 8px;border:2px solid;color:#1B2430}
.card.ok{background:#E8F7EE;border-color:#34A853}
.card.warn{background:#FFF8E1;border-color:#F9A825}
.card.bad{background:#FDECEA;border-color:#D93025}
.card h2{margin:0;font-size:1.6rem;padding:0;color:#1B2430}
.card .scheme{font-size:.95rem;opacity:.75;margin-bottom:4px}
div.stButton>button,div.stLinkButton>a,div[data-testid="stFormSubmitButton"]>button{
    border-radius:12px;font-weight:600;min-height:2.8rem}
</style>
""", unsafe_allow_html=True)
# 1. Multi-Language Translations Dictionary
TRANSLATIONS = {
    "English": {
        "title": "📜 Civic & Legal Navigator",
        "select_lang": "🌐 Choose Language / भाषा चुनें / ಭಾಷೆಯನ್ನು ಆಯ್ಕೆಮಾಡಿ",
        "login_portal": "🔑 Citizen Portal Access",
        "sign_in": "Sign In",
        "create_account": "Create New Account",
        "username": "Username",
        "password": "Password",
        "full_name": "Full Name",
        "age": "Age",
        "occupation": "Occupation",
        "state": "State",
        "annual_income": "Annual Income (₹)",
        "register_profile": "Register Profile",
        "active_profile": "👤 Active Profile",
        "save_changes": "Save Profile Changes",
        "logout": "Log Out",
        "upload_section": "📤 Upload Policy / Scheme Document",
        "upload_label": "Select scheme guidelines PDF to evaluate:",
        "eval_button": "Check Eligibility",
        "evaluating": "Checking document with Gemini AI...",
        "eval_complete": "Check Completed!",
        "report_header": "📋 Simple Eligibility Result",
        "app_portal_header": "🚀 Scheme Application Form (Auto-Filled)",
        "submit_app": "Submit Application",
        "redirect_msg": "Application saved! Redirecting to official portal...",
        "open_portal": "🌐 Go to Official Government Scheme Portal",
        "more_info_warn": "🟡 More Information Needed: Please provide extra documents or details listed above.",
        "ineligible_err": "🔴 Not Eligible: Based on your profile details, you do not qualify for this scheme.",
        "badge_eligible": "🟢 ELIGIBLE (YOU CAN APPLY)",
        "badge_ineligible": "🔴 NOT ELIGIBLE",
        "badge_more_info": "🟡 MORE INFO NEEDED",
        "apply_now": "🔗 Apply on the official website",
        "check_site": "🔗 Check details on the official website",
        "followup_intro": "To decide, please answer these questions:",
        "followup_submit": "Check my eligibility again",
        "yes": "Yes",
        "no": "No",
        "your_answers": "📝 Your answers",
        "tagline": "Check government scheme eligibility in your own language.",
        "how_decided": "🔍 How we decided",
        "my_profile": "✏️ Edit my profile",
        "details": "📄 Details in simple words",
        "reading_page": "Reading page {n} of {total}...",
        "apply_hint_doc": "This link was found inside the scheme document you uploaded.",
        "apply_hint_known": "This is the official website of this scheme.",
        "apply_hint_web": "Found by searching official government sites. Check that the address ends with .gov.in or .nic.in before entering any details.",
        "apply_hint_generic": "No official link was found, so this searches the web for: {scheme}. Open only websites ending in .gov.in or .nic.in."
    },
    "Hindi": {
        "title": "📜 नागरिक एवं कानूनी नेविगेटर",
        "select_lang": "🌐 Choose Language / भाषा चुनें / ಭಾಷೆಯನ್ನು ಆಯ್ಕೆಮಾಡಿ",
        "login_portal": "🔑 नागरिक पोर्टल प्रवेश",
        "sign_in": "साइन इन करें",
        "create_account": "नया खाता बनाएं",
        "username": "उपयोगकर्ता का नाम (Username)",
        "password": "पासवर्ड",
        "full_name": "पूरा नाम",
        "age": "उम्र",
        "occupation": "व्यवसाय",
        "state": "राज्य",
        "annual_income": "वार्षिक आय (₹)",
        "register_profile": "प्रोफाइल रजिस्टर करें",
        "active_profile": "👤 सक्रिय प्रोफाइल",
        "save_changes": "प्रोफाइल बदलाव सेव करें",
        "logout": "लॉग आउट",
        "upload_section": "📤 नीति / योजना दस्तावेज़ अपलोड करें",
        "upload_label": "पात्रता जांच के लिए योजना गाइड PDF चुनें:",
        "eval_button": "पात्रता जांचें",
        "evaluating": "Gemini AI के साथ दस्तावेज़ की जांच की जा रही है...",
        "eval_complete": "जांच पूरी हुई!",
        "report_header": "📋 सरल पात्रता परिणाम",
        "app_portal_header": "🚀 योजना आवेदन फॉर्म (ऑटो-फिल)",
        "submit_app": "आवेदन जमा करें",
        "redirect_msg": "आवेदन सेव हुआ! आधिकारिक पोर्टल पर भेजा जा रहा है...",
        "open_portal": "🌐 आधिकारिक सरकारी पोर्टल पर जाएं",
        "more_info_warn": "🟡 अधिक जानकारी चाहिए: कृपया ऊपर दिए गए दस्तावेज़ या विवरण प्रदान करें।",
        "ineligible_err": "🔴 पात्र नहीं हैं: आपकी प्रोफाइल के अनुसार आप इस योजना के योग्य नहीं हैं।",
        "badge_eligible": "🟢 पात्र हैं (आप आवेदन कर सकते हैं)",
        "badge_ineligible": "🔴 पात्र नहीं हैं",
        "badge_more_info": "🟡 अधिक जानकारी चाहिए",
        "apply_now": "🔗 आधिकारिक वेबसाइट पर आवेदन करें",
        "check_site": "🔗 आधिकारिक वेबसाइट पर विवरण देखें",
        "followup_intro": "निर्णय के लिए कृपया इन सवालों के जवाब दें:",
        "followup_submit": "मेरी पात्रता फिर से जाँचें",
        "yes": "हाँ",
        "no": "नहीं",
        "your_answers": "📝 आपके उत्तर",
        "tagline": "अपनी भाषा में सरकारी योजनाओं की पात्रता जाँचें।",
        "how_decided": "🔍 हमने कैसे तय किया",
        "my_profile": "✏️ मेरी प्रोफाइल बदलें",
        "details": "📄 आसान शब्दों में विवरण",
        "reading_page": "पेज {n} / {total} पढ़ा जा रहा है...",
        "apply_hint_doc": "यह लिंक आपके अपलोड किए गए योजना दस्तावेज़ में मिला है।",
        "apply_hint_known": "यह इस योजना की आधिकारिक वेबसाइट है।",
        "apply_hint_web": "आधिकारिक सरकारी साइटों में खोजकर मिला। कोई भी जानकारी भरने से पहले जाँच लें कि पता .gov.in या .nic.in पर समाप्त होता है।",
        "apply_hint_generic": "कोई आधिकारिक लिंक नहीं मिला, इसलिए यह वेब पर खोजेगा: {scheme}. केवल .gov.in या .nic.in वाली वेबसाइट खोलें।"
    },
    "Kannada": {
        "title": "📜 ನಾಗರಿಕ ಮತ್ತು ಕಾನೂನು ನ್ಯಾವಿಗೇಟರ್",
        "select_lang": "🌐 Choose Language / भाषा चुनें / ಭಾಷೆಯನ್ನು ಆಯ್ಕೆಮಾಡಿ",
        "login_portal": "🔑 ನಾಗರಿಕ ಪೋರ್ಟಲ್ ಪ್ರವೇಶ",
        "sign_in": "ಸೈನ್ ಇನ್ ಮಾಡಿ",
        "create_account": "ಹೊಸ ಖಾತೆ ತೆರೆಯಿರಿ",
        "username": "ಬಳಕೆದಾರರ ಹೆಸರು (Username)",
        "password": "ಪಾಸ್‌ವರ್ಡ್",
        "full_name": "ಪೂರ್ಣ ಹೆಸರು",
        "age": "ವಯಸ್ಸು",
        "occupation": "ವೃತ್ತಿ",
        "state": "ರಾಜ್ಯ",
        "annual_income": "ವಾರ್ಷಿಕ ಆದಾಯ (₹)",
        "register_profile": "ಪ್ರೊಫೈಲ್ ನೋಂದಾಯಿಸಿ",
        "active_profile": "👤 ಸಕ್ರಿಯ ಪ್ರೊಫೈಲ್",
        "save_changes": "ಬದಲಾವಣೆಗಳನ್ನು ಉಳಿಸಿ",
        "logout": "ಲಾಗ್ ಔಟ್",
        "upload_section": "📤 ಯೋಜನೆ ದಾಖಲೆಯನ್ನು ಅಪ್‌ಲೋಡ್ ಮಾಡಿ",
        "upload_label": "ಅರ್ಹತೆಯನ್ನು ಪರಿಶೀಲಿಸಲು ಯೋಜನೆಯ PDF ಆಯ್ಕೆಮಾಡಿ:",
        "eval_button": "ಅರ್ಹತೆ ಪರಿಶೀಲಿಸಿ",
        "evaluating": "Gemini AI ಮೂಲಕ ದಾಖಲೆಯನ್ನು ಪರಿಶೀಲಿಸಲಾಗುತ್ತಿದೆ...",
        "eval_complete": "ಪರಿಶೀಲನೆ ಪೂರ್ಣಗೊಂಡಿದೆ!",
        "report_header": "📋 ಸುಲಭ ಅರ್ಹತಾ ಫಲಿತಾಂಶ",
        "app_portal_header": "🚀 ಯೋಜನೆ ಅರ್ಜಿ ಫಾರ್ಮ್ (ಆಟೋ-ಫಿಲ್)",
        "submit_app": "ಅರ್ಜಿ ಸಲ್ಲಿಸಿ",
        "redirect_msg": "ಅರ್ಜಿ ದಾಖಲಾಗಿದೆ! ಅಧಿಕೃತ ಪೋರ್ಟಲ್‌ಗೆ ಮರುನಿರ್ದೇಶಿಸಲಾಗುತ್ತಿದೆ...",
        "open_portal": "🌐 ಅಧಿಕೃತ ಸರ್ಕಾರಿ ಪೋರ್ಟಲ್‌ಗೆ ತೆರಳಿ",
        "more_info_warn": "🟡 ಹೆಚ್ಚಿನ ಮಾಹಿತಿ ಅಗತ್ಯವಿದೆ: ದಯವಿಟ್ಟು ಅಗತ್ಯ ದಾಖಲೆಗಳನ್ನು ಒದಗಿಸಿ.",
        "ineligible_err": "🔴 ಅರ್ಹರಿಲ್ಲ: ನಿಮ್ಮ ವಿವರಗಳ ಆಧಾರದ ಮೇಲೆ ನೀವು ಈ ಯೋಜನೆಗೆ ಅರ್ಹರಾಗಿಲ್ಲ.",
        "badge_eligible": "🟢 ನೀವು ಅರ್ಹರಾಗಿದ್ದೀರಿ (ಅರ್ಜಿ ಸಲ್ಲಿಸಬಹುದು)",
        "badge_ineligible": "🔴 ನೀವು ಅರ್ಹರಾಗಿಲ್ಲ",
        "badge_more_info": "🟡 ಹೆಚ್ಚಿನ ಮಾಹಿತಿ ಅಗತ್ಯವಿದೆ",
        "apply_now": "🔗 ಅಧಿಕೃತ ವೆಬ್‌ಸೈಟ್‌ನಲ್ಲಿ ಅರ್ಜಿ ಸಲ್ಲಿಸಿ",
        "check_site": "🔗 ಅಧಿಕೃತ ವೆಬ್‌ಸೈಟ್‌ನಲ್ಲಿ ವಿವರ ನೋಡಿ",
        "followup_intro": "ನಿರ್ಧರಿಸಲು ದಯವಿಟ್ಟು ಈ ಪ್ರಶ್ನೆಗಳಿಗೆ ಉತ್ತರಿಸಿ:",
        "followup_submit": "ನನ್ನ ಅರ್ಹತೆಯನ್ನು ಮತ್ತೆ ಪರಿಶೀಲಿಸಿ",
        "yes": "ಹೌದು",
        "no": "ಇಲ್ಲ",
        "your_answers": "📝 ನಿಮ್ಮ ಉತ್ತರಗಳು",
        "tagline": "ನಿಮ್ಮ ಭಾಷೆಯಲ್ಲಿ ಸರ್ಕಾರಿ ಯೋಜನೆಗಳ ಅರ್ಹತೆಯನ್ನು ಪರಿಶೀಲಿಸಿ.",
        "how_decided": "🔍 ನಾವು ಹೇಗೆ ನಿರ್ಧರಿಸಿದ್ದೇವೆ",
        "my_profile": "✏️ ನನ್ನ ಪ್ರೊಫೈಲ್ ಬದಲಿಸಿ",
        "details": "📄 ಸರಳ ಮಾತುಗಳಲ್ಲಿ ವಿವರ",
        "reading_page": "ಪುಟ {n} / {total} ಓದಲಾಗುತ್ತಿದೆ...",
        "apply_hint_doc": "ಈ ಲಿಂಕ್ ನೀವು ಅಪ್‌ಲೋಡ್ ಮಾಡಿದ ಯೋಜನೆಯ ದಾಖಲೆಯಲ್ಲಿ ಸಿಕ್ಕಿದೆ.",
        "apply_hint_known": "ಇದು ಈ ಯೋಜನೆಯ ಅಧಿಕೃತ ವೆಬ್‌ಸೈಟ್.",
        "apply_hint_web": "ಅಧಿಕೃತ ಸರ್ಕಾರಿ ತಾಣಗಳಲ್ಲಿ ಹುಡುಕಿ ಸಿಕ್ಕಿದೆ. ಯಾವುದೇ ಮಾಹಿತಿ ನೀಡುವ ಮೊದಲು ವಿಳಾಸ .gov.in ಅಥವಾ .nic.in ನಲ್ಲಿ ಕೊನೆಗೊಳ್ಳುತ್ತದೆಯೇ ಎಂದು ಪರಿಶೀಲಿಸಿ.",
        "apply_hint_generic": "ಅಧಿಕೃತ ಲಿಂಕ್ ಸಿಗಲಿಲ್ಲ, ಆದ್ದರಿಂದ ಇದು ವೆಬ್‌ನಲ್ಲಿ ಹುಡುಕುತ್ತದೆ: {scheme}. .gov.in ಅಥವಾ .nic.in ಇರುವ ವೆಬ್‌ಸೈಟ್ ಮಾತ್ರ ತೆರೆಯಿರಿ."
    }
}

# Language switcher at the top of the page
lang_choice = st.radio(
    TRANSLATIONS["English"]["select_lang"],
    ["English", "Hindi", "Kannada"],
    horizontal=True,
    format_func=lambda x: {"English": "English", "Hindi": "हिन्दी", "Kannada": "ಕನ್ನಡ"}[x],
    key="lang",
)
t = TRANSLATIONS[lang_choice]

# Password hashing (passwords are never stored in plain text)
def hash_password(password):
    # bcrypt only uses the first 72 bytes, so cut longer passwords safely
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")

def verify_password(password, stored_hash):
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], stored_hash.encode("utf-8"))
    except ValueError:  # old plain-text row or damaged hash
        return False

# Initialize Database
def init_db():
    conn = sqlite3.connect("user_profile.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name TEXT,
            age INTEGER,
            occupation TEXT,
            state TEXT,
            annual_income INTEGER
        )
    """)
    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        cursor.execute("""
            INSERT INTO users (username, password, name, age, occupation, state, annual_income)
            VALUES ('ramesh', ?, 'Ramesh Kumar', 42, 'Farmer', 'Karnataka', 150000)
        """, (hash_password("pass123"),))
    conn.commit()
    conn.close()

init_db()

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "user_data" not in st.session_state:
    st.session_state.user_data = None

def authenticate_user(username, password):
    conn = sqlite3.connect("user_profile.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, username, name, age, occupation, state, annual_income, password FROM users WHERE username = ?",
        (username,)
    )
    row = cursor.fetchone()
    conn.close()
    if row and verify_password(password, row[7]):
        return row[:7]
    return None

def register_user(username, password, name, age, occupation, state, annual_income):
    conn = sqlite3.connect("user_profile.db")
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO users (username, password, name, age, occupation, state, annual_income)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (username, hash_password(password), name, age, occupation, state, annual_income))
        conn.commit()
        conn.close()
        return True, "Account registered successfully!"
    except sqlite3.IntegrityError:
        conn.close()
        return False, "Username already exists."

def update_db_profile(user_id, name, age, occupation, state, annual_income):
    conn = sqlite3.connect("user_profile.db")
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users 
        SET name = ?, age = ?, occupation = ?, state = ?, annual_income = ?
        WHERE id = ?
    """, (name, age, occupation, state, annual_income, user_id))
    conn.commit()
    conn.close()

st.markdown(
    f'<div class="hero"><h1>{html.escape(t["title"])}</h1><p>{html.escape(t["tagline"])}</p></div>',
    unsafe_allow_html=True,
)

# Authentication Flow
if not st.session_state.authenticated:
    st.subheader(t["login_portal"])
    login_tab, register_tab = st.tabs([t["sign_in"], t["create_account"]])

    with login_tab:
        with st.form("login_form"):
            username = st.text_input(t["username"])
            password = st.text_input(t["password"], type="password")
            submitted = st.form_submit_button(t["sign_in"])

            if submitted:
                user = authenticate_user(username, password)
                if user:
                    st.session_state.authenticated = True
                    st.session_state.user_data = {
                        "id": user[0],
                        "username": user[1],
                        "name": user[2],
                        "age": user[3],
                        "occupation": user[4],
                        "state": user[5],
                        "annual_income": user[6]
                    }
                    st.rerun()
                else:
                    st.error("Invalid credentials.")

    with register_tab:
        with st.form("register_form"):
            new_username = st.text_input(f"{t['username']} *")
            new_password = st.text_input(f"{t['password']} *", type="password")
            st.divider()
            new_name = st.text_input(f"{t['full_name']} *")
            new_age = st.number_input(t["age"], min_value=18, max_value=120, value=25)
            new_occupation = st.text_input(t["occupation"], value="Farmer")
            new_state = st.text_input(t["state"], value="Karnataka")
            new_income = st.number_input(t["annual_income"], min_value=0, value=100000, step=5000)

            reg_submitted = st.form_submit_button(t["register_profile"])
            if reg_submitted:
                if not new_username or not new_password or not new_name:
                    st.error("Fill in required fields.")
                else:
                    success, message = register_user(
                        new_username, new_password, new_name, new_age, new_occupation, new_state, new_income
                    )
                    if success:
                        st.success(message)
                    else:
                        st.error(message)

else:
    u = st.session_state.user_data
    st.sidebar.header(f"{t['active_profile']}: {u['username']}")

    with st.sidebar.expander(t["my_profile"], expanded=False), st.form("edit_profile_form"):
        name = st.text_input(t["full_name"], value=u["name"])
        age = st.number_input(t["age"], min_value=18, max_value=120, value=int(u["age"]))
        occupation = st.text_input(t["occupation"], value=u["occupation"])
        state = st.text_input(t["state"], value=u["state"])
        annual_income = st.number_input(t["annual_income"], min_value=0, value=int(u["annual_income"]), step=5000)
        
        update_btn = st.form_submit_button(t["save_changes"])
        if update_btn:
            update_db_profile(u["id"], name, age, occupation, state, annual_income)
            st.session_state.user_data.update({
                "name": name,
                "age": age,
                "occupation": occupation,
                "state": state,
                "annual_income": annual_income
            })
            st.sidebar.success("Saved!")
            st.rerun()

    if st.sidebar.button(t["logout"]):
        st.session_state.authenticated = False
        st.session_state.user_data = None
        if "evaluation_result" in st.session_state:
            del st.session_state.evaluation_result
        st.rerun()

    user_info = f"Name: {u['name']}, Age: {u['age']}, Occupation: {u['occupation']}, State: {u['state']}, Annual Income: ₹{u['annual_income']}"

    st.caption(f"👤 {u['name']} · {u['occupation']} · {u['state']} · ₹{u['annual_income']}")
    st.subheader(t["upload_section"])
    uploaded_pdf = st.file_uploader(t["upload_label"], type=["pdf"])

    if uploaded_pdf is not None:
        if st.button(t["eval_button"], type="primary"):
            with st.spinner(t["evaluating"]):
                try:
                    client = genai.Client()
                    # Build the vector index once per uploaded file (not on every click)
                    file_key = f"{uploaded_pdf.name}-{uploaded_pdf.size}"
                    if st.session_state.get("index_key") != file_key:
                        bar = st.progress(0.0)
                        st.session_state.rag_index = build_index(
                            uploaded_pdf.getvalue(), client,
                            progress=lambda n, total: bar.progress(
                                n / total, text=t["reading_page"].format(n=n, total=total)),
                        )
                        bar.empty()
                        st.session_state.index_key = file_key

                    result = evaluate_with_rag(
                        st.session_state.rag_index, client, u, lang_choice
                    )
                    st.session_state.evaluation_result = result
                    st.session_state.result_lang = lang_choice
                    st.session_state.result_user = dict(u)
                    st.session_state.result_cache = {lang_choice: result}
                    st.session_state.result_extra = []
                    st.session_state.followup_round = 0
                    st.success(t["eval_complete"])

                except Exception as e:
                    st.error(f"Error evaluating document: {e}")

    # If the language was changed after checking, show the report in the new language.
    # Each language is generated once and then remembered (saves API requests).
    if "evaluation_result" in st.session_state and st.session_state.get("result_lang") != lang_choice:
        cache = st.session_state.result_cache
        if lang_choice not in cache:
            with st.spinner(t["evaluating"]):
                try:
                    cache[lang_choice] = evaluate_with_rag(
                        st.session_state.rag_index, genai.Client(),
                        st.session_state.result_user, lang_choice,
                        extra=st.session_state.get("result_extra"),
                        final_round=st.session_state.get("followup_round", 0) >= MAX_FOLLOWUPS,
                    )
                except Exception as e:
                    st.error(f"Error evaluating document: {e}")
        if lang_choice in cache:
            st.session_state.evaluation_result = cache[lang_choice]
            st.session_state.result_lang = lang_choice

    # ---------- Result ----------
    if "evaluation_result" in st.session_state:
        res = st.session_state.evaluation_result
        status = res.get("status", "").upper()
        scheme_name = res.get("scheme_name", "Scheme")
        apply_url = res.get("apply_url") or ""
        portal_url = apply_url or search_link(scheme_name)

        def apply_caption():
            source = res.get("apply_source")
            if apply_url and source == "known":
                return t["apply_hint_known"]
            if apply_url and source == "web":
                return t["apply_hint_web"]
            if apply_url:
                return t["apply_hint_doc"]
            return t["apply_hint_generic"].format(scheme=scheme_name)

        # 1. Verdict card
        kind, badge = {
            "ELIGIBLE": ("ok", t["badge_eligible"]),
            "MORE_INFO_NEEDED": ("warn", t["badge_more_info"]),
        }.get(status, ("bad", t["badge_ineligible"]))
        st.markdown(
            f'<div class="card {kind}"><div class="scheme">{html.escape(scheme_name)}</div>'
            f"<h2>{html.escape(badge)}</h2></div>",
            unsafe_allow_html=True,
        )

        # 2. Main action, right under the verdict
        if status == "ELIGIBLE":
            st.link_button(t["apply_now"], portal_url, type="primary")
            st.caption(apply_caption())
        elif status == "MORE_INFO_NEEDED" and apply_url:
            st.link_button(t["check_site"], apply_url)
            st.caption(apply_caption())

        # 3. Simple explanation
        with st.expander(t["details"], expanded=True):
            st.markdown(res.get("report_markdown", ""))

        if st.session_state.get("result_extra"):
            with st.expander(t["your_answers"]):
                for p in st.session_state.result_extra:
                    st.markdown(f"- **{p['q']}** {p['a']}")

        # 4. For people who want to check the work
        with st.expander(t["how_decided"]):
            st.markdown("**📎 Sources used (PDF pages)**")
            for c in res.get("sources", []):
                st.markdown(f"**PDF page {c['page']}**: {c['text'][:300]}...")
            found_urls = res.get("doc_urls", [])
            st.markdown("**🔎 Web addresses found in the document**")
            st.write("\n".join(f"- {x}" for x in found_urls) if found_urls else "None found.")

        # 5. What happens next
        if status == "ELIGIBLE":
            st.subheader(f"{t['app_portal_header']} - {scheme_name}")

            with st.form("auto_fill_application_form"):
                col1, col2 = st.columns(2)
                with col1:
                    app_name = st.text_input(t["full_name"], value=u["name"])
                    app_age = st.number_input(t["age"], value=int(u["age"]))
                    app_occupation = st.text_input(t["occupation"], value=u["occupation"])
                with col2:
                    app_state = st.text_input(t["state"], value=u["state"])
                    app_income = st.number_input(t["annual_income"], value=int(u["annual_income"]))

                submit_app = st.form_submit_button(f"{t['submit_app']} -> {scheme_name}")

                if submit_app:
                    st.success(t["redirect_msg"])
                    st.link_button(t["open_portal"], portal_url)

        elif status == "MORE_INFO_NEEDED":
            questions = res.get("questions", [])
            round_no = st.session_state.get("followup_round", 0)
            go = False
            if questions:
                st.info(t["followup_intro"])
                with st.form(f"followup_form_{round_no}_{lang_choice}"):
                    answers = []
                    for q in questions:
                        key = f"fq_{round_no}_{q['id']}_{lang_choice}"
                        if q["type"] == "choice":
                            ans = st.selectbox(q["question"], q["options"], key=key)
                        elif q["type"] == "yes_no":
                            ans = st.radio(q["question"], [t["yes"], t["no"]], horizontal=True, key=key)
                        elif q["type"] == "number":
                            ans = f"{st.number_input(q['question'], min_value=0.0, step=1.0, key=key):g}"
                        else:
                            ans = st.text_input(q["question"], key=key)
                        answers.append({"q": q["question"], "a": str(ans)})
                    go = st.form_submit_button(t["followup_submit"], type="primary")
            else:
                st.warning(t["more_info_warn"])

            if go:
                all_answers = st.session_state.get("result_extra", []) + answers
                next_round = round_no + 1
                done = False
                with st.spinner(t["evaluating"]):
                    try:
                        new_result = evaluate_with_rag(
                            st.session_state.rag_index, genai.Client(),
                            st.session_state.result_user, lang_choice,
                            extra=all_answers, final_round=next_round >= MAX_FOLLOWUPS,
                        )
                        st.session_state.evaluation_result = new_result
                        st.session_state.result_extra = all_answers
                        st.session_state.followup_round = next_round
                        st.session_state.result_lang = lang_choice
                        st.session_state.result_cache = {lang_choice: new_result}
                        done = True
                    except Exception as e:
                        st.error(f"Error evaluating document: {e}")
                if done:
                    st.rerun()

        else:
            st.error(t["ineligible_err"])