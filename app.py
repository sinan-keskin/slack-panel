import streamlit as st
import json
import requests
import re
from io import BytesIO
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from datetime import date
import pandas as pd
import os
import time

st.set_page_config(page_title="Slack Mesaj Paneli", layout="wide", initial_sidebar_state="collapsed")

# ================== CONSTANTS ==================
CONFIG_FILE = "config.json"
SENT_LOG_FILE = "sent_log.json"

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


# ================== SAFE JSON IO ==================
def atomic_save_json(path: str, obj: dict):
    """Windows file-lock tolerant atomic-ish save."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

    try:
        if os.path.exists(path):
            os.remove(path)
        os.rename(tmp, path)
    except PermissionError:
        time.sleep(0.1)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


# ================== CONFIG IO (SAFE + MIGRATE) ==================
def default_config():
    return {
        "categories": [DEFAULT_CATEGORY],
        "days": {k: [] for k in DAY_KEYS},
        "variables": {},     # {"Kampanya": {"category":"Kampanya","options":[...]}}
        "attachments": {}    # {"Preset": {"category":"Kampanya","url":"https://prnt.sc/..."}}
    }

def migrate_config(cfg):
    if not isinstance(cfg, dict):
        cfg = default_config()

    # categories
    cats = cfg.get("categories")
    if not isinstance(cats, list) or not cats:
        cats = [DEFAULT_CATEGORY]
    cats = [str(x).strip() for x in cats if str(x).strip()]
    if not cats:
        cats = [DEFAULT_CATEGORY]
    if DEFAULT_CATEGORY not in cats:
        cats.insert(0, DEFAULT_CATEGORY)
    cfg["categories"] = cats

    # days
    if "days" not in cfg or not isinstance(cfg["days"], dict):
        cfg["days"] = {k: [] for k in DAY_KEYS}
    for day in DAY_KEYS:
        cfg["days"].setdefault(day, [])
        new_rows = []
        for r in cfg["days"][day]:
            if isinstance(r, str):
                new_rows.append({"text": r, "requires_attachment": False, "category": DEFAULT_CATEGORY})
            elif isinstance(r, dict):
                cat = str(r.get("category", DEFAULT_CATEGORY) or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
                if cat not in cfg["categories"]:
                    cat = DEFAULT_CATEGORY
                new_rows.append({
                    "text": str(r.get("text", "") or ""),
                    "requires_attachment": bool(r.get("requires_attachment", False)),
                    "category": cat
                })
        cfg["days"][day] = new_rows

    # variables
    if "variables" not in cfg or not isinstance(cfg["variables"], dict):
        cfg["variables"] = {}
    for k, v in list(cfg["variables"].items()):
        name = str(k).strip()
        if not name:
            cfg["variables"].pop(k, None)
            continue
        if isinstance(v, dict):
            cat = str(v.get("category", DEFAULT_CATEGORY) or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
            if cat not in cfg["categories"]:
                cat = DEFAULT_CATEGORY
            opts = v.get("options", [])
            if not isinstance(opts, list):
                opts = [opts] if opts is not None else []
            opts = [str(x).strip() for x in opts if str(x).strip()]
            cfg["variables"][name] = {"category": cat, "options": opts}
        else:
            opts = v if isinstance(v, list) else ([v] if v is not None else [])
            opts = [str(x).strip() for x in opts if str(x).strip()]
            cfg["variables"][name] = {"category": DEFAULT_CATEGORY, "options": opts}

    # attachments
    if "attachments" not in cfg or not isinstance(cfg["attachments"], dict):
        cfg["attachments"] = {}
    for k, v in list(cfg["attachments"].items()):
        name = str(k).strip()
        if not name:
            cfg["attachments"].pop(k, None)
            continue
        if isinstance(v, dict):
            cat = str(v.get("category", DEFAULT_CATEGORY) or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
            if cat not in cfg["categories"]:
                cat = DEFAULT_CATEGORY
            url = str(v.get("url", "") or "").strip()
            if not url:
                cfg["attachments"].pop(k, None)
            else:
                cfg["attachments"][name] = {"category": cat, "url": url}
        else:
            url = str(v or "").strip()
            if not url:
                cfg["attachments"].pop(k, None)
            else:
                cfg["attachments"][name] = {"category": DEFAULT_CATEGORY, "url": url}

    return cfg
    
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

def cleanup_expired_attachments(attachments: dict) -> tuple[dict, bool]:
    """Tarihi geçmiş (bugünden önce) olan isimleri siler. (cleaned, changed)"""
    if not isinstance(attachments, dict):
        return {}, True
    today = date.today()
    out = {}
    changed = False
    for name, data in attachments.items():
        d = extract_tr_date_from_name(name)
        if d and d < today:
            changed = True
            continue
        out[name] = data
    return out, changed


def load_config():
    if not os.path.exists(CONFIG_FILE):
        cfg = default_config()
        atomic_save_json(CONFIG_FILE, cfg)
        return cfg

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()

        if not content:
            cfg = default_config()
            atomic_save_json(CONFIG_FILE, cfg)
            return cfg

        cfg = json.loads(content)
        cfg = migrate_config(cfg)

        # 🔥 Tarihi geçen ek presetlerini otomatik temizle
        cleaned_atts, changed = cleanup_expired_attachments(cfg.get("attachments", {}))
        if changed:
            cfg["attachments"] = cleaned_atts

        atomic_save_json(CONFIG_FILE, cfg)
        return cfg

    except Exception:
        cfg = default_config()
        atomic_save_json(CONFIG_FILE, cfg)
        return cfg




# ================== SENT LOG (PERSIST) ==================
def default_sent_log():
    return {"by_date": {}}

def load_sent_log():
    if not os.path.exists(SENT_LOG_FILE):
        log = default_sent_log()
        atomic_save_json(SENT_LOG_FILE, log)
        return log
    try:
        with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            log = default_sent_log()
            atomic_save_json(SENT_LOG_FILE, log)
            return log
        log = json.loads(content)
        if not isinstance(log, dict) or "by_date" not in log or not isinstance(log["by_date"], dict):
            log = default_sent_log()
        atomic_save_json(SENT_LOG_FILE, log)
        return log
    except Exception:
        log = default_sent_log()
        atomic_save_json(SENT_LOG_FILE, log)
        return log

def add_sent_today(sent_log: dict, template_text: str):
    sent_log["by_date"].setdefault(TODAY_KEY, [])
    if template_text not in sent_log["by_date"][TODAY_KEY]:
        sent_log["by_date"][TODAY_KEY].append(template_text)
        atomic_save_json(SENT_LOG_FILE, sent_log)


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
    """HTML <a> ve Markdown [text](url) anchorlarını kaldırır, sadece metni bırakır."""
    if not text:
        return text
    text = ANCHOR_HTML.sub(r"\1", text)
    text = ANCHOR_MD.sub(r"\1", text)
    return text

def safe_filename_from_category(cat: str) -> str:
    cat = (cat or "image").strip()
    cat = re.sub(r'[\\/:*?"<>|]', "_", cat)  # Windows-safe
    cat = re.sub(r"\s+", " ", cat).strip()
    base = cat[:60] if cat else "image"
    return f"{base}.png"

def get_slack_client():
    token = st.secrets.get("SLACK_USER_TOKEN", "")
    if not token:
        st.error("SLACK_USER_TOKEN secrets içinde yok.")
        st.stop()
    return WebClient(token=token)

def safe_chat_post(client: WebClient, channel_id: str, text: str):
    try:
        client.chat_postMessage(channel=channel_id, text=text)
        return None
    except SlackApiError as e:
        return f"chat_postMessage: {e.response.get('error', str(e))}"
    except Exception as e:
        return f"chat_postMessage: {e}"

def safe_upload_image_with_comment(client: WebClient, channel_id: str, bio: BytesIO, message: str, filename: str):
    """Tek post: görsel + mesaj (initial_comment)."""
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
    """
    files_upload_v2 döndükten sonra, dosyanın kanalda message olarak görünmesini bekler.
    conversations_history scope yoksa fallback sleep yapar.
    """
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


# ================== LOGIN ==================
if "logged" not in st.session_state:
    st.session_state.logged = False

if not st.session_state.logged:
    st.title("🔐 Giriş")
    pw = st.text_input("Parola", type="password")
    if st.button("Giriş"):
        if pw == st.secrets.get("APP_PASSWORD", ""):
            st.session_state.logged = True
            st.rerun()
        else:
            st.error("Parola yanlış")
    st.stop()


# ================== STATE ==================
if "link_cache" not in st.session_state:
    st.session_state.link_cache = {}  # url -> bool

cfg = load_config()
sent_log = load_sent_log()
sent_today = set(sent_log.get("by_date", {}).get(TODAY_KEY, []))

client = get_slack_client()
channel_id = st.secrets.get("SLACK_CHANNEL_ID", "")
if not channel_id:
    st.error("SLACK_CHANNEL_ID secrets içinde yok.")
    st.stop()

page = st.sidebar.radio("Menü", ["📤 Mesaj Gönder", "📜 Gönderim Logu", "⚙️ Ayarlar"])


# =================================================
# 📜 GÖNDERİM LOGU
# =================================================
if page == "📜 Gönderim Logu":
    st.title("📜 Gönderim Logu")
    st.caption("sent_log.json içinden seçtiğin tarihe ait gönderilen satırları gösterir.")
    st.divider()

    sent_log2 = load_sent_log()
    by_date = sent_log2.get("by_date", {})

    selected_date = st.date_input("Tarih seç", value=TODAY)
    selected_key = selected_date.isoformat()

    items = by_date.get(selected_key, [])

    c1, c2, _ = st.columns([2, 2, 6])
    c1.metric("Toplam gün", len(by_date))
    c2.metric("Seçilen gün gönderilen", len(items))

    if not items:
        st.info("Bu tarih için kayıt yok.")
    else:
        st.markdown("### Gönderilenler")
        for i, text in enumerate(items, start=1):
            st.write(f"{i}. {text}")

    st.divider()
    with st.expander("Tüm günleri özetle"):
        all_rows = [{"Tarih": d, "Adet": len(msgs)} for d, msgs in sorted(by_date.items(), reverse=True)]
        if all_rows:
            st.dataframe(pd.DataFrame(all_rows), width="stretch", hide_index=True)
        else:
            st.write("Log boş.")


# =================================================
# 📤 MESAJ GÖNDER
# =================================================
if page == "📤 Mesaj Gönder":
    st.title("Slack Mesaj Paneli")
    st.caption(f"📅 {DAYS_TR[TODAY.weekday()]} — {format_tr_date(TODAY)}")
    st.divider()

    cfg = load_config()
    categories = cfg.get("categories", [DEFAULT_CATEGORY])
    variables = cfg.get("variables", {})
    attachments = cfg.get("attachments", {})

    rows_today = cfg["days"].get(DAY_KEY, [])
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

    table_key = f"table_{DAY_KEY}_{TODAY_KEY}"
    templates_key = f"templates_{DAY_KEY}_{TODAY_KEY}"
    vars_key = f"vars_{DAY_KEY}_{TODAY_KEY}"

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
    if b1.button("✅ Tümünü Seç"):
        st.session_state[table_key]["Gönder"] = True
        st.rerun()
    if b2.button("⛔ Tüm Seçimi Kaldır"):
        st.session_state[table_key]["Gönder"] = False
        st.rerun()
    do_check = b3.button("🔎 Linkleri Kontrol Et")

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
        key=f"editor_{DAY_KEY}_{TODAY_KEY}",
        column_config=column_config,
        disabled=["Ek Zorunlu", "Ek Seç", "Lightshot Link"],
    )

    # ============== AUTO-CLEAN (kategori + dropdown sadece seçim) ==============
    cleaned = False
    allowed_ek = set(["", "None", SELECT_PLACEHOLDER, MANUAL_OPTION] + sorted(list(attachments.keys())))

    for idx in range(len(df_out)):
        template = templates[idx]
        req = bool(df_out.at[idx, "Ek Zorunlu"])

        # Kategori: sadece seçim
        row_cat = str(df_out.at[idx, "Kategori"] or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
        if row_cat not in categories:
            df_out.at[idx, "Kategori"] = DEFAULT_CATEGORY
            row_cat = DEFAULT_CATEGORY
            cleaned = True

        row_vars_in_text = set(extract_vars(template))

        # Ek Seç: sadece seçim
        ek_sec = str(df_out.at[idx, "Ek Seç"]).strip()
        if ek_sec not in allowed_ek:
            df_out.at[idx, "Ek Seç"] = SELECT_PLACEHOLDER if req else ""
            if not req:
                df_out.at[idx, "Lightshot Link"] = ""
            cleaned = True

        # Ek zorunlu değilse ek alanlarını temizle
        if not req:
            if str(df_out.at[idx, "Ek Seç"]).strip() not in ("", "None"):
                df_out.at[idx, "Ek Seç"] = ""
                cleaned = True
            if str(df_out.at[idx, "Lightshot Link"]).strip():
                df_out.at[idx, "Lightshot Link"] = ""
                cleaned = True
        else:
            # Preset seçiliyse kategori kontrol + URL zorla
            if ek_sec and ek_sec not in ("None", "") and ek_sec not in (SELECT_PLACEHOLDER, MANUAL_OPTION):
                preset = attachments.get(ek_sec)
                preset_cat = preset.get("category") if isinstance(preset, dict) else DEFAULT_CATEGORY
                if preset_cat != row_cat:
                    df_out.at[idx, "Ek Seç"] = SELECT_PLACEHOLDER
                    df_out.at[idx, "Lightshot Link"] = ""
                    cleaned = True
                else:
                    preset_url = str(preset.get("url", "") or "").strip()
                    if preset_url and str(df_out.at[idx, "Lightshot Link"]).strip() != preset_url:
                        df_out.at[idx, "Lightshot Link"] = preset_url
                        cleaned = True

        # Değişken kolonları: sadece seçim + kategori uyumu
        for var in vars_today:
            col = f"Var: {var}"
            val = str(df_out.at[idx, col]).strip()

            vdef = variables.get(var, {})
            opts = vdef.get("options", []) if isinstance(vdef, dict) else []
            allowed_var = set(["", "None", SELECT_PLACEHOLDER] + (opts or []))

            # metin yazılmış/paste edilmişse düzelt
            if val not in allowed_var:
                df_out.at[idx, col] = SELECT_PLACEHOLDER if var in row_vars_in_text else ""
                cleaned = True
                val = str(df_out.at[idx, col]).strip()

            # satırda placeholder yoksa temizle
            if var not in row_vars_in_text:
                if val and val != "None":
                    df_out.at[idx, col] = ""
                    cleaned = True
                continue

            # placeholder var → kategori uyumu şart
            vcat = vdef.get("category") if isinstance(vdef, dict) else DEFAULT_CATEGORY
            if vcat != row_cat:
                df_out.at[idx, col] = SELECT_PLACEHOLDER
                cleaned = True

    if cleaned:
        st.session_state[table_key] = df_out
        st.rerun()

    st.session_state[table_key] = df_out

    # ============== LINK CHECK (sonuçları ayrı göster) ==============
    if do_check:
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
                if preset.get("category", DEFAULT_CATEGORY) != row_cat:
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

    st.divider()

    # ============== SEND (tablo sırası + görsel post görünene kadar bekle) ==============
    if st.button("Slack’e Gönder"):
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
            for v in row_vars:
                vdef = variables.get(v, {})
                vcat = vdef.get("category") if isinstance(vdef, dict) else DEFAULT_CATEGORY
                if vcat != row_cat:
                    errors.append(f"- Değişken kategori uyumsuz ({v}/{vcat}) satır:{row_cat} → {template}")
                    break

                col = f"Var: {v}"
                sel = str(row.get(col, "")).strip()
                if sel in ("", SELECT_PLACEHOLDER, "None"):
                    errors.append(f"- {v} seçilmedi: {template}")
                    break

                message = message.replace(f"{{{{{v}}}}}", sel)
            else:
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
                        preset_cat = preset.get("category", DEFAULT_CATEGORY)
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
            st.error("Gönderim durduruldu. Hatalar:")
            for e in errors[:160]:
                st.write(e)
            st.stop()

        if not send_items:
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

            add_sent_today(sent_log, template)

        if slack_errors:
            st.error("Bazı içerikler gönderilemedi:")
            for e in slack_errors[:100]:
                st.write(e)
            st.stop()

        st.success("Slack’e gönderildi ✅")

        for k in [table_key, templates_key, vars_key]:
            if k in st.session_state:
                del st.session_state[k]
        st.rerun()


# =================================================
# ⚙️ AYARLAR
# =================================================
if page == "⚙️ Ayarlar":
    st.title("⚙️ Ayarlar")

    cfg = load_config()
    categories = cfg.get("categories", [DEFAULT_CATEGORY])
    variables = cfg.get("variables", {})
    attachments = cfg.get("attachments", {})

    st.subheader("Kategoriler")
    c1, c2 = st.columns([2, 5])
    new_cat = c1.text_input("Yeni kategori adı", placeholder="Kampanya", key="cat_new_name")
    if c2.button("➕ Kategori Ekle"):
        name = (new_cat or "").strip()
        if not name:
            st.warning("Kategori adı boş olamaz.")
        else:
            if name not in categories:
                categories.append(name)
                cfg["categories"] = categories
                atomic_save_json(CONFIG_FILE, cfg)
                st.success("Kategori eklendi ✅")
                st.rerun()
            else:
                st.info("Bu kategori zaten var.")

    if categories:
        st.write("Mevcut kategoriler:")
        for cat in categories:
            colA, colB = st.columns([6, 1])
            colA.write(f"- **{cat}**")
            disabled = (cat == DEFAULT_CATEGORY) or (len(categories) == 1)
            if colB.button("🗑️", key=f"del_cat_{cat}", disabled=disabled):
                categories = [c for c in categories if c != cat]
                if DEFAULT_CATEGORY not in categories:
                    categories.insert(0, DEFAULT_CATEGORY)

                for d in DAY_KEYS:
                    for r in cfg["days"].get(d, []):
                        if str(r.get("category", DEFAULT_CATEGORY)) == cat:
                            r["category"] = DEFAULT_CATEGORY

                for _, vdef in list(cfg["variables"].items()):
                    if isinstance(vdef, dict) and vdef.get("category") == cat:
                        vdef["category"] = DEFAULT_CATEGORY

                for _, adef in list(cfg["attachments"].items()):
                    if isinstance(adef, dict) and adef.get("category") == cat:
                        adef["category"] = DEFAULT_CATEGORY

                cfg["categories"] = categories
                atomic_save_json(CONFIG_FILE, cfg)
                st.success("Kategori silindi, bağlı içerikler Genel’e taşındı ✅")
                st.rerun()

    st.divider()

    st.subheader("Günlük Satırlar")
    selected_day_index = st.selectbox(
        "Hangi günün satırlarını düzenliyorsun?",
        options=list(range(7)),
        format_func=lambda i: DAYS_TR[i],
        index=TODAY.weekday(),
        key="settings_day_select",
    )
    selected_day_key = DAY_KEYS[selected_day_index]
    rows = cfg["days"].setdefault(selected_day_key, [])

    settings_df = pd.DataFrame({
        "Metin": [str((r.get("text", "") if isinstance(r, dict) else "") or "") for r in rows],
        "Kategori": [str((r.get("category", DEFAULT_CATEGORY) if isinstance(r, dict) else DEFAULT_CATEGORY) or DEFAULT_CATEGORY) for r in rows],
        "Ek Zorunlu": [bool((r.get("requires_attachment", False) if isinstance(r, dict) else False)) for r in rows],
    })
    settings_df["Metin"] = settings_df["Metin"].fillna("").astype(str)
    settings_df["Kategori"] = settings_df["Kategori"].fillna(DEFAULT_CATEGORY).astype(str)
    settings_df["Ek Zorunlu"] = settings_df["Ek Zorunlu"].fillna(False).astype(bool)

    edited = st.data_editor(
        settings_df,
        width="stretch",
        hide_index=True,
        column_config={
            "Metin": st.column_config.TextColumn("Metin"),
            "Kategori": st.column_config.SelectboxColumn("Kategori", options=categories),
            "Ek Zorunlu": st.column_config.CheckboxColumn("Ek Zorunlu"),
        },
        key=f"settings_editor_{selected_day_key}",
    )

    csave, _ = st.columns([2, 6])
    if csave.button("💾 Günlük satırları kaydet"):
        new_rows = []
        for _, r in edited.iterrows():
            t = "" if pd.isna(r["Metin"]) else str(r["Metin"]).strip()
            if not t:
                continue
            cat = str(r.get("Kategori") or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY
            if cat not in categories:
                cat = DEFAULT_CATEGORY
            new_rows.append({"text": t, "requires_attachment": bool(r["Ek Zorunlu"]), "category": cat})
        cfg["days"][selected_day_key] = new_rows
        atomic_save_json(CONFIG_FILE, cfg)
        st.success("Kaydedildi ✅")
        st.rerun()

    st.divider()
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
            cfg["days"][selected_day_key].append({"text": t, "requires_attachment": bool(new_req), "category": new_cat2})
            atomic_save_json(CONFIG_FILE, cfg)
            st.success("Satır eklendi ✅")
            st.rerun()

    st.caption("İpucu: Değişken placeholder `{{Kampanya}}` gibi. Değişken kategorisi satır kategorisiyle aynı olmalı.")

    st.divider()
    st.subheader("Değişkenler")

    existing_vars = sorted(list(cfg.get("variables", {}).keys()))
    pick = st.selectbox("Düzenlemek için mevcut değişken seç (opsiyonel)", options=["(Yeni)"] + existing_vars, key="var_pick")

    if pick != "(Yeni)":
        vdef = cfg["variables"].get(pick, {})
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
            cfg["variables"][name] = {"category": var_cat, "options": options}
            atomic_save_json(CONFIG_FILE, cfg)
            st.success(f"Kaydedildi: {name} ✅")
            st.rerun()

    if bB.button("🗑️ Sil", disabled=(pick == "(Yeni)"), key="var_del"):
        cfg["variables"].pop(pick, None)
        atomic_save_json(CONFIG_FILE, cfg)
        st.success("Silindi ✅")
        st.rerun()

    st.divider()
    st.subheader("Ek Presetleri (Lightshot URL)")

    existing_atts = sorted(list(cfg.get("attachments", {}).keys()))
    apick = st.selectbox("Düzenlemek için mevcut preset seç (opsiyonel)", options=["(Yeni)"] + existing_atts, key="att_pick")

    if apick != "(Yeni)":
        adef = cfg["attachments"].get(apick, {})
        default_att_name = apick
        default_att_cat = adef.get("category", DEFAULT_CATEGORY) if isinstance(adef, dict) else DEFAULT_CATEGORY
        default_att_url = adef.get("url", "") if isinstance(adef, dict) else ""
    else:
        default_att_name, default_att_cat, default_att_url = "", DEFAULT_CATEGORY, ""

    a1, a2, a3 = st.columns([2, 2, 5])
    att_name = a1.text_input("Ek Adı", value=default_att_name, placeholder="Limitli Satış Görseli", key="att_name")
    att_cat = a2.selectbox("Kategori", options=categories, index=categories.index(default_att_cat) if default_att_cat in categories else 0, key="att_cat")
    att_url = a3.text_input("Lightshot / prnt.sc URL", value=default_att_url, placeholder="https://prnt.sc/xxxxxxx", key="att_url")

    xA, xB, _ = st.columns([2, 2, 6])
    if xA.button("💾 Kaydet / Güncelle", key="att_save"):
        n = (att_name or "").strip()
        u = (att_url or "").strip()
        if not n or not u:
            st.error("Ek adı ve URL zorunlu.")
        else:
            cfg["attachments"][n] = {"category": att_cat, "url": u}
            atomic_save_json(CONFIG_FILE, cfg)
            st.success("Eklendi/Güncellendi ✅")
            st.rerun()

    if xB.button("🗑️ Sil", disabled=(apick == "(Yeni)"), key="att_del"):
        cfg["attachments"].pop(apick, None)
        atomic_save_json(CONFIG_FILE, cfg)
        st.success("Silindi ✅")
        st.rerun()
