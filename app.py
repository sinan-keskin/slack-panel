import streamlit as st
import requests
import re
from io import BytesIO
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from datetime import date
import pandas as pd
import time
import psycopg

st.set_page_config(page_title="SinanKee", layout="wide", initial_sidebar_state="collapsed")

# ================== CONSTANTS ==================
TODAY = date.today()
TODAY_KEY = TODAY.isoformat()

DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_KEY = DAY_KEYS[TODAY.weekday()]

DAYS_TR = {
    0: "Pazartesi", 1: "Salı", 2: "Çarşamba",
    3: "Perşembe", 4: "Cuma", 5: "Cumartesi", 6: "Pazar"
}

SELECT_PLACEHOLDER = "Seçiniz…"
MANUAL_OPTION = "Manuel"
DEFAULT_CATEGORY = "Genel"

VAR_PATTERN = re.compile(r"\{\{([^{}]+)\}\}")

# Anchor temizleme
ANCHOR_HTML = re.compile(r'<a\s+[^>]*href=[\'"][^\'"]+[\'"][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
ANCHOR_MD = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')  # [text](url)

# ================== TR DATE (locale bağımsız) ==================
TR_MONTHS = {
    "ocak": 1, "şubat": 2, "subat": 2, "mart": 3, "nisan": 4,
    "mayıs": 5, "mayis": 5, "haziran": 6, "temmuz": 7,
    "ağustos": 8, "agustos": 8, "eylül": 9, "eylul": 9,
    "ekim": 10, "kasım": 11, "kasim": 11, "aralık": 12, "aralik": 12
}
TR_MONTH_NAMES = {
    1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran",
    7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık"
}

DATE_PREFIX_RE = re.compile(
    r"^\s*(\d{1,2})\.?\s+([A-Za-zÇĞİÖŞÜçğıöşü]+)\s*(\d{4})?\b",
    re.UNICODE
)

def extract_tr_date_from_name(name: str):
    """'16 Aralık Ek Limitli' -> date(YYYY,12,16). Yıl yoksa bu yıl."""
    if not name:
        return None
    m = DATE_PREFIX_RE.match(name.strip())
    if not m:
        return None
    day = int(m.group(1))
    mon = (m.group(2) or "").strip().lower()
    year = int(m.group(3)) if m.group(3) else date.today().year
    month = TR_MONTHS.get(mon)
    if not month:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None

def format_tr_date(d: date) -> str:
    """Locale'e bakmadan TR tarih basar: 16 Aralık 2025"""
    return f"{d.day:02d} {TR_MONTH_NAMES[d.month]} {d.year}"

# ================== DB ==================
@st.cache_resource
def get_conn():
    db_url = st.secrets.get("DATABASE_URL", "")
    if not db_url:
        st.error("DATABASE_URL secrets içinde yok.")
        st.stop()
    return psycopg.connect(db_url, autocommit=True)

def db_get_categories():
    with get_conn().cursor() as cur:
        cur.execute("select name from categories order by name")
        rows = cur.fetchall()
    cats = [r[0] for r in rows] if rows else []
    if DEFAULT_CATEGORY not in cats:
        cats.insert(0, DEFAULT_CATEGORY)
    return cats

def db_add_category(name: str):
    name = (name or "").strip()
    if not name:
        return
    with get_conn().cursor() as cur:
        cur.execute("insert into categories(name) values (%s) on conflict do nothing", (name,))

def db_delete_category(name: str):
    name = (name or "").strip()
    if not name or name == DEFAULT_CATEGORY:
        return
    with get_conn().cursor() as cur:
        cur.execute("update day_rows set category=%s where category=%s", (DEFAULT_CATEGORY, name))
        cur.execute("update variables set category=%s where category=%s", (DEFAULT_CATEGORY, name))
        cur.execute("update attachments set category=%s where category=%s", (DEFAULT_CATEGORY, name))
        cur.execute("delete from categories where name=%s and name<>%s", (name, DEFAULT_CATEGORY))

def db_get_day_rows(day_key: str):
    with get_conn().cursor() as cur:
        cur.execute(
            """
            select id, text, category, requires_attachment
            from day_rows
            where day_key=%s and active=true
            order by id asc
            """,
            (day_key,),
        )
        rows = cur.fetchall()
    return [
        {"id": r[0], "text": r[1], "category": r[2], "requires_attachment": bool(r[3])}
        for r in rows
    ]

def db_replace_day_rows(day_key: str, new_rows: list[dict]):
    with get_conn().cursor() as cur:
        cur.execute("delete from day_rows where day_key=%s", (day_key,))
        for r in new_rows:
            cur.execute(
                """
                insert into day_rows(day_key, text, category, requires_attachment, active)
                values (%s, %s, %s, %s, true)
                """,
                (day_key, r["text"], r["category"], bool(r.get("requires_attachment", False))),
            )

def db_add_day_row(day_key: str, text: str, category: str, requires_attachment: bool):
    with get_conn().cursor() as cur:
        cur.execute(
            """
            insert into day_rows(day_key, text, category, requires_attachment, active)
            values (%s, %s, %s, %s, true)
            """,
            (day_key, text, category, bool(requires_attachment)),
        )

def db_get_variables():
    out = {}
    with get_conn().cursor() as cur:
        cur.execute("select name, category from variables order by name")
        vars_ = cur.fetchall()
        for name, cat in vars_:
            cur.execute("select value from variable_options where variable_name=%s order by id", (name,))
            opts = [x[0] for x in cur.fetchall()]
            out[name] = {"category": cat, "options": opts}
    return out

def db_upsert_variable(name: str, category: str, options: list[str]):
    name = (name or "").strip()
    if not name:
        return
    category = (category or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
    options = [o.strip() for o in (options or []) if o and o.strip()]
    with get_conn().cursor() as cur:
        cur.execute(
            """
            insert into variables(name, category)
            values (%s,%s)
            on conflict (name) do update set category=excluded.category
            """,
            (name, category),
        )
        cur.execute("delete from variable_options where variable_name=%s", (name,))
        for o in options:
            cur.execute("insert into variable_options(variable_name, value) values (%s,%s)", (name, o))

def db_delete_variable(name: str):
    name = (name or "").strip()
    if not name:
        return
    with get_conn().cursor() as cur:
        cur.execute("delete from variables where name=%s", (name,))

def db_get_attachments(include_expired: bool):
    with get_conn().cursor() as cur:
        if include_expired:
            cur.execute("select name, category, url, valid_date from attachments order by name")
        else:
            cur.execute(
                """
                select name, category, url, valid_date
                from attachments
                where valid_date is null or valid_date >= current_date
                order by name
                """
            )
        rows = cur.fetchall()
    out = {}
    for name, cat, url, vdate in rows:
        out[name] = {"category": cat, "url": url, "valid_date": vdate}
    return out

def db_upsert_attachment(name: str, category: str, url: str, valid_date):
    name = (name or "").strip()
    url = (url or "").strip()
    if not name or not url:
        return
    category = (category or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
    with get_conn().cursor() as cur:
        cur.execute(
            """
            insert into attachments(name, category, url, valid_date)
            values (%s,%s,%s,%s)
            on conflict (name) do update
            set category=excluded.category, url=excluded.url, valid_date=excluded.valid_date
            """,
            (name, category, url, valid_date),
        )

def db_delete_attachment(name: str):
    name = (name or "").strip()
    if not name:
        return
    with get_conn().cursor() as cur:
        cur.execute("delete from attachments where name=%s", (name,))

# ---------------- SENT LOG ----------------
def db_get_sent_for_date(d: date, user_key: str):
    try:
        with get_conn().cursor() as cur:
            cur.execute(
                "select template_text from sent_log where sent_date=%s and user_key=%s order by id",
                (d, user_key),
            )
            rows = cur.fetchall()
        return [r[0] for r in rows]
    except Exception:
        with get_conn().cursor() as cur:
            cur.execute("select template_text from sent_log where sent_date=%s order by id", (d,))
            rows = cur.fetchall()
        return [r[0] for r in rows]

def db_get_sent_dates(user_key: str):
    try:
        with get_conn().cursor() as cur:
            cur.execute(
                "select sent_date, count(*) from sent_log where user_key=%s group by sent_date order by sent_date desc",
                (user_key,),
            )
            rows = cur.fetchall()
        return rows
    except Exception:
        with get_conn().cursor() as cur:
            cur.execute("select sent_date, count(*) from sent_log group by sent_date order by sent_date desc")
            rows = cur.fetchall()
        return rows

def db_get_sent_today_set(d: date, user_key: str):
    return set(db_get_sent_for_date(d, user_key))

def db_add_sent(d: date, template_text: str, user_key: str):
    try:
        with get_conn().cursor() as cur:
            cur.execute(
                "select 1 from sent_log where sent_date=%s and user_key=%s and template_text=%s limit 1",
                (d, user_key, template_text),
            )
            if cur.fetchone():
                return
            cur.execute(
                "insert into sent_log(sent_date, user_key, template_text) values (%s,%s,%s)",
                (d, user_key, template_text),
            )
    except Exception:
        with get_conn().cursor() as cur:
            cur.execute(
                "select 1 from sent_log where sent_date=%s and template_text=%s limit 1",
                (d, template_text),
            )
            if cur.fetchone():
                return
            cur.execute(
                "insert into sent_log(sent_date, template_text) values (%s,%s)",
                (d, template_text),
            )

# ================== HELPERS ==================
def extract_vars(text: str) -> list[str]:
    if not text:
        return []
    return [m.group(1).strip() for m in VAR_PATTERN.finditer(text) if m.group(1).strip()]

def looks_like_lightshot(url: str) -> bool:
    if not url:
        return False
    u = url.strip().lower()
    return ("prnt.sc/" in u) or ("prntscr.com" in u) or ("image.prntscr.com" in u)

def fetch_lightshot_image(prnt_url: str):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        page = requests.get(prnt_url, headers=headers, timeout=10)
        if page.status_code != 200:
            return None
        match = re.search(r'property="og:image"\s+content="([^"]+)"', page.text)
        if not match:
            return None
        image_url = match.group(1)
        img = requests.get(image_url, headers=headers, timeout=10)
        if img.status_code == 200 and img.headers.get("Content-Type", "").startswith("image/"):
            return BytesIO(img.content)
    except Exception:
        return None
    return None

def strip_anchors(text: str) -> str:
    if not text:
        return text
    text = ANCHOR_HTML.sub(r"\1", text)
    text = ANCHOR_MD.sub(r"\1", text)
    return text

def safe_filename_from_category(cat: str) -> str:
    cat = (cat or "image").strip()
    cat = re.sub(r'[\\/:*?"<>|]', "_", cat)
    cat = re.sub(r"\s+", " ", cat).strip()
    base = cat[:60] if cat else "image"
    return f"{base}.png"

# ================== SLACK ==================
def safe_chat_post(client: WebClient, channel_id: str, text: str):
    try:
        client.chat_postMessage(channel=channel_id, text=text)
        return None
    except SlackApiError as e:
        return f"chat_postMessage: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"chat_postMessage: {e}"

def safe_upload_image_with_comment(client: WebClient, channel_id: str, bio: BytesIO, message: str, filename: str):
    try:
        bio.seek(0)
        resp = client.files_upload_v2(
            channel=channel_id,
            file=bio,
            filename=filename,
            initial_comment=message
        )
        return resp, None
    except SlackApiError as e:
        return None, f"files_upload_v2: {e.response.get('error', str(e))}"
    except Exception as e:
        return None, f"files_upload_v2: {e}"

def wait_until_file_visible(client: WebClient, channel_id: str, file_id: str, timeout_sec: float = 12.0) -> bool:
    start = time.time()
    try:
        while time.time() - start < timeout_sec:
            hist = client.conversations_history(channel=channel_id, limit=10)
            msgs = hist.get("messages", [])
            for m in msgs:
                for f in (m.get("files") or []):
                    if f.get("id") == file_id:
                        return True
            time.sleep(0.4)
    except SlackApiError:
        time.sleep(1.2)
        return False
    except Exception:
        time.sleep(1.2)
        return False
    return False

# ================== LOGIN (2 USER) ==================
if "logged" not in st.session_state:
    st.session_state.logged = False
if "user_key" not in st.session_state:
    st.session_state.user_key = "Sinan"

if not st.session_state.logged:
    st.title("🔐 Giriş")
    pw = st.text_input("Parola", type="password")

    if st.button("Giriş"):
        pw1 = st.secrets.get("APP_PASSWORD", "")
        pw2 = st.secrets.get("APP_PASSWORD_2", "")

        if pw == pw1:
            st.session_state.user_key = "Sinan"
            st.session_state.logged = True
            st.rerun()
        elif pw2 and pw == pw2:
            st.session_state.user_key = "Yağmur"
            st.session_state.logged = True
            st.rerun()
        else:
            st.error("Parola yanlış")
    st.stop()

# ================== STATE ==================
if "link_cache" not in st.session_state:
    st.session_state.link_cache = {}

if "sending" not in st.session_state:
    st.session_state.sending = False
if "checking_links" not in st.session_state:
    st.session_state.checking_links = False

USER_KEY = st.session_state.get("user_key", "Sinan")
IS_SINAN = (USER_KEY == "Sinan")

# Slack token + channel seçimi
if USER_KEY == "Yağmur":
    token = st.secrets.get("SLACK_USER_TOKEN_2", "")
    channel_id = st.secrets.get("SLACK_CHANNEL_ID_2", "")
else:
    token = st.secrets.get("SLACK_USER_TOKEN", "")
    channel_id = st.secrets.get("SLACK_CHANNEL_ID", "")

if not token:
    st.error("Slack token secrets içinde yok.")
    st.stop()

client = WebClient(token=token)

if not channel_id:
    st.error("SLACK_CHANNEL_ID secrets içinde yok.")
    st.stop()

# Menü (rol bazlı)
if IS_SINAN:
    page = st.sidebar.radio("Menü", ["📤 Mesaj Gönder", "📜 Gönderim Logu", "⚙️ Ayarlar"])
    st.sidebar.caption(f"👤 Aktif kullanıcı: {USER_KEY}")
else:
    page = "📤 Mesaj Gönder"
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] { display: none; }
        [data-testid="stSidebarNav"] { display: none; }
        </style>
        """,
        unsafe_allow_html=True
    )

# =================================================
# 📜 GÖNDERİM LOGU (DB) — sadece Sinan
# =================================================
if page == "📜 Gönderim Logu":
    if not IS_SINAN:
        st.error("Bu sayfaya erişimin yok.")
        st.stop()

    st.title("📜 Gönderim Logu")
    st.caption("Supabase DB içinden seçtiğin tarihe ait gönderilen satırları gösterir.")
    st.divider()

    selected_date = st.date_input("Tarih seç", value=TODAY)
    items = db_get_sent_for_date(selected_date, USER_KEY)

    all_dates = db_get_sent_dates(USER_KEY)
    c1, c2, _ = st.columns([2, 2, 6])
    c1.metric("Toplam gün", len(all_dates))
    c2.metric("Seçilen gün gönderilen", len(items))

    if not items:
        st.info("Bu tarih için kayıt yok.")
    else:
        st.markdown("### Gönderilenler")
        for i, text in enumerate(items, start=1):
            st.write(f"{i}. {text}")

    st.divider()
    with st.expander("Tüm günleri özetle"):
        if all_dates:
            df = pd.DataFrame([{"Tarih": d.isoformat(), "Adet": c} for d, c in all_dates])
            st.dataframe(df, width="stretch", hide_index=True)
        else:
            st.write("Log boş.")

# =================================================
# 📤 MESAJ GÖNDER (DB)
# =================================================
if page == "📤 Mesaj Gönder":
    st.title("AksiyonKee")
    st.caption(f"📅 {DAYS_TR[TODAY.weekday()]} — {format_tr_date(TODAY)}")
    st.divider()

    categories = db_get_categories()
    variables = db_get_variables()
    attachments = db_get_attachments(include_expired=False)
    sent_today = db_get_sent_today_set(TODAY, USER_KEY)

    rows_today = db_get_day_rows(DAY_KEY)
    visible_rows = [r for r in rows_today if str(r.get("text", "") or "") not in sent_today]

    if not visible_rows:
        st.info("Bugün için gönderilecek yeni bir satır yok.")
        st.stop()

    templates = [str(r.get("text", "") or "") for r in visible_rows]
    vars_today = sorted({v for t in templates for v in extract_vars(t)})

    row_categories = []
    for r in visible_rows:
        c = str(r.get("category", DEFAULT_CATEGORY) or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
        if c not in categories:
            c = DEFAULT_CATEGORY
        row_categories.append(c)

    table_key = f"table_{DAY_KEY}_{TODAY_KEY}_{USER_KEY}"
    templates_key = f"templates_{DAY_KEY}_{TODAY_KEY}_{USER_KEY}"
    vars_key = f"vars_{DAY_KEY}_{TODAY_KEY}_{USER_KEY}"

    if table_key not in st.session_state:
        df_dict = {
            "Gönder": [True] * len(templates),
            "Kategori": row_categories,
            "Mesaj": templates,
            "Ek Zorunlu": [bool(r.get("requires_attachment", False)) for r in visible_rows],
            "Ek Seç": [SELECT_PLACEHOLDER if bool(r.get("requires_attachment", False)) else "" for r in visible_rows],
            "Lightshot Link": [""] * len(templates),
        }
        for var in vars_today:
            col = f"Var: {var}"
            df_dict[col] = [SELECT_PLACEHOLDER if var in extract_vars(t) else "" for t in templates]

        st.session_state[table_key] = pd.DataFrame(df_dict)
        st.session_state[templates_key] = templates
        st.session_state[vars_key] = vars_today

    b1, b2, b3, _ = st.columns([1, 1.6, 1.8, 6])
    if b1.button("✅ Tümünü Seç", disabled=st.session_state.sending or st.session_state.checking_links):
        st.session_state[table_key]["Gönder"] = True
        st.rerun()
    if b2.button("⛔ Tüm Seçimi Kaldır", disabled=st.session_state.sending or st.session_state.checking_links):
        st.session_state[table_key]["Gönder"] = False
        st.rerun()
    do_check = b3.button(
        "🔎 Linkleri Kontrol Et",
        disabled=st.session_state.checking_links or st.session_state.sending
    )

    df_in = st.session_state[table_key].copy()
    templates = st.session_state[templates_key]
    vars_today = st.session_state[vars_key]

    column_config = {
        "Gönder": st.column_config.CheckboxColumn("Gönder"),
        "Kategori": st.column_config.SelectboxColumn("Kategori", options=categories),
        "Mesaj": st.column_config.TextColumn("Mesaj"),
        "Ek Zorunlu": st.column_config.CheckboxColumn("Ek Zorunlu", disabled=True),
        "Ek Seç": st.column_config.SelectboxColumn(
            "Ek Seç",
            options=[SELECT_PLACEHOLDER, MANUAL_OPTION] + sorted(list(attachments.keys()))
        ),
        "Lightshot Link": st.column_config.TextColumn("Lightshot Link"),
    }

    for var in vars_today:
        vdef = variables.get(var, {})
        opts = vdef.get("options", []) if isinstance(vdef, dict) else []
        column_config[f"Var: {var}"] = st.column_config.SelectboxColumn(
            var,
            options=[SELECT_PLACEHOLDER] + (opts or [])
        )

    df_out = st.data_editor(
        df_in,
        width="stretch",
        hide_index=True,
        key=f"editor_{DAY_KEY}_{TODAY_KEY}_{USER_KEY}",
        column_config=column_config,
        disabled=["Ek Zorunlu"],
    )

    # ============== AUTO-CLEAN (SADE / VERİ SİLMEZ) ==============
    cleaned = False
    for idx in range(len(df_out)):
        req = bool(df_out.at[idx, "Ek Zorunlu"])
        row_cat = str(df_out.at[idx, "Kategori"] or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
        if row_cat not in categories:
            df_out.at[idx, "Kategori"] = DEFAULT_CATEGORY
            cleaned = True

        if not req:
            if str(df_out.at[idx, "Ek Seç"]).strip() not in ("", "None"):
                df_out.at[idx, "Ek Seç"] = ""
                cleaned = True
            if str(df_out.at[idx, "Lightshot Link"]).strip():
                df_out.at[idx, "Lightshot Link"] = ""
                cleaned = True

    if cleaned:
        st.session_state[table_key] = df_out
        st.rerun()
    st.session_state[table_key] = df_out

    # ============== LINK CHECK (LOCKLU) ==============
    if do_check and not st.session_state.checking_links:
        st.session_state.checking_links = True
        st.rerun()

    if st.session_state.checking_links:
        try:
            results = []
            df_check = df_out.reset_index(drop=True)
            for i in range(len(df_check)):
                row = df_check.loc[i]
                if not bool(row["Gönder"]) or not bool(row["Ek Zorunlu"]):
                    continue

                row_cat = str(row.get("Kategori") or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
                ek_sec = str(row.get("Ek Seç", "")).strip()
                link = str(row.get("Lightshot Link", "")).strip()

                if ek_sec in ("", SELECT_PLACEHOLDER, "None"):
                    results.append({"Satır": i + 1, "Sonuç": "❗ Ek seçilmedi"})
                    continue

                if ek_sec != MANUAL_OPTION:
                    preset = attachments.get(ek_sec)
                    if not isinstance(preset, dict):
                        results.append({"Satır": i + 1, "Sonuç": "❗ Preset yok"})
                        continue
                    if str(preset.get("category", DEFAULT_CATEGORY)).strip() != row_cat:
                        results.append({"Satır": i + 1, "Sonuç": "❗ Preset kategori uyumsuz"})
                        continue
                    link = str(preset.get("url", "") or "").strip()

                if not link:
                    results.append({"Satır": i + 1, "Sonuç": "❗ Link yok"})
                    continue

                if not looks_like_lightshot(link):
                    results.append({"Satır": i + 1, "Sonuç": "❗ Link prnt.sc değil"})
                    continue

                ok = st.session_state.link_cache.get(link)
                if ok is None:
                    ok = fetch_lightshot_image(link) is not None
                    st.session_state.link_cache[link] = ok
                results.append({"Satır": i + 1, "Sonuç": "✅ OK" if ok else "❌ Görsel alınamadı"})

            if results:
                df_res = pd.DataFrame(results)
                bad = df_res["Sonuç"].str.startswith("❌") | df_res["Sonuç"].str.startswith("❗")
                st.error("Link kontrolünde sorun var:") if bad.any() else st.success("Link kontrolü OK ✅")
                st.dataframe(df_res, width="stretch", hide_index=True)
            else:
                st.info("Kontrol edilecek ek yok.")
        finally:
            st.session_state.checking_links = False

    st.divider()

    # ============== SEND (locklu) ==============
    send_click = st.button(
        "Slack’e Gönder",
        disabled=st.session_state.sending or st.session_state.checking_links,
    )

    if send_click and not st.session_state.sending:
        st.session_state.sending = True
        st.rerun()

    if st.session_state.sending:
        try:
            errors = []
            send_items = []

            df_send = df_out.reset_index(drop=True)

            for i in range(len(df_send)):
                row = df_send.loc[i]
                if not bool(row["Gönder"]):
                    continue

                template = templates[i]
                message = str(row["Mesaj"]).strip()
                req = bool(row["Ek Zorunlu"])

                row_cat = str(row.get("Kategori") or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
                if row_cat not in categories:
                    row_cat = DEFAULT_CATEGORY

                message = strip_anchors(message)

                # değişken replace + validate
                row_vars = extract_vars(template)
                bad_row = False
                for v in row_vars:
                    vdef = variables.get(v, {})
                    vcat = str((vdef.get("category") if isinstance(vdef, dict) else DEFAULT_CATEGORY) or DEFAULT_CATEGORY).strip()
                    if vcat != row_cat:
                        errors.append(f"- Değişken kategori uyumsuz ({v}/{vcat}) satır:{row_cat} → {template}")
                        bad_row = True
                        break

                    col = f"Var: {v}"
                    sel = str(row.get(col, "")).strip()
                    if sel in ("", SELECT_PLACEHOLDER, "None"):
                        errors.append(f"- {v} seçilmedi: {template}")
                        bad_row = True
                        break

                    message = message.replace(f"{{{{{v}}}}}", sel)

                if bad_row:
                    continue

                fetched_img = None
                if req:
                    ek_sec = str(row.get("Ek Seç", "")).strip()
                    link = str(row.get("Lightshot Link", "")).strip()

                    if ek_sec in ("", SELECT_PLACEHOLDER, "None"):
                        errors.append(f"- Ek seçilmedi: {template}")
                        continue

                    if ek_sec != MANUAL_OPTION:
                        preset = attachments.get(ek_sec)
                        if not isinstance(preset, dict):
                            errors.append(f"- Preset bulunamadı: {template}")
                            continue
                        preset_cat = str(preset.get("category", DEFAULT_CATEGORY)).strip()
                        if preset_cat != row_cat:
                            errors.append(f"- Preset kategori uyumsuz ({ek_sec}/{preset_cat}) satır:{row_cat} → {template}")
                            continue
                        link = str(preset.get("url", "") or "").strip()

                    if not link:
                        errors.append(f"- Ek zorunlu ama link yok: {template}")
                        continue
                    if not looks_like_lightshot(link):
                        errors.append(f"- Link prnt.sc değil: {template}")
                        continue

                    fetched_img = fetch_lightshot_image(link)
                    st.session_state.link_cache[link] = (fetched_img is not None)
                    if fetched_img is None:
                        errors.append(f"- Görsel alınamadı: {template}")
                        continue

                if not message:
                    errors.append(f"- Mesaj boş: {template}")
                    continue

                send_items.append((template, message, fetched_img, row_cat))

            if errors:
                st.session_state.sending = False
                st.error("Gönderim durduruldu. Hatalar:")
                for e in errors[:160]:
                    st.write(e)
                st.stop()

            if not send_items:
                st.session_state.sending = False
                st.warning("Gönderilecek içerik yok.")
                st.stop()

            slack_errors = []

            for template, message, fetched_img, row_cat in send_items:
                if fetched_img is not None:
                    filename = safe_filename_from_category(row_cat)

                    resp, err = safe_upload_image_with_comment(
                        client, channel_id, fetched_img, message=message, filename=filename
                    )
                    if err:
                        slack_errors.append(f"- {template}: {err}")
                        continue

                    file_id = None
                    if isinstance(resp, dict):
                        f = resp.get("file")
                        if isinstance(f, dict):
                            file_id = f.get("id")

                    if file_id:
                        wait_until_file_visible(client, channel_id, file_id, timeout_sec=12.0)
                    else:
                        time.sleep(1.2)
                else:
                    err = safe_chat_post(client, channel_id, message)
                    if err:
                        slack_errors.append(f"- {template}: {err}")
                        continue
                    time.sleep(0.25)

                db_add_sent(TODAY, template, USER_KEY)

            if slack_errors:
                st.session_state.sending = False
                st.error("Bazı içerikler gönderilemedi:")
                for e in slack_errors[:100]:
                    st.write(e)
                st.stop()

            st.success("Slack’e gönderildi ✅")

            for k in [table_key, templates_key, vars_key]:
                st.session_state.pop(k, None)

            st.session_state.sending = False
            st.rerun()

        finally:
            st.session_state.sending = False

# =================================================
# ⚙️ AYARLAR (DB) — sadece Sinan
# =================================================
if page == "⚙️ Ayarlar":
    if not IS_SINAN:
        st.error("Bu sayfaya erişimin yok.")
        st.stop()

    st.title("⚙️ Ayarlar")

    categories = db_get_categories()
    variables = db_get_variables()
    attachments_all = db_get_attachments(include_expired=True)

    # -------- Kategoriler --------
    st.subheader("Kategoriler")
    c1, c2 = st.columns([2, 5])
    new_cat = c1.text_input("Yeni kategori adı", placeholder="Kampanya", key="cat_new_name")
    if c2.button("➕ Kategori Ekle"):
        name = (new_cat or "").strip()
        if not name:
            st.warning("Kategori adı boş olamaz.")
        else:
            db_add_category(name)
            st.success("Kategori eklendi ✅")
            st.rerun()

    st.write("Mevcut kategoriler:")
    for cat in categories:
        colA, colB = st.columns([6, 1])
        colA.write(f"- **{cat}**")
        disabled = (cat == DEFAULT_CATEGORY) or (len(categories) == 1)
        if colB.button("🗑️", key=f"del_cat_{cat}", disabled=disabled):
            db_delete_category(cat)
            st.success("Kategori silindi, bağlı içerikler Genel’e taşındı ✅")
            st.rerun()

    st.divider()

    # -------- Günlük Satırlar (CANLI DÜZENLE / KAYDETTE DB) --------
    st.subheader("Günlük Satırlar")
    selected_day_index = st.selectbox(
        "Hangi günün satırlarını düzenliyorsun?",
        options=list(range(7)),
        format_func=lambda i: DAYS_TR[i],
        index=TODAY.weekday(),
        key="settings_day_select",
    )
    selected_day_key = DAY_KEYS[selected_day_index]

    # --- Edit buffer (DB’ye kaydetmeden canlı düzenleme) ---
    buffer_key = f"day_rows_buffer_{selected_day_key}"

    prev_day_key = st.session_state.get("prev_settings_day_key")
    if prev_day_key != selected_day_key:
        if prev_day_key:
            st.session_state.pop(f"day_rows_buffer_{prev_day_key}", None)
        st.session_state["prev_settings_day_key"] = selected_day_key

    if buffer_key not in st.session_state:
        rows_db = db_get_day_rows(selected_day_key)
        st.session_state[buffer_key] = [
            {
                "rid": int(r["id"]),  # stabil id
                "text": str(r.get("text", "") or ""),
                "category": str(r.get("category", DEFAULT_CATEGORY) or DEFAULT_CATEGORY),
                "requires_attachment": bool(r.get("requires_attachment", False)),
            }
            for r in rows_db
        ]

    rows = st.session_state[buffer_key]

    st.markdown("### Sıralama ve Düzenleme (Kaydet deyince DB’ye yazar)")

    for i, row in enumerate(rows):
        rid = row["rid"]
        c1, c2, c3, c4, c5 = st.columns([0.6, 0.6, 6, 2, 1])

        up_clicked = c1.button("⬆️", key=f"up_{selected_day_key}_{rid}", disabled=(i == 0))
        down_clicked = c2.button("⬇️", key=f"down_{selected_day_key}_{rid}", disabled=(i == len(rows) - 1))

        # butona basmak zaten rerun yapar; ekstra st.rerun yok
        if up_clicked and i > 0:
            rows[i - 1], rows[i] = rows[i], rows[i - 1]
        if down_clicked and i < len(rows) - 1:
            rows[i + 1], rows[i] = rows[i], rows[i + 1]

        row["text"] = c3.text_input(
            "Metin",
            value=row["text"],
            key=f"text_{selected_day_key}_{rid}",
            label_visibility="collapsed",
        )

        current_cat = str(row.get("category") or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
        if current_cat not in categories:
            current_cat = DEFAULT_CATEGORY

        row["category"] = c4.selectbox(
            "Kategori",
            options=categories,
            index=categories.index(current_cat) if current_cat in categories else 0,
            key=f"cat_{selected_day_key}_{rid}",
            label_visibility="collapsed",
        )

        row["requires_attachment"] = c5.checkbox(
            "Ek",
            value=bool(row.get("requires_attachment", False)),
            key=f"req_{selected_day_key}_{rid}",
            label_visibility="collapsed",
        )

    st.session_state[buffer_key] = rows

    csave, _ = st.columns([2, 6])
    if csave.button("💾 Günlük satırları kaydet"):
        cleaned_rows = []
        for r in st.session_state[buffer_key]:
            t = str(r.get("text", "")).strip()
            if not t:
                continue
            cat = str(r.get("category") or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
            if cat not in categories:
                cat = DEFAULT_CATEGORY
            cleaned_rows.append({
                "text": t,
                "category": cat,
                "requires_attachment": bool(r.get("requires_attachment", False)),
            })

        db_replace_day_rows(selected_day_key, cleaned_rows)
        st.session_state.pop(buffer_key, None)
        st.success("Kaydedildi ✅")
        st.rerun()

    st.divider()

    # -------- Yeni Satır Ekle --------
    st.subheader("Yeni Satır Ekle")
    new_text = st.text_input(
        "Mesaj",
        placeholder="Örn: Bugünün Ana Kampanyası {{Kampanya}} Kampanyası Aktif Edildi.",
        key="new_row_text",
    )
    new_cat2 = st.selectbox("Kategori", options=categories, index=0, key="new_row_cat")
    new_req = st.checkbox("Bu satırda ek zorunlu olsun", value=False, key="new_row_req")

    if st.button("➕ Satırı Ekle"):
        t = (new_text or "").strip()
        if not t:
            st.warning("Mesaj boş olamaz.")
        else:
            db_add_day_row(selected_day_key, t, new_cat2, bool(new_req))
            st.session_state.pop(buffer_key, None)  # buffer reset
            st.success("Satır eklendi ✅")
            st.rerun()

    st.caption("İpucu: Değişken placeholder `{{Kampanya}}` gibi. Değişken kategorisi satır kategorisiyle aynı olmalı.")

    st.divider()

    # -------- Değişkenler --------
    st.subheader("Değişkenler")
    existing_vars = sorted(list(variables.keys()))
    pick = st.selectbox("Düzenlemek için mevcut değişken seç (opsiyonel)", options=["(Yeni)"] + existing_vars, key="var_pick")

    if pick != "(Yeni)":
        vdef = variables.get(pick, {})
        default_name = pick
        default_cat = vdef.get("category", DEFAULT_CATEGORY) if isinstance(vdef, dict) else DEFAULT_CATEGORY
        default_opts = "\n".join(vdef.get("options", [])) if isinstance(vdef, dict) else ""
    else:
        default_name, default_cat, default_opts = "", DEFAULT_CATEGORY, ""

    v1, v2, v3 = st.columns([2, 2, 5])
    var_name = v1.text_input("Değişken Adı", value=default_name, placeholder="Kampanya", key="var_name")
    var_cat = v2.selectbox("Kategori", options=categories, index=categories.index(default_cat) if default_cat in categories else 0, key="var_cat")
    var_opts = v3.text_area("Seçenekler (satır satır)", value=default_opts, height=120, key="var_opts")

    bA, bB, _ = st.columns([2, 2, 6])
    if bA.button("💾 Kaydet / Güncelle", key="var_save"):
        name = (var_name or "").strip()
        if not name:
            st.error("Değişken adı boş olamaz.")
        else:
            options = [x.strip() for x in (var_opts or "").splitlines() if x.strip()]
            db_upsert_variable(name, var_cat, options)
            st.success(f"Kaydedildi: {name} ✅")
            st.rerun()

    if bB.button("🗑️ Sil", disabled=(pick == "(Yeni)"), key="var_del"):
        db_delete_variable(pick)
        st.success("Silindi ✅")
        st.rerun()

    st.divider()

    # -------- Ek Presetleri --------
    st.subheader("Ek Presetleri (Lightshot URL)")

    existing_atts = sorted(list(attachments_all.keys()))
    apick = st.selectbox("Düzenlemek için mevcut preset seç (opsiyonel)", options=["(Yeni)"] + existing_atts, key="att_pick")

    if apick != "(Yeni)":
        adef = attachments_all.get(apick, {})
        default_att_name = apick
        default_att_cat = adef.get("category", DEFAULT_CATEGORY) if isinstance(adef, dict) else DEFAULT_CATEGORY
        default_att_url = adef.get("url", "") if isinstance(adef, dict) else ""
    else:
        default_att_name, default_att_cat, default_att_url = "", DEFAULT_CATEGORY, ""

    a1, a2, a3 = st.columns([2, 2, 5])
    att_name = a1.text_input("Ek Adı", value=default_att_name, placeholder="16 Aralık Limitli", key="att_name")
    att_cat = a2.selectbox("Kategori", options=categories, index=categories.index(default_att_cat) if default_att_cat in categories else 0, key="att_cat")
    att_url = a3.text_input("Lightshot / prnt.sc URL", value=default_att_url, placeholder="https://prnt.sc/xxxxxxx", key="att_url")

    inferred_date = extract_tr_date_from_name((att_name or "").strip())
    if inferred_date:
        st.caption(f"🗓️ Tarih algılandı: {format_tr_date(inferred_date)} (bu tarihten önce otomatik gizlenir)")

    xA, xB, _ = st.columns([2, 2, 6])
    if xA.button("💾 Kaydet / Güncelle", key="att_save"):
        n = (att_name or "").strip()
        u = (att_url or "").strip()
        if not n or not u:
            st.error("Ek adı ve URL zorunlu.")
        else:
            vdate = extract_tr_date_from_name(n)  # yoksa NULL => süresiz
            db_upsert_attachment(n, att_cat, u, vdate)
            st.success("Eklendi/Güncellendi ✅")
            st.rerun()

    if xB.button("🗑️ Sil", disabled=(apick == "(Yeni)"), key="att_del"):
        db_delete_attachment(apick)
        st.success("Silindi ✅")
        st.rerun()
