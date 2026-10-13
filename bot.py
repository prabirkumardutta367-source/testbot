import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import os
import re
import time
import html as htmlmod
import base64
from urllib.parse import urljoin
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:
    ZoneInfo = None
    class ZoneInfoNotFoundError(Exception):
        pass
from string import Template
import telebot

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8983424094:AAGJEzc1V7sPUNQuM8EJwShDJugtI4AszQM")
if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN set nahi hai!")

# TNC ka session token (HTTP Toolkit se nikala)
TNC_TOKEN = "f027ce2f781cb9b3cbe92c6928149cb6"

DATA_DIR = os.environ.get("DATA_DIR", ".")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = "."

try:
    telebot.apihelper.CONNECT_TIMEOUT = 15
    telebot.apihelper.READ_TIMEOUT = 120
    telebot.apihelper.SESSION_TIME_TO_LIVE = 600
except Exception:
    pass

bot = telebot.TeleBot(BOT_TOKEN)

HTTP_SESSION = requests.Session()
_retry = Retry(total=2, connect=2, read=2, backoff_factor=0.5,
               status_forcelist=[429, 500, 502, 503, 504],
               allowed_methods=frozenset(["GET", "POST"]), raise_on_status=False)
HTTP_SESSION.mount("https://", HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=10))
HTTP_SESSION.mount("http://", HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=10))

URL = "https://crm.tncnursing.in/common/"

# ============================================================
# TIMEZONE
# ============================================================
try:
    IST = ZoneInfo("Asia/Kolkata") if ZoneInfo is not None else None
except Exception:
    IST = None
if IST is None:
    IST = timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")


def now_ist():
    return datetime.now(IST)


# ============================================================
# HEADERS + PAYLOAD
# ============================================================
def build_headers(ua="Dart/3.7 (dart:io)", extra=None):
    h = {
        "user-agent": ua,
        "content-type": "application/x-www-form-urlencoded",
        "accept": "application/json",
        "accept-encoding": "gzip",
        "host": "crm.tncnursing.in"
    }
    if extra:
        h.update(extra)
    return h


def build_payload(user=None, label=None, se="fl", sch="t_la", cond=None, extra=None):
    p = {"fn": "common_fn", "se": se, "data": {"json": "*"}}
    if sch:
        p["sch"] = sch
    if cond:
        p["cond"] = cond
    if user is not None:
        p["user"] = user
    if label is not None:
        p["label"] = label
    if extra:
        p.update(extra)
    return p


# ============================================================
# GLOBAL CACHE
# ============================================================
all_tests_cache = []
TEST_CATEGORY_CACHE = {}
IMAGE_DATA_CACHE = {}
WORKING_STRATEGY = None
WORKING_HEADERS = None
WORKING_EXTRA_PAYLOAD = {}

# ============================================================
# MULTI-STRATEGY DISCOVERY (Token ko 9 tarikon se try karega)
# ============================================================
STRATEGIES = [
    ("token-as-user+Dart3.7", "Dart/3.7 (dart:io)", TNC_TOKEN, "", {}, {}),
    ("token-as-user+Dart3.9", "Dart/3.9 (dart:io)", TNC_TOKEN, "", {}, {}),
    ("token-as-user+Dart3.10", "Dart/3.10 (dart:io)", TNC_TOKEN, "", {}, {}),
    ("token-as-label+Dart3.7", "Dart/3.7 (dart:io)", "MhiG6fJRQO5ASHKSBYRh8U0JhqC4y9", TNC_TOKEN, {}, {}),
    ("token-both+old", "Dart/3.7 (dart:io)", TNC_TOKEN, "1624097755607_BCKL", {}, {}),
    ("token-as-auth-header", "Dart/3.7 (dart:io)", None, None,
     {"authorization": f"Bearer {TNC_TOKEN}"}, {}),
    ("token-as-token-header", "Dart/3.7 (dart:io)", None, None,
     {"x-auth-token": TNC_TOKEN}, {}),
    ("token-in-payload", "Dart/3.7 (dart:io)", None, None, {},
     {"token": TNC_TOKEN}),
    ("token-as-auth-payload", "Dart/3.7 (dart:io)", None, None, {},
     {"auth": TNC_TOKEN}),
    ("token-as-session", "Dart/3.7 (dart:io)", None, None, {},
     {"session": TNC_TOKEN}),
    ("token-as-user_id", "Dart/3.7 (dart:io)", None, None, {},
     {"user_id": TNC_TOKEN}),
    ("token-as-uid", "Dart/3.7 (dart:io)", None, None, {},
     {"uid": TNC_TOKEN}),
]


def try_strategy(strat):
    name, ua, user, label, extra_h, extra_p = strat
    try:
        h = build_headers(ua, extra_h)
        payload = build_payload(user=user, label=label)
        payload.update(extra_p)
        r = HTTP_SESSION.post(URL, headers=h, data={"payload": json.dumps(payload)}, timeout=20)
        print(f"[TRY] {name}: HTTP {r.status_code} | {r.text[:120]}")
        if r.status_code == 200:
            try:
                data = r.json()
                if (isinstance(data, list) and data) or (isinstance(data, dict) and data):
                    return True, h, user, label, extra_p
            except Exception:
                pass
        return False, None, None, None, None
    except Exception as e:
        print(f"[TRY] {name}: FAIL {e}")
        return False, None, None, None, None


def discover_working_strategy():
    global WORKING_STRATEGY, WORKING_HEADERS, WORKING_EXTRA_PAYLOAD
    print("=" * 60)
    print("[🔎] Discovering API strategy with TNC token...")
    print(f"[🔑] Token: {TNC_TOKEN[:8]}...{TNC_TOKEN[-4:]}")
    for s in STRATEGIES:
        ok, h, u, l, ep = try_strategy(s)
        if ok:
            WORKING_STRATEGY = {"name": s[0], "user": u, "label": l, "ua": s[1]}
            WORKING_HEADERS = h
            WORKING_EXTRA_PAYLOAD = ep or {}
            print(f"✅ Working Strategy: {s[0]}")
            print("=" * 60)
            return True
    print("⚠️ Koi strategy kaam nahi kar rahi — 401 abhi bhi")
    print("=" * 60)
    return False


def safe_post(endpoint, data, timeout=(30, 180)):
    h = WORKING_HEADERS if WORKING_HEADERS else build_headers()
    r = HTTP_SESSION.post(endpoint, headers=h, data=data, timeout=timeout)
    r.raise_for_status()
    return r


def get_effective_user_label():
    """Return (user, label) based on working strategy."""
    if WORKING_STRATEGY:
        return WORKING_STRATEGY.get("user"), WORKING_STRATEGY.get("label")
    return TNC_TOKEN, ""


# ============================================================
# IMAGE HANDLING
# ============================================================
def _looks_like_image_url(value):
    if not isinstance(value, str):
        return False
    v = htmlmod.unescape(value).replace("\\/", "/").strip()
    if v.startswith(("data:image/", "http://", "https://", "//")):
        return True
    low = v.lower().split("?")[0].split("#")[0]
    return low.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"))


def _normalise_image_url(value):
    if not isinstance(value, str):
        return None
    v = htmlmod.unescape(value).replace("\\/", "/").strip().strip('"\'')
    if v.startswith("data:image/"):
        return v
    if v.startswith("//"):
        return "https:" + v
    if v.startswith(("http://", "https://")):
        return v
    if v.startswith(("/", "uploads/", "upload/", "images/", "image/", "media/", "files/")):
        return urljoin("https://crm.tncnursing.in/", v.lstrip("/"))
    return None


def _download_image_as_data_uri(image_url):
    try:
        image_url = _normalise_image_url(image_url)
        if not image_url:
            return None
        if image_url.startswith("data:image/"):
            return image_url
        if image_url in IMAGE_DATA_CACHE:
            return IMAGE_DATA_CACHE[image_url]
        ih = {"User-Agent": "Mozilla/5.0 (Linux; Android 10)",
              "Accept": "image/*,*/*;q=0.8",
              "Referer": "https://crm.tncnursing.in/"}
        r = HTTP_SESSION.get(image_url, headers=ih, timeout=(20, 90), stream=True)
        r.raise_for_status()
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        raw = bytearray()
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                raw.extend(chunk)
                if len(raw) > 12 * 1024 * 1024:
                    return None
        if not raw or not ctype.startswith("image/"):
            return None
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(bytes(raw)))
            im.thumbnail((1600, 1600), Image.LANCZOS)
            if im.mode not in ("RGB", "L"):
                bg = Image.new("RGB", im.size, "white")
                if "A" in im.getbands():
                    bg.paste(im, mask=im.getchannel("A"))
                else:
                    bg.paste(im)
                im = bg
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=82, optimize=True)
            raw_bytes = out.getvalue()
            ctype = "image/jpeg"
        except Exception:
            raw_bytes = bytes(raw)
        data = "data:%s;base64,%s" % (ctype, base64.b64encode(raw_bytes).decode("ascii"))
        IMAGE_DATA_CACHE[image_url] = data
        return data
    except Exception:
        return None


def _extract_image_candidates(obj, key_hint=""):
    out = []
    kw = ("image", "img", "photo", "picture", "media", "attachment",
          "thumbnail", "questionimage", "question_image", "file")

    def add(v):
        if not isinstance(v, str):
            return
        v = htmlmod.unescape(v).replace("\\/", "/").strip()
        if _looks_like_image_url(v) or any(x in key_hint.lower() for x in kw):
            u = _normalise_image_url(v)
            if u and u not in out:
                out.append(u)

    def walk(x, hint=""):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, str(k).lower())
        elif isinstance(x, list):
            for v in x:
                walk(v, hint)
        elif isinstance(x, str):
            for u in re.findall(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)', x, re.I):
                add(u)
            for u in re.findall(r'https?://[^\s"\'<>]+', x, re.I):
                add(u.rstrip('),.;'))
            add(x)

    walk(obj, key_hint)
    return out


def embed_question_images(raw_obj, q_text):
    candidates = _extract_image_candidates(raw_obj)
    tags = []
    for u in candidates:
        data = _download_image_as_data_uri(u)
        if data:
            tags.append('<img src="' + data + '" alt="Q Img">')

    def rep(m):
        data = _download_image_as_data_uri(m.group(1))
        return '<img src="' + data + '" alt="Q Img">' if data else m.group(0)

    q_text = re.sub(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\'][^>]*>', rep, str(q_text), flags=re.I)
    if tags:
        q_text += '<div class="question-images">' + ''.join(tags) + '</div>'
    return q_text


# ============================================================
# HTML TEMPLATE
# ============================================================
CLASSPLUS_HTML_TEMPLATE = Template(r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${TITLE}</title>
<style>
body{font-family:Inter,system-ui,Arial;background:#F7F9FC;margin:0;padding:16px;color:#1F2937}
.card{background:#fff;border-radius:16px;padding:16px;box-shadow:0 6px 20px rgba(30,30,50,.06);margin:0 auto 12px;max-width:920px}
.q-head{display:flex;justify-content:space-between;margin-bottom:8px}
.q-num{font-weight:800;color:#243B53}
.marks{background:#16A34A;color:#fff;padding:5px 10px;border-radius:999px;font-weight:800;font-size:12px}
.q-text{background:#EAF4FF;padding:14px;border-radius:10px;margin:8px 0;font-size:18px;font-weight:700;color:#163A5F;border-left:4px solid #2563EB;line-height:1.6}
.option{background:#fff;border:1px solid #D7DEE8;border-radius:10px;padding:12px;margin:8px 0;font-weight:700;cursor:pointer;display:flex;gap:10px}
.option .label{width:34px;height:34px;border-radius:8px;background:#EEF2F7;display:flex;align-items:center;justify-content:center;font-weight:800;flex:0 0 34px}
.option.sel{background:#FFF7CC;border-color:#F0C419}
.option.correct{background:#EAF8EF;border-color:#79C98A;color:#21613A}
.option.wrong{background:#FFF0F0;border-color:#E5A3A3;color:#9B2C2C}
.scoreGrid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:10px}
.scoreMetric{padding:10px;border:1px solid #E5EAF0;border-radius:10px;font-weight:700;text-align:center}
.btn{width:100%;padding:12px;border:0;border-radius:24px;background:#2563EB;color:#fff;font-weight:800;cursor:pointer;margin-top:10px}
.btn2{background:#16A34A}
.timer{font-weight:800;color:#ef4444;font-size:18px;text-align:right}
.review-box{background:#F5F3FF;border:1px solid #DDD6FE;color:#4C1D95;padding:14px;border-radius:10px;margin-top:10px;font-weight:600;line-height:1.7}
.modebtn{padding:12px;border:1px solid #E5EAF0;background:#fff;border-radius:10px;cursor:pointer;font-weight:800;margin-right:8px;margin-top:8px}
.q-text img,.option img{max-width:100%;height:auto;border-radius:8px;display:block;margin:8px auto}
</style></head><body>
<div class="card"><div style="font-weight:800;font-size:18px">${TITLE}</div>
<div style="margin-top:6px">Batch: ${BATCH_NAME}</div>
<div>Topic: ${TOPIC}</div>
<div>By: ${EXTRACTED_BY}</div>
<div id="timer" class="timer" style="display:none;margin-top:6px">${DURATION_LABEL}</div>
<div id="modeChooser" style="margin-top:12px">
<button class="modebtn" onclick="selectMode('test')">📝 Test Mode</button>
<button class="modebtn" onclick="selectMode('practice')">🎯 Practice Mode</button></div></div>
<div id="questionArea"></div>
<div id="scorecardWrap"></div>
<script>
const Q=${QUESTIONS_JSON};const T=${DURATION_SECONDS};const POS=${POS};const NEG=${NEG};
let cur=0,ans=new Array(Q.length).fill(null),mode='',submitted=false,tstart=0,ttaken=0,tstarted=false;
function selectMode(m){mode=m;document.getElementById('modeChooser').style.display='none';
document.getElementById('timer').style.display=m==='test'?'block':'none';render(0);if(m==='test')startTimer();}
function render(i){cur=i;const q=Q[i]||{};let h='<div class="card"><div class="q-head"><div class="q-num">Q '+(i+1)+'/'+Q.length+'</div><div class="marks">+'+POS+'/-'+NEG+'</div></div>';
h+='<div class="q-text">'+(q.text||'')+'</div>';
(q.options||[]).forEach((op,j)=>{let c='option';if(ans[i]===j)c+=' sel';h+='<div class="'+c+'" onclick="ch('+j+')"><div class="label">'+String.fromCharCode(65+j)+'</div><div>'+op+'</div></div>';});
h+='<button class="btn" onclick="nx()">Next ›</button><button class="btn btn2" onclick="fin()">✓ Submit</button></div>';
document.getElementById('questionArea').innerHTML=h;}
function ch(j){if(submitted)return;ans[cur]=j;render(cur);}
function nx(){if(cur<Q.length-1)render(cur+1);else fin();}
function ft(s){s=Math.max(0,Math.floor(s||0));let h=Math.floor(s/3600),m=Math.floor((s%3600)/60),x=s%60;return(h?String(h).padStart(2,'0')+':':'')+String(m).padStart(2,'0')+':'+String(x).padStart(2,'0');}
function fin(){if(submitted)return;submitted=true;if(mode==='test'&&tstart)ttaken=Math.min(T,Math.floor((Date.now()-tstart)/1000));
let sc=0,co=0,wr=0;for(let i=0;i<Q.length;i++){if(ans[i]!==null){if(Number(ans[i])===Number(Q[i].correct_index)){sc+=POS;co++;}else{sc-=NEG;wr++;}}}
let at=co+wr,ac=at?Math.round(co/at*100):0;
let h='<div class="card"><div style="text-align:center;font-weight:800;font-size:18px">Score Card</div><div class="scoreGrid">';
h+='<div class="scoreMetric">Score<br>'+Math.max(0,sc)+'/'+Q.length+'</div>';
h+='<div class="scoreMetric">Correct<br>'+co+'</div>';
h+='<div class="scoreMetric">Wrong<br>'+wr+'</div>';
h+='<div class="scoreMetric">Skipped<br>'+(Q.length-at)+'</div>';
h+='<div class="scoreMetric">Accuracy<br>'+ac+'%</div>';
h+='<div class="scoreMetric">Time<br>'+ft(ttaken)+'</div></div>';
h+='<button class="btn" onclick="rev()">🔍 Solutions</button></div>';
document.getElementById('scorecardWrap').innerHTML=h;document.getElementById('questionArea').innerHTML='';document.getElementById('timer').style.display='none';}
function rev(){let h='';for(let i=0;i<Q.length;i++){const q=Q[i]||{};h+='<div class="card"><div class="q-num">Q '+(i+1)+'</div><div class="q-text">'+(q.text||'')+'</div>';
(q.options||[]).forEach((op,j)=>{let c='option';if(j===Number(q.correct_index))c+=' correct';if(ans[i]===j&&j!==Number(q.correct_index))c+=' wrong';h+='<div class="'+c+'"><div class="label">'+String.fromCharCode(65+j)+'</div><div>'+op+'</div></div>';});
h+='<div class="review-box"><b>Correct:</b> '+String.fromCharCode(65+Number(q.correct_index));if(ans[i]!==null)h+='<br><b>Your:</b> '+String.fromCharCode(65+Number(ans[i]));if(q.solution)h+='<br><b>Rationale:</b> '+q.solution;h+='</div></div>';}
document.getElementById('questionArea').innerHTML=h;window.scrollTo({top:0});}
function startTimer(){if(tstarted)return;tstarted=true;tstart=Date.now();setInterval(()=>{if(mode!=='test'||submitted)return;let rem=T-Math.floor((Date.now()-tstart)/1000);if(rem<=0){ttaken=T;fin();return;}document.getElementById('timer').textContent=ft(rem);},1000);}
</script></body></html>""")


# ============================================================
# CATEGORY HELPERS
# ============================================================
def _normalise_category(value):
    value = str(value or "UNCATEGORIZED").strip()
    value = re.sub(r"\s+", " ", value)
    return value.upper() if value else "UNCATEGORIZED"


def _infer_category_from_test_name(test_name):
    name = re.sub(r"\s+", " ", str(test_name or "").upper()).strip()
    patterns = [
        (r"\bRRB\b|RAILWAY", "RRB"), (r"\bSGPGI\b", "SGPGI"),
        (r"\bKGMU\b", "KGMU"), (r"\bRML\b", "RML"),
        (r"\bAIIMS\b", "AIIMS"), (r"\bNORCET\b", "NORCET"),
        (r"\bESIC\b", "ESIC"), (r"\bDSSSB\b", "DSSSB"),
        (r"\bCHO\b", "CHO"), (r"\bNHM\b", "NHM"),
        (r"\bGNM\b", "GNM"), (r"\bANM\b", "ANM"),
        (r"\bPRELIMS?\b", "PRELIMS"), (r"\bMAINS?\b", "MAINS"),
        (r"MORNING[ -]?DOSE", "MORNING DOSE"),
        (r"SUBJECT[ -]?WISE", "SUBJECT WISE"),
    ]
    for p, l in patterns:
        if re.search(p, name):
            return l
    return "UNCATEGORIZED"


def get_test_category(test_id):
    return _normalise_category(TEST_CATEGORY_CACHE.get(str(test_id), "UNCATEGORIZED"))


# ============================================================
# GET ALL TESTS
# ============================================================
def get_all_test_ids():
    global TEST_CATEGORY_CACHE

    user, label = get_effective_user_label()
    if not user:
        return []

    payload = build_payload(user=user, label=label)
    payload.update(WORKING_EXTRA_PAYLOAD)

    try:
        res = safe_post(URL, data={"payload": json.dumps(payload)}, timeout=(30, 180))
        master = res.json()
        if isinstance(master, dict):
            for k in ('data', 'result', 'response', 'list'):
                if k in master and isinstance(master[k], list):
                    master = master[k]
                    break
            else:
                return []
        if not isinstance(master, list) or not master:
            return []

        extracted = []
        TEST_CATEGORY_CACHE = {}

        for course in master:
            if not isinstance(course, dict):
                continue
            cd = course.get("json", {})
            if not isinstance(cd, dict):
                continue
            ex_in = cd.get("_ex_in", {})
            if not isinstance(ex_in, dict):
                continue
            for grp in ("_fr_ex", "_pa_ex"):
                tests = ex_in.get(grp, [])
                if not isinstance(tests, list):
                    continue
                for t in tests:
                    if isinstance(t, dict) and "row_id" in t:
                        tid = t["row_id"]
                        name = t.get("_ex_na", "Unnamed")
                        cat = _infer_category_from_test_name(name)
                        TEST_CATEGORY_CACHE[str(tid)] = cat
                        extracted.append((tid, name))

        seen = set()
        unique = []
        for tid, name in extracted:
            if tid not in seen:
                seen.add(tid)
                unique.append((tid, name))
        return unique
    except Exception as e:
        print(f"[ERR] get_all: {e}")
        return []


# ============================================================
# DOWNLOAD SINGLE TEST
# ============================================================
def download_single_test(test_id, fallback_name):
    user, label = get_effective_user_label()
    if not user:
        return None, "No token"

    payload = build_payload(
        user=user, label=label,
        se="la_ex", sch=None,
        cond=[test_id],
        extra={"freeExams": [test_id], "paidExams": []}
    )
    payload.update(WORKING_EXTRA_PAYLOAD)

    try:
        res = safe_post(URL, data={"payload": json.dumps(payload)}, timeout=(30, 180))
        resp = res.json()
        main = resp[0] if isinstance(resp, list) and resp else resp
        if not isinstance(main, dict):
            return None, "Bad response"

        test_json = main.get("json", {}) if isinstance(main.get("json"), dict) else {}
        test_name = test_json.get("_ex_na", fallback_name)

        q_ids = []
        if isinstance(main.get("qu_refid"), list):
            q_ids.extend(main["qu_refid"])
        if not q_ids and isinstance(test_json.get("_sections"), list):
            for s in test_json["_sections"]:
                if isinstance(s, dict):
                    ref = s.get("qurefid") or s.get("qu_refid") or []
                    if isinstance(ref, list):
                        q_ids.extend(ref)

        if not q_ids:
            return None, "No questions in layout"

        q_payload = build_payload(
            user=user, label=label, se="fe", sch="t_qu",
            cond={"(row_id)": q_ids}
        )
        q_payload.update(WORKING_EXTRA_PAYLOAD)

        r_q = safe_post(URL, data={"payload": json.dumps(q_payload)}, timeout=(30, 180))
        raw_q = r_q.json()
        if not isinstance(raw_q, list):
            return None, "Questions not list"

        normalized = []
        cmap = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5}

        for item in raw_q:
            try:
                if not isinstance(item, dict):
                    continue
                qc = item.get("json", {}) or {}
                qu = qc.get("_qu", {})
                if isinstance(qu, dict):
                    qt = qu.get("_qu", "No Text")
                elif isinstance(qu, str):
                    qt = qu
                else:
                    qt = "No Text"
                qt = embed_question_images(item, str(qt))

                op = qc.get("_op", {})
                opts = []
                keys = []
                if isinstance(op, dict):
                    keys = sorted(op.keys())
                    for k in keys:
                        ov = op[k]
                        opts.append(str(ov.get("_op_ti", "")) if isinstance(ov, dict) else (ov if isinstance(ov, str) else ""))
                elif isinstance(op, list):
                    keys = list(range(len(op)))
                    for ov in op:
                        opts.append(str(ov.get("_op_ti", "")) if isinstance(ov, dict) else (ov if isinstance(ov, str) else ""))

                cl = qc.get("_an", "A")
                if isinstance(cl, int):
                    ci = cl
                elif isinstance(cl, str):
                    clu = cl.strip().upper()
                    ci = cmap.get(clu, 0) if clu in cmap else (int(clu) if clu.isdigit() else 0)
                else:
                    ci = 0

                so = qc.get("_so", {})
                rat = so.get("_ti", "No details") if isinstance(so, dict) else (so if isinstance(so, str) else "No details")
                rat = embed_question_images(so if isinstance(so, dict) else {}, str(rat))

                if isinstance(op, dict):
                    opts = [embed_question_images(op.get(k, {}) if isinstance(op.get(k), dict) else {}, x)
                            for k, x in zip(keys, opts)]
                elif isinstance(op, list):
                    opts = [embed_question_images(ov if isinstance(ov, dict) else {}, x)
                            for ov, x in zip(op, opts)]

                normalized.append({"text": qt, "options": opts, "correct_index": ci, "solution": rat})
            except Exception as e:
                print(f"[WARN] Q: {e}")

        if not normalized:
            return None, "Parse fail"

        clean = "".join([c if c.isalnum() or c in "._- " else "_" for c in test_name]) + "_@krrish_bro.html"
        fpath = os.path.join(DATA_DIR, clean)
        dur = 90 * 60

        html = CLASSPLUS_HTML_TEMPLATE.substitute(
            TITLE=htmlmod.escape(test_name),
            BATCH_NAME=htmlmod.escape(test_name),
            TOPIC="TNC Test series",
            EXTRACTED_BY="[𖤍 𝚁𝙾𝙻𝙴𝚇 𖤍]",
            POS="1", NEG="0.33",
            QUESTIONS_JSON=json.dumps(normalized, ensure_ascii=False),
            DURATION_SECONDS=str(dur),
            DURATION_LABEL=str(dur // 60) + " min"
        )

        with open(fpath, "w", encoding="utf-8") as f:
            f.write(html)

        return fpath, f"OK ({len(normalized)} Qs)"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# ============================================================
# CAPTION + SEND
# ============================================================
def _esc(v, fb="-"):
    return htmlmod.escape(str(v if v not in (None, "") else fb), quote=True)


def build_caption(tid, tname):
    cat = get_test_category(tid)
    owner = "🦋⃟ᴠͥɪͣᴘͫ𓊈𖤍 𝗥𝗼𝗹𝗲𝘅 𖤍𓊉 ✰"
    dm = "🦋⃟ᴠͥɪͣᴘͫ𓊈🐉 𝗗𝗿𝗮𝗴𝗼𝗻 🐲𓊉 ✰"
    return (
        f"🦖<strong>TEST NAME :- {_esc(tname)}</strong>\n\n"
        f"<blockquote><i><strong>🌲Topic :- TNC Test series</strong></i>\n"
        f"<strong>⚙️CATEGORY :- {_esc(cat)}</strong></blockquote>\n\n"
        f"<a href=\"https://t.me/krrish_bro\"><strong>🛫OWNER :- {_esc(owner)}</strong></a>\n"
        f"\n<a href=\"https://t.me/Rolex_jaat\"><strong>🛒 DM {_esc(dm)}</strong></a>"
    )


def send_test(chat_id, tid, tname):
    fpath, status = download_single_test(tid, tname)
    if not fpath or not os.path.exists(fpath):
        return False, status
    try:
        if os.path.getsize(fpath) > 48 * 1024 * 1024:
            return False, "File too large"
        cap = build_caption(tid, tname)
        for attempt in range(3):
            try:
                with open(fpath, "rb") as doc:
                    bot.send_document(chat_id, doc, caption=cap, parse_mode="HTML", timeout=300)
                return True, "Sent"
            except Exception as e:
                if attempt < 2:
                    time.sleep(3)
                else:
                    return False, str(e)
    finally:
        try:
            os.remove(fpath)
        except Exception:
            pass


# ============================================================
# TELEGRAM COMMANDS
# ============================================================
@bot.message_handler(commands=['debug'])
def cmd_debug(message):
    lines = ["🔍 **Debug**\n"]
    lines.append(f"Strategy: `{WORKING_STRATEGY['name'] if WORKING_STRATEGY else 'NONE'}`")
    lines.append(f"Token: `{TNC_TOKEN[:8]}...{TNC_TOKEN[-4:]}`")
    lines.append(f"Tests cached: {len(all_tests_cache)}")
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    global all_tests_cache

    if WORKING_STRATEGY is None:
        bot.reply_to(message, "⏳ Loading API...")
        discover_working_strategy()

    bot.reply_to(message, "⏳ Loading tests...")
    all_tests_cache = get_all_test_ids()

    if not all_tests_cache:
        bot.send_message(message.chat.id,
            "❌ **0 tests found!**\n\n"
            "Token kaam nahi kar raha. `/debug` chalao ya naya token lao.",
            parse_mode="Markdown")
        return

    bot.send_message(message.chat.id,
        f"🤖 **Bot Ready!**\n\n"
        f"Strategy: `{WORKING_STRATEGY['name'] if WORKING_STRATEGY else 'NONE'}`\n"
        f"Total: **{len(all_tests_cache)} Tests**\n\n"
        f"**Commands:**\n"
        f"/download <no> — Test download\n"
        f"/downloadall — Sab download\n"
        f"/categories — Category list\n"
        f"/debug — Status",
        parse_mode="Markdown")


@bot.message_handler(commands=['categories'])
def cmd_cats(message):
    global all_tests_cache
    if not all_tests_cache:
        all_tests_cache = get_all_test_ids()
    if not all_tests_cache:
        bot.send_message(message.chat.id, "❌ No tests.")
        return
    groups = {}
    for tid, name in all_tests_cache:
        c = get_test_category(tid)
        groups.setdefault(c, []).append((tid, name))
    lines = ["📚 **CATEGORIES**\n"]
    for c, t in sorted(groups.items()):
        lines.append(f"🏷️ **{c}** — {len(t)}")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=['downloadall'])
def cmd_all(message):
    global all_tests_cache
    all_tests_cache = get_all_test_ids()
    if not all_tests_cache:
        bot.send_message(message.chat.id, "❌ No tests.")
        return
    total = len(all_tests_cache)
    bot.send_message(message.chat.id, f"📦 Downloading {total} tests...")
    ok = fail = 0
    for i, (tid, name) in enumerate(all_tests_cache, 1):
        bot.send_message(message.chat.id, f"📥 [{i}/{total}] {name}")
        s, st = send_test(message.chat.id, tid, name)
        if s:
            ok += 1
        else:
            fail += 1
            bot.send_message(message.chat.id, f"❌ {st}")
    bot.send_message(message.chat.id, f"✅ Done. Success: {ok}, Failed: {fail}")


@bot.message_handler(commands=['download'])
def cmd_dl(message):
    global all_tests_cache
    if not all_tests_cache:
        all_tests_cache = get_all_test_ids()
    if not all_tests_cache:
        bot.send_message(message.chat.id, "❌ No tests.")
        return
    args = message.text.replace('/download', '', 1).strip()
    if not args:
        bot.reply_to(message, "Usage: `/download 12`", parse_mode="Markdown")
        return
    try:
        for idx in [int(x.strip()) for x in args.split(",") if x.strip().isdigit()]:
            if 1 <= idx <= len(all_tests_cache):
                tid, name = all_tests_cache[idx - 1]
                bot.send_message(message.chat.id, f"📥 [{idx}] {name}")
                s, st = send_test(message.chat.id, tid, name)
                if not s:
                    bot.send_message(message.chat.id, f"❌ {st}")
            else:
                bot.send_message(message.chat.id, f"⚠️ {idx} out of range")
    except Exception as e:
        bot.send_message(message.chat.id, f"Error: {e}")


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("[🚀] Bot starting...")
    print(f"[🔑] Bot token: {'YES' if BOT_TOKEN else 'NO'}")
    print(f"[🔑] TNC token: {TNC_TOKEN[:8]}...{TNC_TOKEN[-4:]}")
    print(f"[📁] DATA_DIR: {DATA_DIR}")
    print("=" * 60)

    try:
        bot.delete_webhook(drop_pending_updates=True)
        print("[✅] Webhook deleted")
    except Exception as e:
        print(f"[⚠️] Webhook: {e}")

    try:
        discover_working_strategy()
    except Exception as e:
        print(f"[⚠️] Discovery failed: {e}")

    print("[💡] Telegram pe /start bhejo")

    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
        except Exception as e:
            print(f"[⚠️] Polling error: {e}")
            print("[⏳] 15 second me retry...")
            time.sleep(15)
