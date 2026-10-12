import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import html as htmlmod
import base64
from urllib.parse import urljoin, unquote
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
# CONFIGURATION
# ============================================================
# Railway pe BOT_TOKEN env variable se lega.
# Local testing ke liye neeche default token hai.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8983424094:AAGJEzc1V7sPUNQuM8EJwShDJugtI4AszQM")

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN set nahi hai!")

# Data directory (Railway Volume ke liye)
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

url = "https://crm.tncnursing.in/common/"
headers = {
    "user-agent": "Dart/3.7 (dart:io)",
    "content-type": "application/x-www-form-urlencoded",
    "accept": "application/json",
    "accept-encoding": "gzip",
    "host": "crm.tncnursing.in"
}


def safe_post(endpoint, data, timeout=(30, 180)):
    r = HTTP_SESSION.post(endpoint, headers=headers, data=data, timeout=timeout)
    r.raise_for_status()
    return r


# ============================================================
# GLOBAL STORAGE
# ============================================================
all_tests_cache = []
TEST_CATEGORY_CACHE = {}
TEST_UPDATED_DATE_CACHE = {}
TEST_DURATION_CACHE = {}
IMAGE_DATA_CACHE = {}


# ============================================================
# TIMEZONE
# ============================================================
try:
    IST = ZoneInfo("Asia/Kolkata") if ZoneInfo is not None else None
except (ZoneInfoNotFoundError, Exception):
    IST = None

if IST is None:
    IST = timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")


def now_ist():
    return datetime.now(IST)


# ============================================================
# IMAGE EXTRACTION
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
        ih = dict(headers)
        ih.update({
            "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": "https://crm.tncnursing.in/"
        })
        r = HTTP_SESSION.get(image_url, headers=ih, timeout=(20, 90), stream=True, allow_redirects=True)
        r.raise_for_status()
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        raw = bytearray()
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                raw.extend(chunk)
                if len(raw) > 12 * 1024 * 1024:
                    return None
        if not raw:
            return None
        if not ctype.startswith("image/"):
            low = r.url.lower().split("?")[0].split("#")[0]
            extmap = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                      ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                      ".svg": "image/svg+xml"}
            for ext, ct in extmap.items():
                if low.endswith(ext):
                    ctype = ct
                    break
        if not ctype.startswith("image/"):
            return None
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(bytes(raw)))
            if getattr(im, "is_animated", False):
                raise ValueError("animated")
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
    image_key_words = ("image", "img", "photo", "picture", "media", "attachment",
                       "thumbnail", "questionimage", "question_image", "file")

    def add(v):
        if not isinstance(v, str):
            return
        v = htmlmod.unescape(v).replace("\\/", "/").strip()
        if _looks_like_image_url(v) or any(x in key_hint.lower() for x in image_key_words):
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
            tags.append('<img src="' + data + '" alt="Question Image" loading="eager">')

    def replace_img(m):
        data = _download_image_as_data_uri(m.group(1))
        return '<img src="' + data + '" alt="Question Image" loading="eager">' if data else m.group(0)

    q_text = re.sub(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\'][^>]*>',
                    replace_img, str(q_text), flags=re.I)
    if tags:
        q_text += '<div class="question-images">' + ''.join(tags) + '</div>'
    return q_text


# ============================================================
# HTML TEMPLATE (short version - same as before)
# ============================================================
CLASSPLUS_HTML_TEMPLATE = Template(r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${TITLE}</title>
<style>
body{font-family:Inter,Segoe UI,system-ui,Arial;background:linear-gradient(135deg,#F7F9FC,#EEF2F7);margin:0;padding:18px;color:#1F2937}
.topbar{max-width:980px;margin:6px auto 12px;display:flex;flex-direction:column;gap:8px}
.testTitle{font-weight:800;font-size:20px;color:#243B53}
.modeChooser{background:#fff;border:1px solid #e7ebf0;border-radius:18px;padding:14px;box-shadow:0 6px 20px rgba(30,30,50,.06);margin-top:8px}
.chooseTitle{text-align:center;font-size:18px;font-weight:800;margin-bottom:10px}
.modebar{display:flex;gap:10px;flex-wrap:wrap}
.modebtn{padding:10px 14px;border:1px solid #e7ebf0;background:#fff;border-radius:10px;cursor:pointer;font-weight:800;flex:1}
.modebtn.active{background:#4a90e2;color:#fff}
.card{background:#fff;border-radius:16px;padding:16px;box-shadow:0 6px 20px rgba(30,30,50,.06);margin-bottom:12px;max-width:920px;margin-left:auto;margin-right:auto}
.q-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.q-num{font-weight:800;color:#243B53}
.marks{background:#16A34A;color:#fff;padding:6px 10px;border-radius:999px;font-weight:800;font-size:13px}
.q-text{background:linear-gradient(135deg,#EAF4FF,#F5F9FF);padding:14px;border-radius:10px;margin:8px 0;font-size:19px;font-weight:800;color:#163A5F;border:1px solid #C9DDF2;border-left:4px solid #2563EB;line-height:1.6}
.option{background:#fff;border:1px solid #D7DEE8;border-radius:10px;padding:12px;margin:8px 0;font-weight:800;cursor:pointer;display:flex;gap:10px}
.option .label{width:36px;height:36px;border-radius:8px;background:#EEF2F7;color:#334155;display:flex;align-items:center;justify-content:center;font-weight:700;flex:0 0 36px}
.option .body{flex:1;line-height:1.5}
.option.sel{background:#FFF7CC;border-color:#F0C419}
.option.correct{background:#EAF8EF;border-color:#79C98A;color:#21613A}
.option.wrong{background:#FFF0F0;border-color:#E5A3A3;color:#9B2C2C}
.bottom{display:flex;gap:10px;margin-top:12px}
.bottom button{flex:1;padding:11px;border-radius:999px;border:0;font-weight:800;cursor:pointer;background:#f3f4f6;color:#374151}
.scorecard{background:#fff;border-radius:16px;padding:12px;margin-bottom:12px;max-width:920px;margin-left:auto;margin-right:auto}
.scoreGrid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
.scoreMetric{padding:9px;border:1px solid #edf0f5;border-radius:12px}
.review-box{background:#F5F3FF;border:1px solid #DDD6FE;color:#4C1D95;padding:14px;border-radius:10px;margin-top:10px;font-weight:600;line-height:1.7}
.timer{font-weight:800;color:#ef4444;font-size:18px}
</style></head><body>
<div class="topbar"><div class="testTitle">${TITLE}</div>
<div style="font-size:14px;">Batch: ${BATCH_NAME}</div>
<div style="font-size:14px;">Topic: ${TOPIC}</div>
<div style="font-size:14px;">Extracted By: ${EXTRACTED_BY}</div>
<div id="timer" class="timer" style="display:none">${DURATION_LABEL}</div></div>

<div id="modeChooser" class="modeChooser"><div class="chooseTitle">Select Test Mode</div>
<div class="modebar">
<button class="modebtn" onclick="selectMode('test')">📝 Test Mode</button>
<button class="modebtn" onclick="selectMode('practice')">🎯 Practice Mode</button>
</div></div>

<div id="questionArea"></div>
<div id="scorecardWrap"></div>

<script>
const QUESTIONS=${QUESTIONS_JSON};
const SITE_DURATION=${DURATION_SECONDS};
const POS=${POS};const NEG=${NEG};
let current=0,answers=new Array(QUESTIONS.length).fill(null);
let reviewLaterFlags=new Array(QUESTIONS.length).fill(false);
let skippedFlags=new Array(QUESTIONS.length).fill(false);
let mode='',submitted=false,timerStarted=false,testStartTime=0,timeTakenSeconds=0;
const TOTAL_TIME=SITE_DURATION;

function selectMode(m){mode=m;document.getElementById('modeChooser').style.display='none';
document.getElementById('timer').style.display=m==='test'?'block':'none';
renderQuestion(0);if(m==='test')startTimer();}

function renderQuestion(i){current=i;const q=QUESTIONS[i]||{};
let h='<div class="card"><div class="q-head"><div class="q-num">Q '+(i+1)+' / '+QUESTIONS.length+'</div><div class="marks">+'+POS+' / -'+NEG+'</div></div>';
h+='<div class="q-text">'+(q.text||'')+'</div>';
(q.options||[]).forEach((op,j)=>{let cls='option';if(answers[i]===j)cls+=' sel';
h+='<div class="'+cls+'" onclick="choose('+j+')"><div class="label">'+String.fromCharCode(65+j)+'</div><div class="body">'+op+'</div></div>';});
h+='<div class="bottom"><button onclick="prevQ()">‹ Prev</button><button onclick="skipQ()">Skip</button><button onclick="nextQ()">Next ›</button></div>';
h+='<button style="width:100%;margin-top:10px;padding:12px;border:0;border-radius:24px;background:#16a34a;color:#fff;font-weight:800;cursor:pointer" onclick="finish()">✓ Submit</button></div>';
document.getElementById('questionArea').innerHTML=h;}

function choose(j){if(submitted)return;answers[current]=j;skippedFlags[current]=false;renderQuestion(current);}
function nextQ(){if(current<QUESTIONS.length-1)renderQuestion(current+1);else finish();}
function prevQ(){if(current>0)renderQuestion(current-1);}
function skipQ(){answers[current]=null;skippedFlags[current]=true;nextQ();}
function formatTime(s){s=Math.max(0,Math.floor(s||0));let h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;
return (h?String(h).padStart(2,'0')+':':'')+String(m).padStart(2,'0')+':'+String(sec).padStart(2,'0');}

function finish(){if(submitted)return;submitted=true;
if(mode==='test'&&testStartTime)timeTakenSeconds=Math.min(TOTAL_TIME,Math.floor((Date.now()-testStartTime)/1000));
let score=0,correct=0,wrong=0;
for(let i=0;i<QUESTIONS.length;i++){if(answers[i]!==null){if(Number(answers[i])===Number(QUESTIONS[i].correct_index)){score+=POS;correct++;}else{score-=NEG;wrong++;}}}
let attempted=correct+wrong;let acc=attempted?Math.round(correct/attempted*100):0;
let h='<div class="scorecard"><div style="text-align:center;font-weight:800;font-size:18px;margin-bottom:10px">Score Card</div>';
h+='<div class="scoreGrid">';
h+='<div class="scoreMetric">Score: '+Math.max(0,score)+' / '+QUESTIONS.length+'</div>';
h+='<div class="scoreMetric">Correct: '+correct+'</div>';
h+='<div class="scoreMetric">Wrong: '+wrong+'</div>';
h+='<div class="scoreMetric">Skipped: '+(QUESTIONS.length-attempted)+'</div>';
h+='<div class="scoreMetric">Accuracy: '+acc+'%</div>';
h+='<div class="scoreMetric">Time: '+formatTime(timeTakenSeconds)+'</div></div></div>';
h+='<button style="width:100%;padding:12px;border:0;border-radius:24px;background:#2563EB;color:#fff;font-weight:800;cursor:pointer" onclick="openReview()">🔍 View Solutions</button>';
document.getElementById('scorecardWrap').innerHTML=h;
document.getElementById('questionArea').innerHTML='';document.getElementById('timer').style.display='none';}

function openReview(){let h='';
for(let i=0;i<QUESTIONS.length;i++){const q=QUESTIONS[i]||{};
h+='<div class="card"><div class="q-num">Q '+(i+1)+' / '+QUESTIONS.length+'</div>';
h+='<div class="q-text">'+(q.text||'')+'</div>';
(q.options||[]).forEach((op,j)=>{let cls='option';
if(j===Number(q.correct_index))cls+=' correct';
if(answers[i]===j&&j!==Number(q.correct_index))cls+=' wrong';
h+='<div class="'+cls+'"><div class="label">'+String.fromCharCode(65+j)+'</div><div class="body">'+op+'</div></div>';});
h+='<div class="review-box"><b>Correct:</b> '+String.fromCharCode(65+Number(q.correct_index));
if(answers[i]!==null)h+='<br><b>Your Answer:</b> '+String.fromCharCode(65+Number(answers[i]));
if(q.solution)h+='<br><b>Rationale:</b> '+q.solution;
h+='</div></div>';}
document.getElementById('questionArea').innerHTML=h;window.scrollTo({top:0,behavior:'smooth'});}

function startTimer(){if(timerStarted)return;timerStarted=true;testStartTime=Date.now();
setInterval(function(){if(mode!=='test'||submitted)return;
let remain=TOTAL_TIME-Math.floor((Date.now()-testStartTime)/1000);
if(remain<=0){timeTakenSeconds=TOTAL_TIME;finish();return;}
document.getElementById('timer').textContent=formatTime(remain);},1000);}
</script></body></html>""")


# ============================================================
# CATEGORY HELPERS
# ============================================================
def _extract_test_category(test, course_data=None):
    course_data = course_data or {}
    candidates = ["category", "_category", "cat", "_cat", "cat_name", "_cat_name",
                  "category_name", "_category_name", "_ex_ca", "_ex_cat", "_type", "type",
                  "exam_category", "examCategory", "section_category", "course_category",
                  "group", "group_name", "_group", "_grp", "folder", "folder_name",
                  "collection", "collection_name", "parent_category", "parent_name"]

    def value_name(value):
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for subkey in ("name", "title", "label", "_ti", "_na", "value"):
                sub = value.get(subkey)
                if isinstance(sub, str) and sub.strip():
                    return sub.strip()
        return None

    for obj in (test, course_data):
        if isinstance(obj, dict):
            for key in candidates:
                found = value_name(obj.get(key))
                if found:
                    return found
    return None


def _normalise_category(value):
    value = str(value or "UNCATEGORIZED").strip()
    value = re.sub(r"\s+", " ", value)
    return value.upper() if value else "UNCATEGORIZED"


def _infer_category_from_test_name(test_name):
    name = re.sub(r"\s+", " ", str(test_name or "").upper()).strip()
    patterns = [
        (r"\bRRB\b|RAILWAY", "RRB"), (r"\bSGPGI\b|SGPGIMS", "SGPGI"),
        (r"\bKGMU\b", "KGMU"), (r"\bRML\b", "RML"),
        (r"\bAIIMS\b", "AIIMS"), (r"\bNORCET\b", "NORCET"),
        (r"\bESIC\b", "ESIC"), (r"\bDSSSB\b", "DSSSB"),
        (r"\bCHO\b", "CHO"), (r"\bNHM\b", "NHM"),
        (r"\bGNM\b", "GNM"), (r"\bANM\b", "ANM"),
        (r"\bPRELIMS?\b", "PRELIMS"), (r"\bMAINS?\b", "MAINS"),
        (r"MORNING[ -]?DOSE", "MORNING DOSE"),
        (r"SUBJECT[ -]?WISE", "SUBJECT WISE"),
    ]
    for pattern, label in patterns:
        if re.search(pattern, name):
            return label
    return "UNCATEGORIZED"


def get_test_category(test_id):
    return _normalise_category(TEST_CATEGORY_CACHE.get(str(test_id), "UNCATEGORIZED"))


# ============================================================
# GET ALL TESTS - WITH DEBUG PRINTS
# ============================================================
def get_all_test_ids():
    global TEST_CATEGORY_CACHE
    payload = {"fn": "common_fn", "se": "fl", "data": {"json": "*"}, "sch": "t_la"}
    try:
        res = safe_post(url, data={"payload": json.dumps(payload)}, timeout=(30, 180))
        print(f"[DEBUG] HTTP Status: {res.status_code}")
        print(f"[DEBUG] Response first 300: {res.text[:300]}")

        master_list = res.json()

        # Handle different response structures
        if isinstance(master_list, dict):
            for key in ('data', 'result', 'response', 'list'):
                if key in master_list and isinstance(master_list[key], list):
                    master_list = master_list[key]
                    break
            else:
                print(f"[ERROR] Response is dict with keys: {list(master_list.keys())}")
                return []

        if not isinstance(master_list, list):
            print(f"[ERROR] master_list type: {type(master_list)}")
            return []

        print(f"[DEBUG] master_list length: {len(master_list)}")

        if len(master_list) == 0:
            print("[ERROR] ❌ Master list khaali hai - API ne empty array diya")
            return []

        extracted_tests = []
        TEST_CATEGORY_CACHE = {}

        for course in master_list:
            if not isinstance(course, dict):
                continue
            course_data = course.get("json", {})
            if not isinstance(course_data, dict):
                continue
            course_category = _extract_test_category(course_data, {})

            ex_in = course_data.get("_ex_in", {})
            if not isinstance(ex_in, dict):
                print(f"[DEBUG] _ex_in missing/not dict. Keys: {list(course_data.keys())[:20]}")
                continue

            for group_name in ("_fr_ex", "_pa_ex"):
                tests = ex_in.get(group_name, [])
                if not isinstance(tests, list):
                    continue
                for test in tests:
                    if isinstance(test, dict) and "row_id" in test:
                        tid = test["row_id"]
                        name = test.get("_ex_na", "Unnamed Test")
                        name_cat = _infer_category_from_test_name(name)
                        cat = _normalise_category(_extract_test_category(test, {}) or course_category)
                        if name_cat != "UNCATEGORIZED":
                            cat = name_cat
                        TEST_CATEGORY_CACHE[str(tid)] = cat
                        extracted_tests.append((tid, name))

        print(f"[DEBUG] Total extracted: {len(extracted_tests)}")

        # Dedupe
        seen = set()
        unique = []
        for r_id, r_name in extracted_tests:
            if r_id not in seen:
                seen.add(r_id)
                unique.append((r_id, r_name))
        return unique
    except Exception as e:
        import traceback
        print(f"[FATAL] get_all_test_ids: {e}")
        traceback.print_exc()
        return []


# ============================================================
# DOWNLOAD SINGLE TEST - WITH ROBUST ERROR HANDLING
# ============================================================
def download_single_test(test_id, fallback_name):
    payload_layout = {
        "fn": "common_fn", "se": "la_ex", "data": {"json": "*"},
        "cond": [test_id], "freeExams": [test_id], "paidExams": [],
        "user": "MhiG6fJRQO5ASHKSBYRh8U0JhqC4y9", "label": "1624097755607_BCKL"
    }
    try:
        res = safe_post(url, data={"payload": json.dumps(payload_layout)}, timeout=(30, 180))
        resp_data = res.json()
        main_info = resp_data[0] if isinstance(resp_data, list) and resp_data else resp_data
        if not isinstance(main_info, dict):
            return None, f"Bad response type: {type(main_info)}"

        test_json = main_info.get("json", {})
        if not isinstance(test_json, dict):
            test_json = {}
        test_name = test_json.get("_ex_na", fallback_name)

        # Category
        try:
            detected = _extract_test_category(main_info, test_json)
            name_cat = _infer_category_from_test_name(test_name)
            if name_cat != "UNCATEGORIZED":
                detected = name_cat
            if detected:
                TEST_CATEGORY_CACHE[str(test_id)] = _normalise_category(detected)
        except Exception:
            pass

        # Question IDs
        q_ids = []
        if isinstance(main_info.get("qu_refid"), list):
            q_ids.extend(main_info["qu_refid"])
        if not q_ids and isinstance(test_json.get("_sections"), list):
            for sec in test_json["_sections"]:
                if isinstance(sec, dict):
                    ref = sec.get("qurefid") or sec.get("qu_refid") or []
                    if isinstance(ref, list):
                        q_ids.extend(ref)
        if not q_ids and isinstance(test_json.get("qu_refid"), list):
            q_ids.extend(test_json["qu_refid"])

        if not q_ids:
            print(f"[ERROR] No question IDs for test {test_id}")
            print(f"[DEBUG] main_info keys: {list(main_info.keys()) if isinstance(main_info, dict) else type(main_info)}")
            print(f"[DEBUG] test_json keys: {list(test_json.keys()) if isinstance(test_json, dict) else type(test_json)}")
            return None, "No questions found inside layout."

        # Fetch questions
        payload_q = {
            "fn": "common_fn", "se": "fe", "data": {"json": "*"},
            "cond": {"(row_id)": q_ids}, "sch": "t_qu"
        }
        res_qu = safe_post(url, data={"payload": json.dumps(payload_q)}, timeout=(30, 180))
        raw_q = res_qu.json()

        if not isinstance(raw_q, list):
            return None, f"Questions response not list: {type(raw_q)}"

        normalized = []
        char_map = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5}

        for idx, item in enumerate(raw_q):
            try:
                if not isinstance(item, dict):
                    continue
                q_core = item.get("json", {})
                if not isinstance(q_core, dict):
                    q_core = {}

                # Question text
                qu = q_core.get("_qu", {})
                if isinstance(qu, dict):
                    q_text = qu.get("_qu", "No Text")
                elif isinstance(qu, str):
                    q_text = qu
                else:
                    q_text = "No Text"
                q_text = str(q_text)
                q_text = embed_question_images(item, q_text)

                # Options
                op_field = q_core.get("_op", {})
                options_list = []
                option_keys = []
                if isinstance(op_field, dict):
                    option_keys = sorted(op_field.keys())
                    for k in option_keys:
                        ov = op_field[k]
                        if isinstance(ov, dict):
                            options_list.append(str(ov.get("_op_ti", "")))
                        elif isinstance(ov, str):
                            options_list.append(ov)
                        else:
                            options_list.append("")
                elif isinstance(op_field, list):
                    option_keys = list(range(len(op_field)))
                    for ov in op_field:
                        if isinstance(ov, dict):
                            options_list.append(str(ov.get("_op_ti", "")))
                        elif isinstance(ov, str):
                            options_list.append(ov)
                        else:
                            options_list.append("")

                # Correct answer
                correct_letter = q_core.get("_an", "A")
                if isinstance(correct_letter, int):
                    correct_idx = correct_letter
                elif isinstance(correct_letter, str):
                    cl = correct_letter.strip().upper()
                    correct_idx = char_map.get(cl, 0) if cl in char_map else (int(cl) if cl.isdigit() else 0)
                else:
                    correct_idx = 0

                # Rationale
                so = q_core.get("_so", {})
                if isinstance(so, dict):
                    rationale = so.get("_ti", "No details.")
                elif isinstance(so, str):
                    rationale = so
                else:
                    rationale = "No details."
                rationale = str(rationale)
                rationale = embed_question_images(so if isinstance(so, dict) else {}, rationale)

                # Embed images in options
                if isinstance(op_field, dict):
                    options_list = [
                        embed_question_images(op_field.get(k, {}) if isinstance(op_field.get(k, {}), dict) else {}, x)
                        for k, x in zip(option_keys, options_list)
                    ]
                elif isinstance(op_field, list):
                    options_list = [
                        embed_question_images(ov if isinstance(ov, dict) else {}, x)
                        for ov, x in zip(op_field, options_list)
                    ]

                normalized.append({
                    "text": q_text,
                    "options": options_list,
                    "correct_index": correct_idx,
                    "solution": rationale
                })
            except Exception as qerr:
                print(f"[WARN] Q{idx} failed: {qerr}")
                continue

        if not normalized:
            return None, "All questions failed to parse."

        clean_name = "".join([c if c.isalnum() or c in "._- " else "_" for c in test_name]) + "_@krrish_bro.html"
        filepath = os.path.join(DATA_DIR, clean_name)

        # Duration
        duration_seconds = 90 * 60
        try:
            def _dur_from_val(v, hint=""):
                if v is None or isinstance(v, bool):
                    return None
                hint = str(hint).lower()
                try:
                    if isinstance(v, (int, float)) and v > 0:
                        if "sec" in hint:
                            return int(v)
                        return int(v * 60) if v < 300 else int(v)
                    if isinstance(v, str):
                        raw = v.strip().lower()
                        m = re.fullmatch(r"(\d{1,3}):(\d{1,2})(?::(\d{1,2}))?", raw)
                        if m:
                            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3) or 0)
                        nums = re.findall(r"\d+(?:\.\d+)?", raw)
                        if nums:
                            n = float(nums[0])
                            if "hour" in raw:
                                return int(n * 3600)
                            if "sec" in raw:
                                return int(n)
                            return int(n * 60)
                except Exception:
                    return None
                return None

            def _find_dur(obj, depth=0):
                if depth > 7:
                    return None
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        lk = str(k).lower()
                        if any(x in lk for x in ("duration", "time_limit", "exam_time", "test_time")):
                            f = _dur_from_val(v, lk)
                            if f and 10 <= f <= 24 * 3600:
                                return f
                    for v in obj.values():
                        f = _find_dur(v, depth + 1)
                        if f:
                            return f
                elif isinstance(obj, list):
                    for v in obj:
                        f = _find_dur(v, depth + 1)
                        if f:
                            return f
                return None

            found = _find_dur(main_info)
            if found:
                duration_seconds = found
        except Exception:
            pass

        TEST_DURATION_CACHE[str(test_id)] = int(duration_seconds)

        html_output = CLASSPLUS_HTML_TEMPLATE.substitute(
            TITLE=htmlmod.escape(test_name),
            BATCH_NAME=htmlmod.escape(test_name),
            TOPIC="TNC Test series 12.0",
            EXTRACTED_BY="[𖤍 𝚁𝙾𝙻𝙴𝚇 𖤍]",
            POS="1", NEG="0.33",
            QUESTIONS_JSON=json.dumps(normalized, ensure_ascii=False),
            DURATION_SECONDS=str(duration_seconds),
            DURATION_LABEL=htmlmod.escape(str(duration_seconds // 60) + " min")
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_output)

        return filepath, f"Success ({len(normalized)} Qs)"
    except Exception as e:
        import traceback
        print(f"[FATAL] download_single_test({test_id}): {e}")
        traceback.print_exc()
        return None, f"{type(e).__name__}: {e}"


# ============================================================
# CAPTION + SEND
# ============================================================
def _safe_caption_text(value, fallback="-"):
    value = str(value if value not in (None, "") else fallback)
    return htmlmod.escape(value, quote=True)


def build_document_caption(test_id, test_name, auto_update=False):
    category = get_test_category(test_id)
    owner = "🦋⃟ᴠͥɪͣᴘͫ𓊈𒆜𝗥𝗼𝗹𝗲𝘅𒆜𓊉 ✰"
    dm_owner = "🦋⃟ᴠͥɪͣᴘͫ𓊈𒆜𝗗𝗿𝗮𝗴𝗼𝗻 🐉🐲𓊉 ✰"
    return (
        f"🦖<strong>TEST NAME :- {_safe_caption_text(test_name)}</strong>\n\n"
        f"<blockquote><i><strong>🌲Topic :- TNC Test series 12.0</strong></i>\n"
        f"<strong>⚙️CATEGORY :- {_safe_caption_text(category)}</strong></blockquote>\n\n"
        f"<a href=\"https://t.me/krrish_bro\"><strong>🛫OWNER :- {_safe_caption_text(owner)}</strong></a>\n"
        f"\n<a href=\"https://t.me/Rolex_jaat\"><strong>🛒 DM {_safe_caption_text(dm_owner)}</strong></a>"
    )


def send_extracted_test(chat_id, test_id, test_name, auto_update=False):
    filepath, status = download_single_test(test_id, test_name)
    if not filepath or not os.path.exists(filepath):
        return False, f"Extraction failed: {status}"
    try:
        if os.path.getsize(filepath) > 48 * 1024 * 1024:
            return False, "File too large."
        caption = build_document_caption(test_id, test_name, auto_update=auto_update)
        last_err = None
        for attempt in range(1, 4):
            try:
                with open(filepath, "rb") as doc:
                    bot.send_document(chat_id, doc, caption=caption, parse_mode="HTML", timeout=300)
                return True, "Sent"
            except Exception as e:
                last_err = e
                if attempt < 3:
                    time.sleep(3 * attempt)
        return False, f"Upload failed: {last_err}"
    finally:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass


# ============================================================
# TELEGRAM COMMANDS
# ============================================================
@bot.message_handler(commands=['debug'])
def debug_api(message):
    """Raw API response dekhne ke liye."""
    import traceback
    try:
        payload = {"fn": "common_fn", "se": "fl", "data": {"json": "*"}, "sch": "t_la"}
        r = safe_post(url, data={"payload": json.dumps(payload)}, timeout=(30, 180))
        data = r.json()

        msg = f"✅ HTTP {r.status_code}\n"
        msg += f"Type: `{type(data).__name__}`\n"
        msg += f"Length: `{len(data) if hasattr(data, '__len__') else 'N/A'}`\n\n"

        if isinstance(data, list):
            if len(data) == 0:
                msg += "❌ **EMPTY LIST** — API ne khaali array diya\n"
            else:
                msg += f"First item keys:\n`{list(data[0].keys())[:20]}`\n"
        elif isinstance(data, dict):
            msg += f"Dict keys:\n`{list(data.keys())[:20]}`\n"

        raw = json.dumps(data, ensure_ascii=False)[:900]
        msg += f"\nRaw:\n```\n{raw}\n```"
        bot.reply_to(message, msg, parse_mode="Markdown")
    except Exception as e:
        tb = traceback.format_exc()[-1200:]
        bot.reply_to(message, f"❌ `{type(e).__name__}: {e}`\n\n```\n{tb}\n```", parse_mode="Markdown")


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    global all_tests_cache
    bot.reply_to(message, "⏳ Loading test list...")
    all_tests_cache = get_all_test_ids()

    if not all_tests_cache:
        bot.send_message(message.chat.id,
            "❌ **0 Tests Found!**\n\n"
            "Iska matlab API response me kuch problem hai.\n\n"
            "👉 Abhi `/debug` command chalao aur output bhejo.",
            parse_mode="Markdown")
        return

    msg = (
        "🤖 **Bot Ready!**\n\n"
        f"Total: **{len(all_tests_cache)} Tests**\n\n"
        "📋 **Commands:**\n"
        "👉 /debug - API check\n"
        "👉 /download <S.No>\n"
        "👉 /downloadall\n"
        "👉 /categories"
    )
    bot.send_message(message.chat.id, msg, parse_mode="Markdown")


@bot.message_handler(commands=['categories'])
def list_categories(message):
    global all_tests_cache
    if not all_tests_cache:
        all_tests_cache = get_all_test_ids()
    if not all_tests_cache:
        bot.send_message(message.chat.id, "❌ No tests.")
        return
    groups = {}
    for tid, name in all_tests_cache:
        cat = get_test_category(tid)
        groups.setdefault(cat, []).append((tid, name))
    lines = ["📚 **CATEGORIES**", ""]
    for cat, tests in sorted(groups.items()):
        lines.append(f"🏷️ **{cat}** — {len(tests)} test(s)")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=['downloadall'])
def download_all(message):
    global all_tests_cache
    all_tests_cache = get_all_test_ids()
    if not all_tests_cache:
        bot.send_message(message.chat.id, "❌ No tests found. /debug chalao.")
        return
    total = len(all_tests_cache)
    bot.send_message(message.chat.id, f"📦 Downloading {total} tests...")
    success = 0
    failed = 0
    for n, (tid, name) in enumerate(all_tests_cache, 1):
        bot.send_message(message.chat.id, f"📥 [{n}/{total}] {name}")
        ok, status = send_extracted_test(message.chat.id, tid, name)
        if ok:
            success += 1
        else:
            failed += 1
            bot.send_message(message.chat.id, f"❌ {status}")
    bot.send_message(message.chat.id, f"✅ Done. Success: {success}, Failed: {failed}")


@bot.message_handler(commands=['download'])
def trigger_download(message):
    global all_tests_cache
    if not all_tests_cache:
        all_tests_cache = get_all_test_ids()
    if not all_tests_cache:
        bot.send_message(message.chat.id, "❌ No tests. /debug chalao.")
        return

    args = message.text.replace('/download', '', 1).strip()
    if not args:
        bot.reply_to(message, "Usage: `/download 12`", parse_mode="Markdown")
        return

    try:
        indices = [int(x.strip()) for x in args.split(",") if x.strip().isdigit()]
        for idx in indices:
            if 1 <= idx <= len(all_tests_cache):
                tid, name = all_tests_cache[idx - 1]
                bot.send_message(message.chat.id, f"📥 Downloading [{idx}]: {name}...")
                ok, status = send_extracted_test(message.chat.id, tid, name)
                if not ok:
                    bot.send_message(message.chat.id, f"❌ {status}")
            else:
                bot.send_message(message.chat.id, f"⚠️ {idx} out of range.")
    except Exception as e:
        bot.send_message(message.chat.id, f"Error: {e}")


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=" * 50)
    print("[🚀] Bot started")
    print(f"[📁] DATA_DIR: {DATA_DIR}")
    print(f"[🔑] Token set: {'YES' if BOT_TOKEN else 'NO'}")
    print("=" * 50)
    print("[💡] Telegram pe /start aur /debug bhejo")
    bot.infinity_polling(skip_pending=True)
