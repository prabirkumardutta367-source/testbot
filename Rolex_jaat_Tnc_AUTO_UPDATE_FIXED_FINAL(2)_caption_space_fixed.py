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
import telebot  # PIP se 'pyTelegramBotAPI' install hona chahiye

# --- TELEGRAM CONFIGURATION ---
BOT_TOKEN = "8983424094:AAGJEzc1V7sPUNQuM8EJwShDJugtI4AszQM"
try:
    telebot.apihelper.CONNECT_TIMEOUT = 15
    telebot.apihelper.READ_TIMEOUT = 120
    telebot.apihelper.SESSION_TIME_TO_LIVE = 600
except Exception:
    pass
bot = telebot.TeleBot(BOT_TOKEN)

HTTP_SESSION = requests.Session()
_retry = Retry(total=2, connect=2, read=2, backoff_factor=0.5, status_forcelist=[429,500,502,503,504], allowed_methods=frozenset(["GET","POST"]), raise_on_status=False)
HTTP_SESSION.mount("https://", HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=10))
HTTP_SESSION.mount("http://", HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=10))

def safe_post(endpoint, data, timeout=(30,180)):
    r=HTTP_SESSION.post(endpoint, headers=headers, data=data, timeout=timeout)
    r.raise_for_status()
    return r

url = "https://crm.tncnursing.in/common/"
headers = {
    "user-agent": "Dart/3.7 (dart:io)",
    "content-type": "application/x-www-form-urlencoded",
    "accept": "application/json",
    "accept-encoding": "gzip",
    "host": "crm.tncnursing.in"
}

# --- GLOBAL STORAGE FOR TESTS LIST ---
all_tests_cache = []
# Test ID -> website category metadata (e.g. PRELIMS, MAINS, etc.)
TEST_CATEGORY_CACHE = {}
TEST_UPDATED_DATE_CACHE = {}  # test_id -> actual website updated date (YYYY-MM-DD)
TEST_DURATION_CACHE = {}  # test_id -> duration seconds fetched from website response

# --- ROBUST QUESTION IMAGE EXTRACTION ---
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

IMAGE_DATA_CACHE = {}

def _download_image_as_data_uri(image_url):
    try:
        image_url=_normalise_image_url(image_url)
        if not image_url: return None
        if image_url.startswith("data:image/"): return image_url
        if image_url in IMAGE_DATA_CACHE: return IMAGE_DATA_CACHE[image_url]
        ih=dict(headers)
        ih.update({"User-Agent":"Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36","Accept":"image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8","Referer":"https://crm.tncnursing.in/"})
        r=HTTP_SESSION.get(image_url,headers=ih,timeout=(20,90),stream=True,allow_redirects=True)
        r.raise_for_status()
        ctype=(r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        raw=bytearray()
        for chunk in r.iter_content(chunk_size=64*1024):
            if chunk:
                raw.extend(chunk)
                if len(raw)>12*1024*1024: return None
        if not raw: return None
        if not ctype.startswith("image/"):
            low=r.url.lower().split("?")[0].split("#")[0]
            extmap={".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",".gif":"image/gif",".webp":"image/webp",".bmp":"image/bmp",".svg":"image/svg+xml"}
            for ext,ct in extmap.items():
                if low.endswith(ext): ctype=ct; break
        if not ctype.startswith("image/"): return None
        try:
            from PIL import Image
            import io
            im=Image.open(io.BytesIO(bytes(raw)))
            if getattr(im,"is_animated",False): raise ValueError("animated")
            im.thumbnail((1600,1600),Image.LANCZOS)
            if im.mode not in ("RGB","L"):
                bg=Image.new("RGB",im.size,"white")
                if "A" in im.getbands(): bg.paste(im,mask=im.getchannel("A"))
                else: bg.paste(im)
                im=bg
            out=io.BytesIO(); im.save(out,format="JPEG",quality=82,optimize=True)
            raw_bytes=out.getvalue(); ctype="image/jpeg"
        except Exception:
            raw_bytes=bytes(raw)
        data="data:%s;base64,%s"%(ctype,base64.b64encode(raw_bytes).decode("ascii"))
        IMAGE_DATA_CACHE[image_url]=data
        return data
    except Exception:
        return None

def _extract_image_candidates(obj, key_hint=""):
    out=[]
    image_key_words=("image","img","photo","picture","media","attachment","thumbnail","questionimage","question_image","file")
    def add(v):
        if not isinstance(v,str): return
        v=htmlmod.unescape(v).replace("\\/", "/").strip()
        if _looks_like_image_url(v) or any(x in key_hint.lower() for x in image_key_words):
            u=_normalise_image_url(v)
            if u and u not in out: out.append(u)
    def walk(x, hint=""):
        if isinstance(x, dict):
            for k,v in x.items():
                kh=str(k).lower()
                walk(v, kh)
        elif isinstance(x, list):
            for v in x: walk(v, hint)
        elif isinstance(x, str):
            # Existing HTML img tags
            for u in re.findall(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)', x, re.I): add(u)
            # Any URL in a value, especially media/image fields
            for u in re.findall(r'https?://[^\s"\'<>]+', x, re.I): add(u.rstrip('),.;'))
            add(x)
    walk(obj, key_hint)
    return out

def embed_question_images(raw_obj, q_text):
    candidates=_extract_image_candidates(raw_obj)
    # Remove image URLs already represented inside q_text, then append fetched images.
    tags=[]
    for u in candidates:
        data=_download_image_as_data_uri(u)
        if data:
            tags.append('<img src="'+data+'" alt="Question Image" loading="eager">')
    # Also convert any literal remote <img src> already in question HTML.
    def replace_img(m):
        data=_download_image_as_data_uri(m.group(1))
        return '<img src="'+data+'" alt="Question Image" loading="eager">' if data else m.group(0)
    q_text=re.sub(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\'][^>]*>', replace_img, str(q_text), flags=re.I)
    if tags:
        q_text += '<div class="question-images">'+''.join(tags)+'</div>'
    return q_text

# --- REFERENCE-STYLE HTML TEMPLATE ---
CLASSPLUS_HTML_TEMPLATE = Template(r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${TITLE}</title>
<style>
:root{--primary:#4a90e2;--accent:#ef4444;--green:#22c55e;--red:#ef4444;--purple:#7c3aed;--muted:#6f7780;--card:#fff;--bg:#f6f8fb;--selYellow:#fff8c8;--border:#e7ebf0;--shadow:0 6px 20px rgba(30,30,50,.06);--btnGrad:linear-gradient(90deg,#ff6b6b,#ef4444)}
*{box-sizing:border-box}body{font-family:Inter,Segoe UI,system-ui,Arial;background:linear-gradient(180deg,#fbfcfd,#eef3f8);margin:0;padding:18px;color:#222}
.topbar{max-width:980px;margin:6px auto 12px;display:flex;flex-direction:column;gap:8px}.titleRow{display:flex;align-items:center;gap:12px;justify-content:space-between}.testTitle{font-weight:800;font-size:20px;color:var(--primary)}.topRight{color:var(--muted);font-size:13px}.tabs{display:flex;align-items:center;padding:6px 4px;border-radius:12px;justify-content:space-between;gap:18px}.tab{padding:10px 14px;cursor:pointer;font-weight:800;color:#444;position:relative;border-radius:8px}.tab.active{color:var(--accent)}.tab.active:after{content:'';position:absolute;left:10px;right:10px;bottom:-12px;height:4px;background:linear-gradient(90deg,#ff6b6b,#ef4444);border-radius:6px}
.modeChooser{background:#fff;border:1px solid #e7ebf0;border-radius:18px;padding:14px;box-shadow:var(--shadow);margin-top:8px}.chooseTitle{text-align:center;font-size:18px;font-weight:800;margin-bottom:10px;color:#111827}.modebar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:2px}.modebtn{padding:10px 14px;border:1px solid #e7ebf0;background:#fff;border-radius:10px;cursor:pointer;font-weight:800;flex:1}.modebtn.active{background:#4a90e2;color:#fff;border-color:#4a90e2}.modebtn.practice.active{background:#16a34a;border-color:#16a34a}.modebtn.review.active{background:#7c3aed;border-color:#7c3aed}.modeinfo{font-size:13px;color:#6f7780}
.container{max-width:980px;margin:12px auto;display:block;position:relative}.mainCol{width:100%;min-width:0}.sideCol{position:fixed;right:14px;top:100px;width:245px;max-width:calc(100vw - 28px);z-index:20;display:none}.sideCol.show{display:block}.question-nav-toggle{position:fixed;right:10px;top:50%;transform:translateY(-50%);z-index:25;padding:7px 9px;border:0;border-radius:999px;background:#4a90e2;color:#fff;font-size:12px;font-weight:800;box-shadow:0 4px 12px rgba(0,0,0,.18);cursor:pointer}.card{background:#fff;border-radius:18px;padding:16px;box-shadow:var(--shadow);margin-bottom:12px}.q-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}.q-num{font-weight:800;color:var(--primary)}.marks{background:var(--green);color:#fff;padding:6px 10px;border-radius:999px;font-weight:800;font-size:13px}.q-text{background:linear-gradient(135deg,#FFFDF5,#F7F3E8);padding:14px 15px;border-radius:10px;margin:8px 0;font-size:19px;font-weight:800;color:#5F4B32;border:1px solid #E8DFC9;line-height:1.6;box-shadow:0 3px 10px rgba(95,75,50,.06)}.options{list-style:none;padding:0;margin:8px 0}.option{color:#343434;background:#fffefa;border:1px solid #e5dfd3;border-radius:10px;padding:12px 13px;margin:8px 0;font-weight:800;line-height:1.5}.option .label{width:36px;height:36px;border-radius:8px;background:#f4f6f9;color:#444;display:flex;align-items:center;justify-content:center;font-weight:700;flex:0 0 36px}.option .body{flex:1;min-width:0;text-align:left;direction:ltr;line-height:1.5;font-weight:800}.option .body p,.option .body span,.option .body div{margin:0!important;padding:0!important;text-align:left!important}.option.sel{background:var(--selYellow);border-color:#f4eaa3}.option.correct{background:#EDF8F0;border-color:#86C99A;color:#2E6B3E}.option.correct .label{background:#D9F0DF;color:#2E6B3E}.option.wrong{background:#FFF3F1;border-color:#E5AAA1;color:#9B3A32}.option.wrong .label{background:#F8DED9;color:#9B3A32}
.summaryTitle{font-weight:800;color:var(--primary);margin-bottom:8px}.summaryGrid{display:grid;grid-template-columns:1fr;gap:10px}.metric{display:flex;gap:12px;align-items:center;padding:10px;border-radius:12px;background:#fff;box-shadow:0 10px 30px rgba(15,23,42,.03)}.metric .emoji{font-size:22px}.metric .label{color:var(--muted);font-size:13px}.metric .value{font-weight:800;color:var(--primary);font-size:18px}
.scorecard{background:#fff;border-radius:18px;padding:12px;box-shadow:var(--shadow);margin-bottom:12px}.scorecardTitle{text-align:center;font-size:19px;font-weight:800;margin:2px 0 10px}.scoreGrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.scoreMetric{display:flex;align-items:center;gap:8px;padding:9px;border:1px solid #edf0f5;border-radius:12px;background:#fff}.scoreMetric .emoji{font-size:20px}.scoreMetric .label{font-size:11px;color:#667085}.scoreMetric .value{font-size:16px;font-weight:800;color:#111827}.scoreMetric.full{grid-column:1/-1;justify-content:center}.performanceMsg{text-align:center;margin-top:10px;padding:10px 12px;border-radius:12px;background:#faf7ef;color:#66533B;font-size:15px;font-weight:800;border:1px solid #e8dfcf}.view-solutions{width:100%;margin-top:10px;padding:11px 14px;border:0;border-radius:24px;background:#ff3b45;color:#fff;font-size:14px;font-weight:800;cursor:pointer}
.practice-box,.review-box{display:none;margin-top:10px;padding:13px 14px;border-radius:10px;font-size:17px;font-weight:800;line-height:1.65}.practice-box.show,.review-box.show{display:block}.practice-box.correct{background:#ecfdf5;border:1px solid #bbf7d0}.practice-box.wrong{background:#fff0f0;border:1px solid #fecaca}.review-box{background:#f5f3ff;border:1px solid #ddd6fe;font-size:17px;font-weight:700;line-height:1.7}.review-box b{font-weight:900}.palette{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;max-height:420px;overflow-y:auto;padding-right:3px}.palette::-webkit-scrollbar{width:5px}.palette::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:8px}.p{height:38px;border:1px solid #e7ebf0;border-radius:9px;background:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;font-weight:700}.p.active{outline:2px solid var(--purple)}.p.answered{background:#ecfdf5;border-color:#bbf7d0}.p.not-answered{background:#fee2e2;border-color:#fecaca}.p.review-later{background:#fff7ed;border-color:#fb923c;color:#c2410c}.p.skipped{background:#f1f5f9;border-color:#cbd5e1;color:#64748b}.reviewLaterBtn{background:#fff7ed;color:#c2410c;border:1px solid #fb923c}.skipBtn{background:#f1f5f9;color:#475569;border:1px solid #cbd5e1}.submitTestBtn{width:100%;margin-top:10px;padding:12px;border:0;border-radius:24px;background:#16a34a;color:#fff;font-weight:800;font-size:14px;cursor:pointer}.review-filters{display:none;gap:7px;flex-wrap:wrap;margin:10px 0 12px}.review-filters.show{display:flex}.review-filter{padding:9px 12px;min-height:38px;border:1px solid #e5e7eb;border-radius:999px;background:#fff;color:#374151;font-weight:800;font-size:12px;cursor:pointer}.review-filter.active{background:#7c3aed;color:#fff;border-color:#7c3aed}.review-filter.correct.active{background:#16a34a;border-color:#16a34a}.review-filter.incorrect.active{background:#ef4444;border-color:#ef4444}.review-filter.skipped.active{background:#64748b;border-color:#64748b}.review-filter.later.active{background:#f97316;border-color:#f97316}.statrow{display:flex;align-items:center;gap:8px;margin:7px 0}.sdot{width:9px;height:9px;border-radius:50%}.sgreen{background:#22c55e}.sred{background:#ef4444}.submit{width:100%;padding:12px;border:0;border-radius:28px;background:#ef4444;color:#fff;font-weight:800;cursor:pointer}.submit.practice{background:#16a34a}.bottom{display:flex;gap:10px;margin-top:12px}.bottom button{flex:1}.btnSmall{padding:11px 12px;min-height:42px;border-radius:999px;border:0;font-weight:800;font-size:14px;cursor:pointer}.btnGhost{background:#f3f4f6;color:#374151}.hidden{display:none!important}.timer{font-weight:800;color:#ef4444;text-align:right;font-size:18px}.watermark{position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.06;background-repeat:repeat;background-position:center;background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='320' height='200'><text x='0' y='110' transform='rotate(-28 160 100)' font-family='Arial' font-size='26' font-weight='bold' fill='%23ef4444'>@rolex_jaat</text><text x='40' y='160' transform='rotate(-28 160 100)' font-family='Arial' font-size='26' font-weight='bold' fill='%234a90e2'>@krrish_bro</text></svg>")}.content{position:relative;z-index:1}
.review-vertical-enter{animation:reviewFromBottom .28s ease-out};.review-question-card{scroll-margin-top:12px}.review-static{cursor:default!important}.review-question-card .review-box{margin-top:12px}.review-vertical-exit{animation:reviewToTop .18s ease-in}@keyframes reviewFromBottom{from{transform:translateY(55px);opacity:0}to{transform:translateY(0);opacity:1}}@keyframes reviewToTop{from{transform:translateY(0);opacity:1}to{transform:translateY(-35px);opacity:0}}
@media(max-width:840px){body{padding:10px}.container{display:block}.sideCol{right:10px;top:75px;width:230px;max-width:calc(100vw - 20px)}.mainCol{width:100%}.tabs{gap:4px}.tab{padding:8px 10px}.modebtn{font-size:13px;padding:9px 7px}.q-text{font-size:18px;font-weight:800}.option{color:#343434;padding:11px}.scoreMetric .value{font-size:15px}.performanceMsg{font-size:14px}.review-box{font-size:15px}.question-nav-toggle{right:7px;padding:6px 8px;font-size:11px}}

/* ===== PREMIUM TEST UI ===== */
body{background:linear-gradient(180deg,#fbfaf7 0%,#f6f2ea 100%)}
.container{max-width:920px}
.card,.question-card,.test-card{border-radius:16px;box-shadow:0 6px 22px rgba(76,62,43,.07)}
.q-text{font-size:19px;font-weight:800;color:#5F4B32;background:linear-gradient(135deg,#FFFDF5,#F7F3E8);border:1px solid #E8DFC9;border-left:4px solid #806A48;padding:14px 15px;border-radius:12px;line-height:1.58;box-shadow:0 3px 12px rgba(95,75,50,.05)}
.option{color:#343434;background:#fffefa;border:1px solid #e5dfd3;border-radius:12px;padding:12px 13px;margin:9px 0;font-weight:800;transition:transform .15s ease,box-shadow .15s ease,border-color .15s ease,background .15s ease}
.option:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(76,62,43,.07)}
.option:active{transform:scale(.992)}
.option.selected{background:#f5efe3;border-color:#b9a887;box-shadow:0 0 0 2px rgba(128,106,72,.10)}
.option.correct{background:#EDF8F0;border-color:#86C99A;color:#2E6B3E;box-shadow:0 3px 10px rgba(46,107,62,.07)}
.option.wrong{background:#FFF3F1;border-color:#E5AAA1;color:#9B3A32;box-shadow:0 3px 10px rgba(155,58,50,.06)}
.option .label{font-weight:900}
.review-box{background:linear-gradient(135deg,#FFFDF5,#F6F1E7);border:1px solid #E4D8C3;color:#66533B;padding:15px 16px;font-size:16px;font-weight:600;line-height:1.7;box-shadow:0 3px 12px rgba(95,75,50,.06)}
.rationale-title{display:block;font-size:17px;font-weight:900;color:#806A48;margin-bottom:6px}
.progress-wrap{margin:10px 0 14px}
.progress-track{height:7px;background:#e9e3d8;border-radius:99px;overflow:hidden}
.progress-fill{height:100%;width:0;background:linear-gradient(90deg,#806A48,#A65E1F);border-radius:99px;transition:width .25s ease}
.question-count-badge{display:inline-flex;align-items:center;padding:5px 9px;border-radius:999px;background:#f3eee5;color:#806A48;border:1px solid #e2d8c8;font-size:12px;font-weight:900;margin-bottom:8px}
.btnSmall{padding:11px 12px;min-height:42px;border-radius:999px;border:0;font-weight:800;font-size:14px;cursor:pointer;transition:transform .15s ease,box-shadow .15s ease}
.btnSmall:hover{transform:translateY(-1px);box-shadow:0 4px 10px rgba(76,62,43,.08)}
.performanceMsg{text-align:center;margin-top:10px;padding:10px 12px;border-radius:12px;background:#FAF7EF;color:#66533B;font-size:15px;font-weight:800;border:1px solid #E8DFCF}


@media(max-width:600px){
  .q-text{font-size:18px;padding:13px 13px}
  .option{padding:12px 11px;margin:8px 0}
  .review-box{font-size:15px}
  .question-count-badge{font-size:11px}
  .btnSmall{min-height:44px}
}

.submitted-hide{display:none!important}.question-images{width:100%;margin:12px 0;text-align:center}.q-text img,.question-images img,.option img,.review-box img{display:block!important;max-width:100%!important;width:auto!important;height:auto!important;max-height:650px!important;margin:12px auto!important;border-radius:10px;object-fit:contain}

/* ===== LATEST COLOUR COMBINATION ===== */
body{background:linear-gradient(135deg,#F7F9FC 0%,#EEF2F7 50%,#F8FAFC 100%)!important;color:#1F2937}
.topbar .testTitle,.q-num{color:#243B53!important}
.modeChooser,.card,.scorecard,.option{background:#FFFFFF}
.q-text{background:linear-gradient(135deg,#EAF4FF 0%,#F5F9FF 100%)!important;color:#163A5F!important;border:1px solid #C9DDF2!important;border-left:4px solid #2563EB!important}
.option{color:#263238!important;border:1px solid #D7DEE8!important;background:#FFFFFF!important}
.option .label{background:#EEF2F7!important;color:#334155!important}
.option.sel{background:#FFF7CC!important;border-color:#F0C419!important}
.option.correct{background:#EAF8EF!important;border-color:#79C98A!important;color:#21613A!important}
.option.correct .label{background:#D8F1DE!important;color:#21613A!important}
.option.wrong{background:#FFF0F0!important;border-color:#E5A3A3!important;color:#9B2C2C!important}
.option.wrong .label{background:#F9DADA!important;color:#9B2C2C!important}
.marks{background:#16A34A!important}
.progress-track{background:#E2E8F0!important}
.progress-fill{background:linear-gradient(90deg,#2563EB,#0EA5E9)!important}
.question-count-badge{background:#EAF2FF!important;color:#1D4ED8!important;border-color:#C7D9F5!important}
.review-box{background:linear-gradient(135deg,#F5F3FF,#FAF8FF)!important;border-color:#DDD6FE!important;color:#4C1D95!important}
.rationale-title{color:#6D28D9!important}
.view-solutions,.submit{background:linear-gradient(135deg,#2563EB,#1D4ED8)!important}
.submit.practice,.submitTestBtn{background:linear-gradient(135deg,#16A34A,#15803D)!important}
.btnSmall[onclick="nextQ()"]{background:linear-gradient(135deg,#2563EB,#1D4ED8)!important}
.admin-contact-title{color:#1D4ED8!important}
.admin-contact{background:linear-gradient(135deg,#FFFFFF,#EFF6FF)!important;border-color:#BFDBFE!important}
.admin-btn.rolex{background:linear-gradient(135deg,#2563EB,#3B82F6)!important}
.admin-btn.krrish{background:linear-gradient(135deg,#0F766E,#14B8A6)!important}
.performanceMsg{background:#EFF6FF!important;color:#1E3A5F!important;border-color:#BFDBFE!important}

/* ADMIN CONTACT + COMPACT PREMIUM OVERRIDES */
.admin-contact{max-width:920px;margin:8px auto 0;padding:9px 10px;border-radius:13px;background:linear-gradient(135deg,#ffffff,#faf5ff);border:1px solid #ddd6fe;box-shadow:0 5px 16px rgba(76,29,149,.08);text-align:center}
.admin-contact-title{font-size:14px;font-weight:900;color:#4c1d95;margin-bottom:7px}
.admin-contact-buttons{display:flex;gap:7px}
.admin-btn{flex:1;text-decoration:none;color:#fff;padding:8px 9px;border-radius:9px;font-size:13px;font-weight:900;box-shadow:0 4px 10px rgba(0,0,0,.10)}
.admin-btn.rolex{background:linear-gradient(135deg,#7c3aed,#a855f7)}
.admin-btn.krrish{background:linear-gradient(135deg,#e11d48,#f97316)}
.card,.question-card,.test-card{border-radius:13px!important;padding:11px!important}
.q-text{padding:10px 12px!important;border-radius:9px!important}
.option{padding:9px 10px!important;margin:6px 0!important;border-radius:9px!important}
body{background:linear-gradient(135deg,#f8f7ff 0%,#fff7f8 48%,#f3f7ff 100%)!important}
@media(max-width:600px){.admin-contact{padding:8px}.admin-btn{font-size:12px;padding:8px 6px}.card,.question-card,.test-card{padding:9px!important}.option{padding:9px!important;margin:6px 0!important}.q-text{padding:10px 11px!important}}
</style></head><body><div class="watermark"></div><div class="content">
<div class="topbar"><div class="titleRow"><div><div class="testTitle">${TITLE}</div><div style="font-size:13px;margin-top:6px;font-weight:800;"><a href="https://t.me/Rolex_jaat" target="_blank" style="color:#229ED9;text-decoration:none;">💾 Backup Channel :- @Rolex_jaat</a></div><div style="font-size:14px;margin-top:6px;">Batch Name :- ${BATCH_NAME}</div><div style="font-size:14px;margin-top:4px;">Topic :- ${TOPIC}</div><div style="font-size:14px;margin-top:4px;">EXTRACTED BY :- ${EXTRACTED_BY}</div></div><div><div id="timer" class="timer" style="display:none">${DURATION_LABEL}</div><div class="topRight">Mode: <strong id="modeLabel">Select Mode</strong></div></div></div><div id="modeChooser" class="modeChooser"><div class="chooseTitle">Select Test Mode</div><div class="modebar"><button id="testModeBtn" class="modebtn" onclick="selectMode('test')">📝 Test Mode</button><button id="practiceModeBtn" class="modebtn practice" onclick="selectMode('practice')">🎯 Practice Mode</button></div><div class="modeinfo">Choose one mode. Only the selected mode will run.</div></div><div id="selectedModeBar" style="display:none"><div class="tabs"><div class="tab active" id="tabQuestions">Questions</div></div><div id="modeInfo" class="modeinfo"></div></div></div><div class="admin-contact"><div class="admin-contact-title">📞 Contact To Admin</div><div class="admin-contact-buttons"><a class="admin-btn rolex" href="https://t.me/Rolex_jaat" target="_blank">𖤍 @Rolex_jaat</a><a class="admin-btn krrish" href="https://t.me/Krrish_bro" target="_blank">𖤍 @Krrish_bro</a></div></div>
<button id="questionNavToggle" class="question-nav-toggle" onclick="toggleQuestionNav()" title="Question Navigation">☰ Q</button><div class="container"><div class="mainCol"><div id="scorecardWrap"></div><div id="questionArea"></div></div><div class="sideCol" id="questionNavPanel"><div class="card"><div class="summaryTitle">Question Navigation</div><div class="statrow"><span class="sdot sgreen"></span><b id="sAns">0</b> Answered</div><div class="statrow"><span class="sdot sred"></span><b id="sNot">0</b> Not Answered</div><div class="statrow"><span class="sdot" style="background:#f97316"></span><b id="sReview">0</b> Review Later</div><div class="statrow"><span class="sdot" style="background:#94a3b8"></span><b id="sSkip">0</b> Skipped</div><div class="palette" id="pal"></div><button id="submitBtn" class="submit" onclick="finish()" style="margin-top:14px;">✓ Submit Test</button></div></div></div></div>
<script>
const QUESTIONS=${QUESTIONS_JSON};const SITE_DURATION=${DURATION_SECONDS};const POS=${POS};const NEG=${NEG};let current=0;let answers=new Array(QUESTIONS.length).fill(null);let visited=new Array(QUESTIONS.length).fill(false);let reviewLaterFlags=new Array(QUESTIONS.length).fill(false);let skippedFlags=new Array(QUESTIONS.length).fill(false);let startTime=Date.now();let mode='test';let submitted=false;let timerStarted=false;let testStartTime=0;let timeTakenSeconds=0;const TOTAL_TIME=SITE_DURATION;
function selectMode(newMode){if(mode!=='' && mode!==null && window.modeLocked)return;mode=newMode;window.modeLocked=true;document.getElementById('modeChooser').style.display='none';document.getElementById('selectedModeBar').style.display='block';document.getElementById('modeLabel').textContent=mode==='test'?'Test Mode':'Practice Mode';document.getElementById('modeInfo').textContent=mode==='test'?'Test Mode: timer + negative marking + final scorecard.':'Practice Mode: answer select karte hi correct answer aur rationale dikhega; end par scorecard milega.';document.getElementById('timer').style.display=mode==='test'?'block':'none';document.getElementById('submitBtn').style.display='block';document.getElementById('submitBtn').textContent=mode==='practice'?'Finish Practice':'Submit Test';renderQuestion(current);if(mode==='test')startTimer();}
function renderQuestion(i){if(!QUESTIONS.length)return;current=Math.max(0,Math.min(i,QUESTIONS.length-1));visited[current]=true;const q=QUESTIONS[current]||{};let h='<div class="card"><div class="q-head"><div class="q-num">Question '+(current+1)+' of '+QUESTIONS.length+'</div><div class="marks">+'+POS+' / -'+NEG+'</div></div><div class="question-count-badge" id="questionCountBadge">Question 1 / 1</div><div class="progress-wrap"><div class="progress-track"><div class="progress-fill" id="questionProgressFill"></div></div></div><div class="q-text">'+(q.text||'')+'</div><div class="options">';(q.options||[]).forEach((op,j)=>{let cls='option';if(answers[current]===j)cls+=' sel';if(mode==='review'){if(j===q.correct_index)cls+=' correct';else if(answers[current]===j)cls+=' wrong'}h+='<div class="'+cls+'" onclick="choose('+j+')"><div class="label">'+String.fromCharCode(65+j)+'</div><div class="body">'+op+'</div></div>';});h+='</div><div id="feedback" class="practice-box"></div><div id="reviewInfo" class="review-box"></div><div class="bottom"><button class="btnSmall btnGhost" onclick="prevQ()">‹ Previous</button><button class="btnSmall skipBtn" onclick="skipQ()">⏭ Skip</button><button class="btnSmall reviewLaterBtn" onclick="reviewLater()">🔖 Review Later</button><button class="btnSmall" style="background:#4a90e2;color:#fff" onclick="nextQ()">'+(current===QUESTIONS.length-1?'Finish':'Save & Next ›')+'</button></div><button class="submitTestBtn" onclick="finish()">✓ Submit Test</button></div>';const qa=document.getElementById('questionArea');qa.innerHTML=h;if(mode==='review'){qa.classList.remove('review-vertical-enter');void qa.offsetWidth;qa.classList.add('review-vertical-enter');}const fb=document.getElementById('feedback'),ri=document.getElementById('reviewInfo');if(mode==='practice'&&answers[current]!==null){let correct=Number(answers[current])===Number(q.correct_index);fb.className='practice-box show '+(correct?'correct':'wrong');fb.innerHTML=(correct?'✅ Correct!':'❌ Incorrect. Correct answer: '+String.fromCharCode(65+Number(q.correct_index)))+(q.solution?'<div class="rationale-title">💡 Rationale</div>'+q.solution:'');}if(mode==='review'){ri.className='review-box show';ri.innerHTML='<b>Correct Answer:</b> '+String.fromCharCode(65+Number(q.correct_index))+(q.solution?'<div class="rationale-title">💡 Rationale</div>'+q.solution:'');}highlightPalette();refreshStats();}
function choose(j){if(!window.modeLocked||mode==='review'||submitted)return;answers[current]=j;skippedFlags[current]=false;reviewLaterFlags[current]=false;renderQuestion(current);}
function nextQ(){skippedFlags[current]=false;if(current<QUESTIONS.length-1)renderQuestion(current+1);else finish();}
function prevQ(){if(current>0)renderQuestion(current-1);}
function skipQ(){if(!window.modeLocked||mode==='review'||submitted)return;answers[current]=null;skippedFlags[current]=true;reviewLaterFlags[current]=false;if(current<QUESTIONS.length-1)renderQuestion(current+1);else finish();}
function reviewLater(){if(!window.modeLocked||mode==='review'||submitted)return;reviewLaterFlags[current]=true;skippedFlags[current]=false;if(current<QUESTIONS.length-1)renderQuestion(current+1);else finish();}
function buildPalette(){const p=document.getElementById('pal');p.innerHTML='';for(let i=0;i<QUESTIONS.length;i++){let d=document.createElement('div');d.className='p';d.textContent=i+1;d.onclick=()=>{if(window.modeLocked)renderQuestion(i)};p.appendChild(d);}highlightPalette();}
function highlightPalette(){const p=document.getElementById('pal').children;for(let i=0;i<p.length;i++){let c='p';if(skippedFlags[i])c+=' skipped';else if(reviewLaterFlags[i])c+=' review-later';else if(answers[i]!==null)c+=' answered';else if(visited[i])c+=' not-answered';if(i===current)c+=' active';p[i].className=c;}}
function refreshStats(){let a=answers.filter(x=>x!==null).length,na=visited.filter((v,i)=>v&&answers[i]===null&&!skippedFlags[i]&&!reviewLaterFlags[i]).length,rv=reviewLaterFlags.filter(Boolean).length,sk=skippedFlags.filter(Boolean).length;document.getElementById('sAns').textContent=a;document.getElementById('sNot').textContent=na;document.getElementById('sReview').textContent=rv;document.getElementById('sSkip').textContent=sk;let correct=0,wrong=0;for(let i=0;i<QUESTIONS.length;i++){if(answers[i]!==null){if(Number(answers[i])===Number(QUESTIONS[i].correct_index))correct++;else wrong++;}}let attempted=correct+wrong;}
function scoreData(){let score=0,correct=0,wrong=0;for(let i=0;i<QUESTIONS.length;i++){let a=answers[i],c=QUESTIONS[i].correct_index;if(a!==null){if(Number(a)===Number(c)){score+=POS;correct++;}else{score-=NEG;wrong++;}}}let total=QUESTIONS.length,attempted=correct+wrong,unattempted=total-attempted,accuracy=attempted?Math.round(correct/attempted*100):0;return{score:Math.max(0,score),rawScore:score,correct,wrong,attempted,unattempted,accuracy,total};}
function formatTime(sec){sec=Math.max(0,Math.floor(sec||0));let h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60),s=sec%60;return (h?String(h).padStart(2,'0')+':':'')+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');}function cardHTML(d,title){return '<div class="scorecard"><div class="scorecardTitle">'+title+'</div><div class="scoreGrid"><div class="scoreMetric"><div class="emoji">🌞</div><div><div class="label">Score</div><div class="value">'+d.score+' / '+d.total+'</div></div></div><div class="scoreMetric"><div class="emoji">✅</div><div><div class="label">Correct</div><div class="value">'+d.correct+' / '+d.total+'</div></div></div><div class="scoreMetric"><div class="emoji">❌</div><div><div class="label">Incorrect</div><div class="value">'+d.wrong+' / '+d.total+'</div></div></div><div class="scoreMetric"><div class="emoji">📝</div><div><div class="label">Attempted</div><div class="value">'+d.attempted+' / '+d.total+'</div></div></div><div class="scoreMetric"><div class="emoji">📭</div><div><div class="label">Unattempted</div><div class="value">'+d.unattempted+' / '+d.total+'</div></div></div><div class="scoreMetric"><div class="emoji">🎯</div><div><div class="label">Accuracy</div><div class="value">'+d.accuracy+'%</div></div></div><div class="scoreMetric full"><div class="emoji">🚦</div><div><div class="label">Skipped</div><div class="value">'+d.unattempted+' / '+d.total+'</div></div></div><div class="scoreMetric full"><div class="emoji">⏱️</div><div><div class="label">Time Taken</div><div class="value">'+formatTime(timeTakenSeconds)+'</div></div></div></div><div class="performanceMsg">'+performanceMessage(d)+'</div></div>';}

function performanceMessage(d){if(d.accuracy>=85)return '🏆 Excellent Performance — Keep it up!';if(d.accuracy>=70)return '👏 Good Performance — A little more revision can make it stronger.';if(d.accuracy>=50)return '📚 Good Attempt — Focus on the incorrect questions for improvement.';return '💪 Keep Practicing — Review the solutions and try again.';}
function updateReviewCounts(){let correct=0,wrong=0,skipped=0,later=0;for(let i=0;i<QUESTIONS.length;i++){let a=answers[i],c=QUESTIONS[i].correct_index;if(skippedFlags[i])skipped++;else if(reviewLaterFlags[i])later++;else if(a!==null){if(Number(a)===Number(c))correct++;else wrong++;}}let wrongCount=0;for(let i=0;i<QUESTIONS.length;i++){if(answers[i]!==null&&!skippedFlags[i]&&!reviewLaterFlags[i]&&Number(answers[i])!==Number(QUESTIONS[i].correct_index))wrongCount++;}let ids={rfAll:QUESTIONS.length,rfCorrect:correct,rfIncorrect:wrongCount,rfSkipped:skipped,rfLater:later};Object.keys(ids).forEach(id=>{let el=document.getElementById(id);if(el)el.textContent=ids[id];});}

function updatePremiumProgress(){
  const total=QUESTIONS.length||1;
  const progressCurrent=Math.min(Math.max(current+1,1),total);
  const badge=document.getElementById('questionCountBadge');
  const fill=document.getElementById('questionProgressFill');
  if(badge) badge.textContent='Question '+progressCurrent+' / '+total;
  if(fill) fill.style.width=((progressCurrent/total)*100)+'%';
}

function finish(){
// After submitting, keep the score/result visible and hide the normal question list.
// Questions will appear only when the user explicitly opens Review Mode.
setTimeout(function(){
  ['questionArea','questionsArea','quizArea','testArea','questionContainer'].forEach(function(id){
    var el=document.getElementById(id);
    if(el) el.style.display='none';
  });
  document.querySelectorAll('.question-card,.q-card,.question-list,.questions-list').forEach(function(el){
    el.style.display='none';
  });
},50);
if(submitted)return;if(mode==='test'&&testStartTime){timeTakenSeconds=Math.min(TOTAL_TIME,Math.max(0,Math.floor((Date.now()-testStartTime)/1000)));}let d=scoreData();document.getElementById('scorecardWrap').innerHTML=cardHTML(d,mode==='practice'?'🎯 Practice Score Card':'📊 Test Score Card')+'<button class="view-solutions" onclick="openReview()">🔍 Review Mode / View Solutions</button><div id="reviewFilters" class="review-filters"><button class="review-filter active" onclick="setReviewFilter(\'all\',this)">All</button><button class="review-filter correct" onclick="setReviewFilter(\'correct\',this)">✅ Correct</button><button class="review-filter incorrect" onclick="setReviewFilter(\'incorrect\',this)">❌ Incorrect</button><button class="review-filter skipped" onclick="setReviewFilter(\'skipped\',this)">⏭ Skipped</button><button class="review-filter later" onclick="setReviewFilter(\'later\',this)">🔖 Review Later</button></div>';submitted=true;document.getElementById('submitBtn').style.display='none';if(mode==='test'){document.getElementById('timer').style.display='none';}window.scrollTo({top:0,behavior:'smooth'});}
let reviewFilter='all';
function toggleQuestionNav(){const panel=document.getElementById('questionNavPanel'),btn=document.getElementById('questionNavToggle');if(!panel||mode==='review')return;panel.classList.toggle('show');btn.textContent=panel.classList.contains('show')?'✕':'☰ Q';}
function setReviewFilter(filter,btn){reviewFilter=filter;document.querySelectorAll('.review-filter').forEach(b=>b.classList.remove('active'));if(btn)btn.classList.add('active');renderAllQuestionsReview();}
function renderAllQuestionsReview(){
  if(!QUESTIONS.length)return;
  const qa=document.getElementById('questionArea');
  let h='',shown=0;
  for(let qi=0;qi<QUESTIONS.length;qi++){
    const q=QUESTIONS[qi]||{};
    const answered=answers[qi]!==null;
    const isCorrect=answered && Number(answers[qi])===Number(q.correct_index);
    const isIncorrect=answered && !isCorrect;
    const isSkipped=!!skippedFlags[qi];
    const isLater=!!reviewLaterFlags[qi];
    let show=true;
    if(reviewFilter==='correct')show=isCorrect;
    else if(reviewFilter==='incorrect')show=isIncorrect;
    else if(reviewFilter==='skipped')show=isSkipped;
    else if(reviewFilter==='later')show=isLater;
    if(!show)continue;
    shown++;
    h+='<div class="card review-question-card" id="reviewQ_'+qi+'">';
    h+='<div class="q-head"><div class="q-num">Question '+(qi+1)+' of '+QUESTIONS.length+'</div><div class="marks">+'+POS+' / -'+NEG+'</div></div>';
    h+='<div class="q-text">'+(q.text||'')+'</div><div class="options">';
    (q.options||[]).forEach((op,j)=>{
      let cls='option review-static';
      if(j===Number(q.correct_index))cls+=' correct';
      if(answers[qi]===j && j!==Number(q.correct_index))cls+=' wrong';
      h+='<div class="'+cls+'"><div class="label">'+String.fromCharCode(65+j)+'</div><div class="body">'+op+'</div></div>';
    });
    h+='</div><div class="review-box show">';
    if(isSkipped)h+='⏭ <b>Skipped</b><br>';
    if(isLater)h+='🔖 <b>Review Later</b><br>';
    h+='<b>Correct Answer:</b> '+String.fromCharCode(65+Number(q.correct_index));
    if(answered)h+='<br><b>Your Answer:</b> '+String.fromCharCode(65+Number(answers[qi]))+(isCorrect?' ✅':' ❌');
    if(q.solution)h+='<br><b>Rationale:</b> '+q.solution;
    h+='</div></div>';
  }
  qa.innerHTML=shown?h:'<div class="card" style="text-align:center;font-weight:800;color:#6b7280;padding:22px;">No questions in this category.</div>';
  for(let i=0;i<QUESTIONS.length;i++)visited[i]=true;
  highlightPalette();
  refreshStats();
  window.scrollTo({top:0,behavior:'smooth'});
}
function openReview(){document.getElementById('modeLabel').textContent='Review Mode';document.getElementById('modeInfo').textContent='Review Mode: Correct, Incorrect, Skipped aur Review Later ko filter karke dekho.';mode='review';const panel=document.getElementById('questionNavPanel'),toggle=document.getElementById('questionNavToggle'),filters=document.getElementById('reviewFilters'),qa=document.getElementById('questionArea');if(panel)panel.classList.remove('show');if(toggle)toggle.style.display='none';if(filters)filters.classList.add('show');if(qa)qa.style.display='block';updateReviewCounts();reviewFilter='all';document.querySelectorAll('.review-filter').forEach((b,i)=>b.classList.toggle('active',i===0));renderAllQuestionsReview();}
function startTimer(){if(timerStarted)return;timerStarted=true;testStartTime=Date.now();startTime=testStartTime;document.getElementById('timer').textContent=formatTime(TOTAL_TIME);setInterval(function(){if(mode!=='test'||submitted)return;let remain=TOTAL_TIME-Math.floor((Date.now()-testStartTime)/1000);if(remain<=0){timeTakenSeconds=TOTAL_TIME;finish();return;}document.getElementById('timer').textContent=formatTime(remain);},1000);}
document.addEventListener('DOMContentLoaded',()=>{buildPalette();refreshStats();});
</script></body></html>""")

def _extract_test_category(test, course_data=None):
    """Extract the real website category as reliably as possible.

    The source has used several different metadata layouts, so first check
    explicit category fields, then nested category-like fields, and finally
    known category labels anywhere inside the current test/course metadata.
    """
    course_data = course_data or {}
    candidates = [
        "category", "_category", "cat", "_cat", "cat_name", "_cat_name",
        "category_name", "_category_name", "_ex_ca", "_ex_cat", "_type", "type",
        "exam_category", "examCategory", "section_category", "course_category",
        "group", "group_name", "_group", "_grp", "folder", "folder_name",
        "collection", "collection_name", "parent_category", "parent_name"
    ]

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

    # Search nested metadata, but only keys that actually look category-related.
    def walk_category_keys(obj, depth=0):
        if depth > 10:
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = str(k).lower().replace('-', '_')
                if ("category" in lk or "cat_name" in lk or "exam_category" in lk
                        or lk in {"group", "group_name", "folder", "folder_name", "collection", "collection_name"}):
                    found = value_name(v)
                    if found:
                        return found
                found = walk_category_keys(v, depth + 1)
                if found:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = walk_category_keys(v, depth + 1)
                if found:
                    return found
        return None

    found = walk_category_keys(test) or walk_category_keys(course_data)
    if found:
        return found

    # Last safe fallback: recognise the category labels themselves anywhere
    # in this test's metadata. This avoids unrelated categories from another test.
    known = [
        ("MORNING DOSE", "MORNING DOSE"),
        ("SUBJECT WISE", "SUBJECT WISE"),
        ("SUBJECT-WISE", "SUBJECT WISE"),
        ("SUBJECTWISE", "SUBJECT WISE"),
        ("PRELIMS", "PRELIMS"),
        ("MAINS", "MAINS"),
    ]
    def find_known(obj, depth=0):
        if depth > 10:
            return None
        if isinstance(obj, str):
            up = obj.upper()
            for needle, label in known:
                if needle in up:
                    return label
            return None
        if isinstance(obj, dict):
            # Prefer metadata values over arbitrary keys.
            for v in obj.values():
                found = find_known(v, depth + 1)
                if found:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = find_known(v, depth + 1)
                if found:
                    return found
        return None
    return find_known(test) or find_known(course_data)


def _normalise_category(value):
    value = str(value or "UNCATEGORIZED").strip()
    value = re.sub(r"\s+", " ", value)
    return value.upper() if value else "UNCATEGORIZED"


def _infer_category_from_test_name(test_name):
    """Infer the category directly from the test name when website metadata is missing/generic.

    Institution/exam names are checked before generic buckets so names such as
    ``RRB Staff Nurse``, ``SGPGI Nursing Officer``, ``KGMU`` and ``RML`` get
    their own category instead of falling back to UNCATEGORIZED.
    """
    name = re.sub(r"\s+", " ", str(test_name or "").upper()).strip()

    # Specific exam / recruitment / institute categories. Keep this list broad
    # because test names on the source platform use many spellings.
    institution_patterns = [
        (r"\bRRB\b|RAILWAY RECRUITMENT BOARD|RAILWAY STAFF NURSE|RRB NURSING", "RRB"),
        (r"\bSGPGI\b|SANJAY GANDHI POSTGRADUATE|SGPGIMS", "SGPGI"),
        (r"\bKGMU\b|KING GEORGE(?:'S)? MEDICAL UNIVERSITY", "KGMU"),
        (r"\bRML\b|\bRMLH\b|RAM MANOHAR LOHIA|DR\.? RAM MANOHAR LOHIA", "RML"),
        (r"\bAIIMS\b|ALL INDIA INSTITUTE OF MEDICAL SCIENCES", "AIIMS"),
        (r"\bNORCET\b", "NORCET"),
        (r"\bESIC\b|EMPLOYEES'? STATE INSURANCE", "ESIC"),
        (r"\bDSSSB\b|DELHI SUBORDINATE SERVICES", "DSSSB"),
        (r"\bJIPMER\b", "JIPMER"),
        (r"\bPGIMER\b", "PGIMER"),
        (r"\bPGI\b CHANDIGARH", "PGIMER"),
        (r"\bNIMHANS\b", "NIMHANS"),
        (r"\bJIPMER\b", "JIPMER"),
        (r"\bDNB\b", "DNB"),
        (r"\bCHO\b|COMMUNITY HEALTH OFFICER", "CHO"),
        (r"\bAIIMS NORCET\b", "NORCET"),
        (r"\bUPPSC\b", "UPPSC"),
        (r"\bUPSSSC\b", "UPSSSC"),
        (r"\bBPSC\b", "BPSC"),
        (r"\bRSSB\b|\bRSMSSB\b", "RSSB/RSMSSB"),
        (r"\bCET\b", "CET"),
        (r"\bNHM\b", "NHM"),
        (r"\bNHS\b", "NHS"),
        (r"\bNURSING OFFICER\b", "NURSING OFFICER"),
        (r"\bSTAFF NURSE\b", "STAFF NURSE"),
        (r"\bGNM\b", "GNM"),
        (r"\bANM\b", "ANM"),
    ]
    for pattern, label in institution_patterns:
        if re.search(pattern, name):
            return label

    # Generic source/platform categories.
    generic_patterns = [
        (r"\bPRELIMS?\b|PRELIMINARY", "PRELIMS"),
        (r"\bMAINS?\b|MAIN EXAM", "MAINS"),
        (r"MORNING[ -]?DOSE", "MORNING DOSE"),
        (r"SUBJECT[ -]?WISE|SUBJECTWISE", "SUBJECT WISE"),
        (r"\bMOCK(?: TEST)?\b|MOCK TEST", "MOCK TEST"),
        (r"\bPYQ\b|PREVIOUS YEAR|PREVIOUS YEARS", "PYQ"),
        (r"\bPRACTICE\b", "PRACTICE"),
        (r"\bTEST SERIES\b", "TEST SERIES"),
    ]
    for pattern, label in generic_patterns:
        if re.search(pattern, name):
            return label
    return "UNCATEGORIZED"


def get_all_test_ids():
    global TEST_CATEGORY_CACHE, TEST_UPDATED_DATE_CACHE
    payload = {"fn": "common_fn", "se": "fl", "data": {"json": "*"}, "sch": "t_la"}
    try:
        res = safe_post(url, data={"payload": json.dumps(payload)}, timeout=(30,180))
        master_list = res.json()
        extracted_tests = []
        TEST_CATEGORY_CACHE = {}
        for course in master_list:
            course_data = course.get("json", {})
            course_category = _extract_test_category(course_data, {})
            for test in course_data.get("_ex_in", {}).get("_fr_ex", []):
                if isinstance(test, dict) and "row_id" in test:
                    tid = test["row_id"]
                    name = test.get("_ex_na", "Unnamed Free Test")
                    source_updated_date = _find_source_updated_date(test)
                    if source_updated_date:
                        TEST_UPDATED_DATE_CACHE[str(tid)] = source_updated_date
                    # Prefer a specific category inferred from the test name.
                    # This keeps RRB/SGPGI/KGMU/RML/etc. correct even when the
                    # master response contains a generic course-level category.
                    name_cat = _infer_category_from_test_name(name)
                    cat = _normalise_category(_extract_test_category(test, {}) or course_category)
                    if name_cat != "UNCATEGORIZED":
                        cat = name_cat
                    elif cat == "UNCATEGORIZED":
                        cat = _infer_category_from_test_name(name)
                    TEST_CATEGORY_CACHE[str(tid)] = cat
                    extracted_tests.append((tid, name))
            for test in course_data.get("_ex_in", {}).get("_pa_ex", []):
                if isinstance(test, dict) and "row_id" in test:
                    tid = test["row_id"]
                    name = test.get("_ex_na", "Unnamed Paid Test")
                    source_updated_date = _find_source_updated_date(test)
                    if source_updated_date:
                        TEST_UPDATED_DATE_CACHE[str(tid)] = source_updated_date
                    # Prefer a specific category inferred from the test name.
                    # This keeps RRB/SGPGI/KGMU/RML/etc. correct even when the
                    # master response contains a generic course-level category.
                    name_cat = _infer_category_from_test_name(name)
                    cat = _normalise_category(_extract_test_category(test, {}) or course_category)
                    if name_cat != "UNCATEGORIZED":
                        cat = name_cat
                    elif cat == "UNCATEGORIZED":
                        cat = _infer_category_from_test_name(name)
                    TEST_CATEGORY_CACHE[str(tid)] = cat
                    extracted_tests.append((tid, name))
        seen = set()
        unique_tests = []
        for r_id, r_name in extracted_tests:
            if r_id not in seen:
                seen.add(r_id)
                unique_tests.append((r_id, r_name))
        return unique_tests
    except Exception:
        return []

def format_duration_label(seconds):
    """Human-readable timing shown in the extracted test header."""
    try:
        seconds = int(seconds)
    except Exception:
        return "Timing unavailable"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} hr"
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds // 60} min {seconds % 60} sec"


def _parse_source_date(value):
    """Parse common website date/timestamp formats into IST YYYY-MM-DD."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            v = float(value)
            # Unix milliseconds / seconds. Ignore implausibly small values.
            if v > 10_000_000_000:
                v /= 1000.0
            if v > 1_000_000_000:
                return datetime.fromtimestamp(v, tz=timezone.utc).astimezone(IST).strftime("%Y-%m-%d")

        text = str(value).strip()
        if not text:
            return None

        # Numeric timestamps sometimes arrive as strings.
        if re.fullmatch(r"\d{10,13}(?:\.\d+)?", text):
            v = float(text)
            if v > 10_000_000_000:
                v /= 1000.0
            if v > 1_000_000_000:
                return datetime.fromtimestamp(v, tz=timezone.utc).astimezone(IST).strftime("%Y-%m-%d")

        # ISO / RFC-like datetime strings.
        iso = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            return dt.astimezone(IST).strftime("%Y-%m-%d")
        except Exception:
            pass

        # Common human-readable forms used by web APIs/admin panels.
        formats = (
            "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
            "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
            "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M:%S",
            "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
            "%d %b %Y %H:%M:%S", "%d %B %Y %H:%M:%S",
        )
        for fmt in formats:
            try:
                return datetime.strptime(text[:40], fmt).replace(tzinfo=IST).strftime("%Y-%m-%d")
            except Exception:
                continue

        # Date embedded in a longer value (e.g. "Updated: 25-08-2026").
        patterns = (
            r"\b(\d{4}-\d{2}-\d{2})\b",
            r"\b(\d{2}-\d{2}-\d{4})\b",
            r"\b(\d{2}/\d{2}/\d{4})\b",
            r"\b(\d{4}/\d{2}/\d{2})\b",
        )
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return _parse_source_date(m.group(1))
    except Exception:
        pass
    return None


def _find_source_updated_date(obj, path="", depth=0):
    """Find a test's actual website update/modified date from nested metadata.

    The TNC API has used several metadata names over time.  This detector is
    deliberately tolerant of names such as updatedAt, upd_date, _updt,
    lastChange, publishDate, etc., while avoiding ordinary created dates.
    """
    if depth > 14 or obj is None:
        return None

    update_tokens = (
        "updated", "update", "modified", "modify", "lastmodified",
        "lastupdate", "last_updated", "last_update", "updatedat",
        "updatedon", "updateddate", "updateat", "updatedtime",
        "upd", "updt", "up_at", "edit", "edited", "revision",
        "lastchange", "changedat", "change_date", "publish", "published",
        "publishedat", "publishdate", "release", "releasedat",
    )

    def key_norm(k):
        return re.sub(r"[^a-z0-9]", "", str(k).lower())

    def is_update_key(k):
        nk = key_norm(k)
        return any(tok in nk for tok in update_tokens)

    if isinstance(obj, dict):
        # Pass 1: explicit update-like keys in the current object.
        candidates = []
        for k, v in obj.items():
            if is_update_key(k):
                d = _parse_source_date(v)
                if d:
                    candidates.append((k, d))
        if candidates:
            # Prefer the most explicit/strongest update key.
            priority = ("updatedat", "updatedon", "updateddate", "updated",
                        "modifiedat", "modifiedon", "modified", "lastupdated",
                        "lastupdate", "lastmodified", "upd", "updt", "edit",
                        "publish", "published")
            candidates.sort(key=lambda x: next((i for i,t in enumerate(priority) if t in key_norm(x[0])), 999))
            return candidates[0][1]

        # Pass 2: some APIs use generic date/datetime/timestamp fields inside
        # a test object.  Only accept them when the surrounding path/object
        # itself strongly indicates update/change/publish metadata.
        context = key_norm(path)
        update_context = any(tok in context for tok in (
            "update", "updated", "modify", "modified", "change", "publish", "revision", "edit"
        ))
        if update_context:
            for k, v in obj.items():
                nk = key_norm(k)
                if nk in {"date", "datetime", "timestamp", "time", "dt", "at"}:
                    d = _parse_source_date(v)
                    if d:
                        return d

        # Pass 3: recurse, but do not use generic dates from unrelated fields.
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                d = _find_source_updated_date(v, f"{path}.{k}".strip("."), depth + 1)
                if d:
                    return d

    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            d = _find_source_updated_date(v, f"{path}[{i}]", depth + 1)
            if d:
                return d
    return None


def _fetch_light_test_metadata(test_id):
    """Fetch only one test's metadata (no question download)."""
    payload = {
        "fn": "common_fn", "se": "la_ex", "data": {"json": "*"},
        "cond": [test_id], "freeExams": [test_id], "paidExams": [],
        "user": "MhiG6fJRQO5ASHKSBYRh8U0JhqC4y9", "label": "1624097755607_BCKL"
    }
    try:
        res = safe_post(url, data={"payload": json.dumps(payload)}, timeout=(20,90))
        data = res.json()
        main = data[0] if isinstance(data, list) and data else data
        if not isinstance(main, dict):
            return None
        test_json = main.get("json", {}) if isinstance(main.get("json", {}), (dict, list)) else {}
        # Complete response first; then nested test JSON.
        found = _find_source_updated_date(main)
        return found or _find_source_updated_date(test_json)
    except Exception:
        return None


def refresh_source_updated_dates(test_items, only_missing=True, max_workers=12):
    """Fetch real website update dates for tests whose master metadata lacks them.

    Only the lightweight test-detail metadata endpoint is requested. Results are
    stored in TEST_UPDATED_DATE_CACHE, so subsequent runs do not refetch dates
    that have already been discovered.
    """
    targets = []
    for tid, _name in test_items:
        sid = str(tid)
        if only_missing and TEST_UPDATED_DATE_CACHE.get(sid):
            continue
        targets.append(sid)
    if not targets:
        return 0

    found = 0
    workers = max(1, min(int(max_workers or 1), 12))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_light_test_metadata, tid): tid for tid in targets}
        for fut in as_completed(futures):
            tid = futures[fut]
            try:
                d = fut.result()
            except Exception:
                d = None
            if d:
                TEST_UPDATED_DATE_CACHE[str(tid)] = d
                found += 1
    return found

def download_single_test(test_id, fallback_name):
    payload_layout = {
        "fn": "common_fn", "se": "la_ex", "data": {"json": "*"},
        "cond": [test_id], "freeExams": [test_id], "paidExams": [],
        "user": "MhiG6fJRQO5ASHKSBYRh8U0JhqC4y9", "label": "1624097755607_BCKL"
    }
    try:
        res = safe_post(url, data={"payload": json.dumps(payload_layout)}, timeout=(30,180))
        resp_data = res.json()
        main_info = resp_data[0] if isinstance(resp_data, list) else resp_data
        test_json = main_info.get("json", {}) if isinstance(main_info, dict) else {}
        test_name = test_json.get("_ex_na", fallback_name)

        # Capture category from the same website response used to build this test.
        # This fixes captions showing UNCATEGORIZED when the master list stores the
        # category deeper inside the test metadata.
        detected_category = _extract_test_category(main_info, test_json)
        name_category = _infer_category_from_test_name(test_name)
        if name_category != "UNCATEGORIZED":
            # Test name is authoritative for the requested exam/institute buckets
            # (RRB, SGPGI, KGMU, RML, AIIMS, etc.).
            detected_category = name_category
        elif not detected_category:
            detected_category = "UNCATEGORIZED"
        if detected_category:
            TEST_CATEGORY_CACHE[str(test_id)] = _normalise_category(detected_category)

        # Capture the actual website update date when available. This is NOT the
        # bot start date. It is used by the date-wise report/last-2-days tracker.
        source_updated_date = _find_source_updated_date(main_info) or _find_source_updated_date(test_json)
        if source_updated_date:
            TEST_UPDATED_DATE_CACHE[str(test_id)] = source_updated_date

        q_ids = main_info.get("qu_refid", [])
        
        if not q_ids and "_sections" in main_info.get("json", {}):
            for sec in main_info["json"]["_sections"]:
                q_ids.extend(sec.get("qurefid", []))
                
        if not q_ids:
            return None, "Error: No questions found inside layout."
            
        payload_questions = {
            "fn": "common_fn", "se": "fe", "data": {"json": "*"},
            "cond": {"(row_id)": q_ids}, "sch": "t_qu"
        }
        res_qu = safe_post(url, data={"payload": json.dumps(payload_questions)}, timeout=(30,180))
        raw_questions_list = res_qu.json()
        
        normalized_questions = []
        char_map = {"A": 0, "B": 1, "C": 2, "D": 3}
        for item in raw_questions_list:
            q_core = item.get("json", {})
            q_text = q_core.get("_qu", {}).get("_qu", "No Text Available")
            # IMPORTANT: TNC question photographs are not necessarily inside _qu._qu.
            # Search the entire raw question object for image/media/photo fields and URLs.
            q_text = embed_question_images(item, q_text)
            options_dict = q_core.get("_op", {})
            options_list = []
            for key in sorted(options_dict.keys()):
                options_list.append(options_dict[key].get("_op_ti", ""))
            correct_letter = q_core.get("_an", "A")
            correct_idx = char_map.get(correct_letter.strip().upper(), 0)
            rationale = q_core.get("_so", {}).get("_ti", "No details uploaded.")
            rationale = embed_question_images(q_core.get("_so", {}), rationale)
            options_list = [embed_question_images(q_core.get("_op", {}).get(k, {}), x) for k, x in zip(sorted(options_dict.keys()), options_list)]

            normalized_questions.append({
                "text": q_text, "options": options_list,
                "correct_index": correct_idx, "solution": rationale
            })

        clean_name = "".join([c if c.isalnum() or c in "._- " else "_" for c in test_name]) + "_@krrish_bro.html"

        # ============================================================
        # FETCH TEST TIMING DIRECTLY FROM WEBSITE METADATA
        # ============================================================
        # Never use a fixed 90-minute timer when the source provides timing.
        # The layout response is the source of truth; we inspect its nested
        # metadata because different tests can store the duration under
        # different field names / structures.
        duration_seconds = None
        duration_source = None

        def _duration_from_value(value, key_hint=""):
            if value is None or isinstance(value, bool):
                return None
            hint = str(key_hint or "").lower()
            try:
                if isinstance(value, (int, float)) and value > 0:
                    # Fields explicitly saying seconds are seconds; duration/time
                    # fields are normally minutes on the source platform.
                    if "sec" in hint or hint.endswith("_s"):
                        return int(value)
                    return int(value * 60) if value < 300 else int(value)
                if isinstance(value, str):
                    raw = value.strip().lower()
                    if not raw:
                        return None
                    # HH:MM or HH:MM:SS
                    m = re.fullmatch(r"(\d{1,3}):(\d{1,2})(?::(\d{1,2}))?", raw)
                    if m:
                        h, mi, sec = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
                        return h * 3600 + mi * 60 + sec
                    nums = re.findall(r"\d+(?:\.\d+)?", raw)
                    if not nums:
                        return None
                    n = float(nums[0])
                    if "hour" in raw or re.search(r"\bh\b", raw):
                        return int(n * 3600)
                    if "sec" in raw or re.search(r"\bs\b", raw):
                        return int(n)
                    # Source strings such as "60 min" / "90 minutes".
                    return int(n * 60)
            except Exception:
                return None
            return None

        def _find_duration(obj, path="", depth=0):
            if depth > 7:
                return None
            if isinstance(obj, dict):
                # Prefer explicit duration/time-limit keys first.
                priority = []
                others = []
                for k, v in obj.items():
                    lk = str(k).lower()
                    if any(x in lk for x in ("duration", "time_limit", "timelimit", "exam_time", "test_time", "duration_minutes", "duration_seconds")):
                        priority.append((k, v))
                    else:
                        others.append((k, v))
                for k, v in priority + others:
                    lk = str(k).lower()
                    if any(x in lk for x in ("duration", "time_limit", "timelimit", "exam_time", "test_time")):
                        found = _duration_from_value(v, lk)
                        if found and 10 <= found <= 24 * 3600:
                            return found, (path + "." + str(k)).strip(".")
                    found = _find_duration(v, (path + "." + str(k)).strip("."), depth + 1)
                    if found:
                        return found
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    found = _find_duration(v, f"{path}[{i}]", depth + 1)
                    if found:
                        return found
            return None

        found_duration = _find_duration(main_info)
        if found_duration:
            duration_seconds, duration_source = found_duration
        else:
            # If the source does not expose timing for this particular test,
            # keep a safe fallback but clearly label it as fallback.
            duration_seconds = 90 * 60
            duration_source = "fallback (website timing not exposed)"

        # Keep the exact timing used by the generated HTML available to the
        # Telegram caption sender so every download path shows the same value.
        TEST_DURATION_CACHE[str(test_id)] = int(duration_seconds)

        html_output = CLASSPLUS_HTML_TEMPLATE.substitute(
            TITLE=htmlmod.escape(test_name),
            BATCH_NAME=htmlmod.escape(test_name),
            TOPIC="TNC NORCET 11.0",
            EXTRACTED_BY="[𖤍 𝚁𝙾𝙻𝙴𝚇 𖤍]",
            POS="1",
            NEG="0.33",
            QUESTIONS_JSON=json.dumps(normalized_questions, ensure_ascii=False),
            DURATION_SECONDS=str(duration_seconds),
            DURATION_LABEL=htmlmod.escape(format_duration_label(duration_seconds))
        )

        # Pydroid ki local working directory me save karna
        with open(clean_name, "w", encoding="utf-8") as f:
            f.write(html_output)

        return clean_name, f"Success ({len(q_ids)} Qs)"
    except Exception as e:
        return None, str(e)


# ============================================================
# NEW TEST TRACKER + AUTOMATIC UPDATE ENGINE
# ============================================================
SEEN_TESTS_FILE = "seen_tests.json"
TEST_REGISTRY_FILE = "test_registry.json"
# Pydroid/Android may not have the IANA tzdata package installed.
# Use Asia/Kolkata from ZoneInfo when available; otherwise use the fixed
# IST offset (+05:30), which avoids the tzdata dependency completely.
try:
    IST = ZoneInfo("Asia/Kolkata") if ZoneInfo is not None else None
except (ZoneInfoNotFoundError, Exception):
    IST = None

if IST is None:
    IST = timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")
AUTO_CHATS_FILE = "auto_update_chats.json"
AUTO_UPDATE_INTERVAL = 300
AUTO_UPDATE_ENABLED = True
AUTO_UPDATE_LOCK = threading.Lock()
AUTO_UPDATE_THREAD = None

def now_ist():
    return datetime.now(IST)

def test_registry_key(test_id):
    return str(test_id)

def load_test_registry():
    try:
        if os.path.exists(TEST_REGISTRY_FILE):
            with open(TEST_REGISTRY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}

def save_test_registry(registry):
    tmp = TEST_REGISTRY_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(registry, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TEST_REGISTRY_FILE)
        return True
    except Exception:
        try:
            if os.path.exists(tmp): os.remove(tmp)
        except Exception: pass
        return False

def sync_test_registry(current_tests, baseline=False):
    """Keep SN plus the test's actual website update date (not bot-start date)."""
    registry = load_test_registry()
    changed = False
    used_sns = []
    for item in registry.values():
        try: used_sns.append(int(item.get("sn", 0)))
        except Exception: pass
    next_sn = max(used_sns, default=0) + 1
    now = now_ist()
    for tid, name in current_tests:
        key = test_registry_key(tid)
        source_date = TEST_UPDATED_DATE_CACHE.get(str(tid))
        if key not in registry:
            registry[key] = {
                "sn": next_sn,
                "test_id": str(tid),
                "name": name,
                "first_seen": None if baseline else now.isoformat(),
                "first_seen_date": None if baseline else now.strftime("%Y-%m-%d"),
                "updated_date": source_date,
                "baseline": False if source_date else bool(baseline)
            }
            next_sn += 1
            changed = True
        else:
            if name and registry[key].get("name") != name:
                registry[key]["name"] = name
                changed = True
            if source_date:
                if registry[key].get("updated_date") != source_date:
                    registry[key]["updated_date"] = source_date
                    changed = True
                if registry[key].get("baseline"):
                    registry[key]["baseline"] = False
                    changed = True
    if changed:
        save_test_registry(registry)
    return registry

def get_test_record(test_id, fallback_name=""):
    reg = load_test_registry()
    rec = reg.get(test_registry_key(test_id))
    if rec:
        return rec
    return {"sn": "-", "test_id": str(test_id), "name": fallback_name, "first_seen_date": None, "updated_date": None, "first_seen": None, "baseline": False}

def format_test_line(tid, name, registry=None, marker=""):
    registry = registry or load_test_registry()
    rec = registry.get(test_registry_key(tid), {})
    sn = rec.get("sn", "-")
    date = rec.get("updated_date") or rec.get("first_seen_date") or "old"
    return f"**SN {sn}** {marker}{name}  |  ID: `{tid}`  |  {date}\n"

def report_for_date(date_text=None):
    """Report tests by ACTUAL website updated date. No bot-start-date guessing."""
    if not date_text or date_text.lower() in ("today", "aaj"):
        date_text = now_ist().strftime("%Y-%m-%d")
    reg = load_test_registry()
    rows = []
    for rec in reg.values():
        d = rec.get("updated_date")
        if d == date_text and not rec.get("baseline"):
            rows.append(rec)
    rows.sort(key=lambda x: int(x.get("sn", 0)))
    return date_text, rows

def report_last_2_days_text():
    """Show only tests updated today and yesterday according to source metadata."""
    today = now_ist().date()
    reg = load_test_registry()
    groups = {}
    for rec in reg.values():
        raw = rec.get("updated_date")
        try:
            d = datetime.strptime(str(raw), "%Y-%m-%d").date()
        except Exception:
            continue
        delta = (today - d).days
        if 0 <= delta <= 1:
            groups.setdefault(str(d), []).append(rec)
    if not groups:
        return "📊 **LAST 2 DAYS — UPDATED TEST REPORT**\n\n❌ Aaj ya kal website par koi updated test nahi mila."
    lines = ["📊 **LAST 2 DAYS — UPDATED TEST REPORT**", ""]
    for d in sorted(groups.keys(), reverse=True):
        rows = sorted(groups[d], key=lambda x: int(x.get("sn", 0)))
        lines.append(f"📅 **{d} — {len(rows)} test(s)**")
        for rec in rows:
            lines.append(f"• **SN {rec.get('sn','-')}** — {rec.get('name','Unnamed Test')}")
        lines.append("")
    return "\n".join(lines).rstrip()

def report_text(date_text=None):
    date_text, rows = report_for_date(date_text)
    if not rows:
        return f"📅 **TEST REPORT — {date_text}**\n\n❌ Is date ko website update metadata ke according koi test nahi mila."
    lines = [f"📅 **TEST REPORT — {date_text}**", "", f"🆕 **{len(rows)} UPDATED TEST(S)**", ""]
    for rec in rows:
        lines.append(f"**SN {rec.get('sn','-')}** — {rec.get('name','Unnamed Test')}\n`ID: {rec.get('test_id','-')}`")
    return "\n".join(lines)

def all_date_report_text():
    reg = load_test_registry()
    groups = {}
    for rec in reg.values():
        d = rec.get("updated_date")
        if d:
            groups.setdefault(d, []).append(rec)
    if not groups:
        return "📊 **DATE-WISE REPORT**\n\n❌ Abhi koi updated-test history nahi hai."
    lines = ["📊 **DATE-WISE TEST REPORT**", ""]
    for d in sorted(groups.keys(), reverse=True):
        rows = sorted(groups[d], key=lambda x: int(x.get("sn", 0)))
        lines.append(f"📅 **{d} — {len(rows)} test(s)**")
        for rec in rows:
            lines.append(f"• **SN {rec.get('sn','-')}** — {rec.get('name','Unnamed Test')}")
        lines.append("")
    return "\n".join(lines)

def load_seen_tests():
    try:
        if os.path.exists(SEEN_TESTS_FILE):
            with open(SEEN_TESTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return {str(x) for x in data}
            if isinstance(data, dict):
                return {str(x) for x in data.get("seen_ids", [])}
    except Exception:
        pass
    return set()

def save_seen_tests(seen_ids):
    tmp = SEEN_TESTS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted({str(x) for x in seen_ids}), f, ensure_ascii=False, indent=2)
        os.replace(tmp, SEEN_TESTS_FILE)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False

def load_auto_chats():
    try:
        if os.path.exists(AUTO_CHATS_FILE):
            with open(AUTO_CHATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {int(x) for x in data} if isinstance(data, list) else set()
    except Exception:
        pass
    return set()

def save_auto_chats(chat_ids):
    tmp = AUTO_CHATS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted({int(x) for x in chat_ids}), f, ensure_ascii=False, indent=2)
        os.replace(tmp, AUTO_CHATS_FILE)
        return True
    except Exception:
        return False

def register_auto_chat(chat_id):
    chats = load_auto_chats()
    chats.add(int(chat_id))
    save_auto_chats(chats)

def unregister_auto_chat(chat_id):
    chats = load_auto_chats()
    chats.discard(int(chat_id))
    save_auto_chats(chats)

def get_new_tests_from_cache():
    seen = load_seen_tests()
    return [(tid, name) for tid, name in all_tests_cache if str(tid) not in seen]

def mark_tests_seen(test_ids):
    seen = load_seen_tests()
    seen.update(str(x) for x in test_ids)
    save_seen_tests(seen)

def create_baseline_if_missing(current_tests):
    # First run: current tests are old, so they must NOT appear in today's report.
    if not os.path.exists(SEEN_TESTS_FILE):
        save_seen_tests([tid for tid, _ in current_tests])
        sync_test_registry(current_tests, baseline=True)
        return True
    sync_test_registry(current_tests, baseline=False)
    return False

def _safe_caption_text(value, fallback="-"):
    """Escape dynamic values for Telegram HTML captions."""
    value = str(value if value not in (None, "") else fallback)
    return htmlmod.escape(value, quote=True)


def build_document_caption(test_id, test_name, auto_update=False):
    """Exact Telegram caption requested by user."""
    category = get_test_category(test_id)
    owner = "🦋⃟ᴠͥɪͣᴘͫ𓊈𒆜𝗥𝗼𝗹𝗲𝘅𒆜𓊉 ✰"
    dm_owner = "🦋⃟ᴠͥɪͣᴘͫ𓊈𒆜𝗗𝗿𝗮𝗴𝗼𝗻 🐉🐲𓊉 ✰"
    def make(name_value):
        return (
            f"🦖<strong>TEST NAME :- {_safe_caption_text(name_value)}</strong>\n\n"
            f"<blockquote><i><strong>🌲Topic :- TNC Test series 12.0</strong></i>\n"
            f"<strong>⚙️CATEGORY :- {_safe_caption_text(category)}</strong></blockquote>\n\n"
            f"<a href=\"https://t.me/krrish_bro\"><strong>🛫OWNER :- {_safe_caption_text(owner)}</strong></a>\n"
            f"\n<a href=\"https://t.me/Rolex_jaat\"><strong>🛒 DM {_safe_caption_text(dm_owner)}</strong></a>"
        )
    caption = make(test_name)
    if len(caption) > 900:
        clean_name = str(test_name or "Unnamed Test")
        lo, hi = 0, min(len(clean_name), 300)
        best = "Test"
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate_name = clean_name[:mid].rstrip()
            candidate = make(candidate_name + ("…" if mid < len(clean_name) else ""))
            if len(candidate) <= 900:
                best = candidate_name + ("…" if mid < len(clean_name) else "")
                lo = mid + 1
            else:
                hi = mid - 1
        caption = make(best)
    return caption

def send_extracted_test(chat_id, test_id, test_name, auto_update=False):
    filepath, status = download_single_test(test_id, test_name)
    if not filepath or not os.path.exists(filepath):
        return False, f"Extraction failed: {status}"
    try:
        if os.path.getsize(filepath) > 48 * 1024 * 1024:
            return False, "HTML file too large for Telegram."
        caption = build_document_caption(test_id, test_name, auto_update=auto_update)
        last_error = None
        for attempt in range(1, 5):
            try:
                with open(filepath, "rb") as doc:
                    bot.send_document(chat_id, doc, caption=caption, parse_mode="HTML", timeout=300)
                return True, "Sent"
            except Exception as e:
                last_error = e
                if attempt < 4:
                    time.sleep(3 * attempt)
        return False, f"Telegram upload failed: {last_error}"
    finally:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass

def check_for_new_tests(auto_send=True):
    global all_tests_cache
    with AUTO_UPDATE_LOCK:
        fresh = get_all_test_ids()
        if not fresh:
            return {"ok": False, "new": [], "sent": [], "failed": [], "count": 0}
        all_tests_cache = fresh
        if create_baseline_if_missing(fresh):
            return {"ok": True, "new": [], "sent": [], "failed": [], "count": len(fresh), "baseline": True}

        new_tests = get_new_tests_from_cache()
        if not new_tests:
            return {"ok": True, "new": [], "sent": [], "failed": [], "count": len(fresh)}

        sent_ids, failed = [], []
        if auto_send:
            chats = load_auto_chats()
            for tid, name in new_tests:
                all_ok = bool(chats)
                for chat_id in list(chats):
                    ok, msg = send_extracted_test(chat_id, tid, name, auto_update=True)
                    if not ok:
                        all_ok = False
                        failed.append((tid, name, msg))
                        try:
                            bot.send_message(chat_id, f"⚠️ Auto-update failed:\n{name}\n\n{msg}")
                        except Exception:
                            pass
                if all_ok:
                    sent_ids.append(tid)
                elif not chats:
                    failed.append((tid, name, "No auto-update chat registered"))
            if sent_ids:
                mark_tests_seen(sent_ids)
                # Send one compact date-wise summary after the batch is delivered.
                summary = report_text()
                for chat_id in list(chats):
                    try:
                        send_text_chunks(chat_id, summary, parse_mode="Markdown")
                    except Exception:
                        pass

        return {"ok": True, "new": new_tests, "sent": sent_ids, "failed": failed, "count": len(fresh)}

def auto_update_loop():
    print(f"[AUTO UPDATE] Checking every {AUTO_UPDATE_INTERVAL}s")
    while AUTO_UPDATE_ENABLED:
        try:
            result = check_for_new_tests(auto_send=True)
            if result.get("baseline"):
                print(f"[AUTO UPDATE] Baseline: {result.get('count', 0)} existing tests.")
            elif result.get("new"):
                print(f"[AUTO UPDATE] New={len(result['new'])}, Sent={len(result['sent'])}")
        except Exception as e:
            print(f"[AUTO UPDATE] Error: {e}")
        time.sleep(AUTO_UPDATE_INTERVAL)

def start_auto_update_thread():
    global AUTO_UPDATE_THREAD
    if AUTO_UPDATE_THREAD and AUTO_UPDATE_THREAD.is_alive():
        return
    AUTO_UPDATE_THREAD = threading.Thread(target=auto_update_loop, daemon=True)
    AUTO_UPDATE_THREAD.start()


def get_test_category(test_id):
    return _normalise_category(TEST_CATEGORY_CACHE.get(str(test_id), "UNCATEGORIZED"))


def get_category_groups():
    """Return {CATEGORY: [(test_id, name), ...]} from the latest website master list."""
    groups = {}
    for tid, name in all_tests_cache:
        cat = get_test_category(tid)
        groups.setdefault(cat, []).append((tid, name))
    return dict(sorted(groups.items(), key=lambda kv: kv[0]))


def _send_one_extracted_test(chat_id, tid, name, label_prefix="📥"):
    """Common sender used by manual/all/category download systems."""
    bot.send_message(chat_id, f"{label_prefix} Extracting: {name}\n🏷️ Category: {get_test_category(tid)}")
    ok, status = send_extracted_test(chat_id, tid, name)
    if not ok:
        bot.send_message(chat_id, f"❌ Failed: {name}\n{status}")
    return ok


def send_text_chunks(chat_id, text, parse_mode="Markdown", chunk_size=3500):
    """Send long Telegram text safely in multiple messages.

    Telegram bot messages have a 4096-character limit (and emoji can use multiple UTF-16 code units). Reports can grow as
    the test history grows, so split on line boundaries and hard-split only
    an exceptionally long individual line.
    """
    text = str(text or "")
    if len(text) <= chunk_size:
        return bot.send_message(chat_id, text, parse_mode=parse_mode)

    chunks = []
    current = ""
    for line in text.splitlines(True):
        if len(line) > chunk_size:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), chunk_size):
                chunks.append(line[i:i + chunk_size])
            continue
        if len(current) + len(line) > chunk_size:
            if current:
                chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)

    results = []
    for chunk in chunks:
        try:
            results.append(bot.send_message(chat_id, chunk, parse_mode=parse_mode))
        except Exception as e:
            # If Telegram still rejects a chunk, retry it without parse mode;
            # this prevents the report handler from crashing the polling thread.
            try:
                results.append(bot.send_message(chat_id, chunk, parse_mode=None))
            except Exception:
                # Last-resort hard split with a conservative size.
                safe_size = 2500
                for i in range(0, len(chunk), safe_size):
                    results.append(bot.send_message(chat_id, chunk[i:i + safe_size], parse_mode=None))
    return results[-1] if results else None

# --- TELEGRAM BOT LOGIC ROUTINES ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    global all_tests_cache
    bot.reply_to(message, "⏳ Initializing master platform test list, please wait...")
    all_tests_cache = get_all_test_ids()
    register_auto_chat(message.chat.id)
    # Fast startup: do NOT make one metadata API request per test here.
    # Website dates are fetched lazily by /report and /download2days.
    if all_tests_cache:
        sync_test_registry(all_tests_cache, baseline=False)

    msg_text = (
        "🤖 **Nursing Test Downloader Bot Ready!**\n\n"
        f"Platform par total **{len(all_tests_cache)} Tests** mile hain.\n"
        "🔄 Auto-update: **ON (every 5 min)**\n"
        "📥 Website ki actual updated date ke basis par tests track honge.\n\n"
        "📋 **Commands:**\n"
        "👉 /report - Last 2 days ki updated-date report\n"
        "👉 /report YYYY-MM-DD - Specific updated date\n"
        "👉 /report all - Full date-wise history\n"
        "👉 /download <S.No> - Manual extraction\n"
        "👉 /download2days - Aaj + kal ke website-updated tests download\n"
        "👉 /downloadall - Saare tests download\n"
        "👉 /categories - Website ki saari categories + test count\n"
        "👉 /downloadcat <Category> - Category-wise download\n"
        "👉 /scan / /newtests - Website se new tests scan\n"
        "👉 /autoon / /autooff - Auto-update ON/OFF"
    )
    bot.send_message(message.chat.id, msg_text, parse_mode="Markdown")




@bot.message_handler(commands=['report'])
def date_report(message):
    global all_tests_cache
    args = message.text.replace('/report', '', 1).strip()
    # Always refresh website dates before date-wise reporting; do not depend on bot start date.
    if not all_tests_cache:
        all_tests_cache = get_all_test_ids()
    if all_tests_cache:
        refresh_source_updated_dates(all_tests_cache, only_missing=True)
        sync_test_registry(all_tests_cache, baseline=False)
    if args.lower() == 'all':
        send_text_chunks(message.chat.id, all_date_report_text(), parse_mode="Markdown")
        return
    if not args:
        send_text_chunks(message.chat.id, report_last_2_days_text(), parse_mode="Markdown")
        return
    send_text_chunks(message.chat.id, report_text(args), parse_mode="Markdown")


@bot.message_handler(commands=['download2days', 'downloadlast2days'])
def download_last_2_days(message):
    """Fetch website metadata dates, then download tests updated today/yesterday."""
    global all_tests_cache
    bot.send_message(message.chat.id, "🔄 Website se actual updated dates fetch ho rahi hain...")
    all_tests_cache = get_all_test_ids()
    if not all_tests_cache:
        bot.send_message(message.chat.id, "❌ Website master list load nahi ho paayi.")
        return
    # Master list dates first; for missing dates, query the individual test metadata.
    found = refresh_source_updated_dates(all_tests_cache, only_missing=True)
    sync_test_registry(all_tests_cache, baseline=False)
    today = now_ist().date()
    reg = load_test_registry()
    selected = []
    for tid, name in all_tests_cache:
        rec = reg.get(test_registry_key(tid), {})
        raw = rec.get("updated_date") or TEST_UPDATED_DATE_CACHE.get(str(tid))
        try:
            d = datetime.strptime(str(raw), "%Y-%m-%d").date()
        except Exception:
            continue
        if 0 <= (today - d).days <= 1:
            selected.append((tid, name, d))
    if not selected:
        bot.send_message(message.chat.id, f"❌ Website ke actual updated-date metadata se aaj/kal ka koi test nahi mila.\n🔎 Metadata dates found: {found}")
        return
    bot.send_message(message.chat.id, f"📥 Last 2 days ke {len(selected)} updated tests mil gaye. Download start...")
    success = failed = 0
    for n, (tid, name, d) in enumerate(selected, 1):
        bot.send_message(message.chat.id, f"📥 [{n}/{len(selected)}] {name}\n📅 Updated: {d.strftime('%d-%m-%Y')}")
        ok, _status = send_extracted_test(message.chat.id, tid, name)
        if ok:
            success += 1
        else:
            failed += 1
    bot.send_message(message.chat.id, f"✅ Last 2 days download complete\n📥 Success: {success}\n❌ Failed: {failed}")



@bot.message_handler(commands=['scan', 'newtests', 'sync'])
def manual_scan(message):
    """Force one website scan and show only tests not seen before."""
    global all_tests_cache
    bot.send_message(message.chat.id, "🔎 Website se latest test list scan ho rahi hai...")
    result = check_for_new_tests(auto_send=False)
    if not result.get("ok"):
        bot.send_message(message.chat.id, "❌ Website master list fetch nahi ho paayi.")
        return
    new_tests = result.get("new", [])
    if not new_tests:
        bot.send_message(message.chat.id, f"✅ Scan complete. Koi new test nahi mila.\n📦 Total tests: {result.get('count', 0)}")
        return
    lines = [f"🆕 **NEW TESTS FOUND — {len(new_tests)}**", ""]
    for i, (tid, name) in enumerate(new_tests, 1):
        lines.append(f"**{i}. {name}**\n`ID: {tid}`")
    lines.append("")
    lines.append("📥 Inhe auto-download karne ke liye `/autoon` ON rakho.")
    send_text_chunks(message.chat.id, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=['autoon'])
def auto_on(message):
    register_auto_chat(message.chat.id)
    bot.send_message(message.chat.id, "✅ Auto-update enabled. Naye tests automatically extract/send honge.")

@bot.message_handler(commands=['autooff'])
def auto_off(message):
    unregister_auto_chat(message.chat.id)
    bot.send_message(message.chat.id, "⛔ Auto-update disabled for this chat. Manual commands kaam karenge.")




@bot.message_handler(commands=['downloadall'])
def download_all_tests(message):
    global all_tests_cache
    bot.send_message(message.chat.id, "🔄 Website se latest master list fetch ho rahi hai...")
    all_tests_cache = get_all_test_ids()
    if not all_tests_cache:
        bot.send_message(message.chat.id, "❌ Master list load nahi ho paayi.")
        return
    sync_test_registry(all_tests_cache, baseline=False)
    total = len(all_tests_cache)
    bot.send_message(message.chat.id, f"📦 **ALL TEST DOWNLOAD STARTED**\nTotal: **{total}** tests\nCategory-wise metadata bhi preserve rahega.", parse_mode="Markdown")
    success = 0
    failed = 0
    for n, (tid, name) in enumerate(all_tests_cache, 1):
        bot.send_message(message.chat.id, f"📥 [{n}/{total}] {name}\n🏷️ {get_test_category(tid)}")
        if _send_one_extracted_test(message.chat.id, tid, name, label_prefix="⚙️"):
            success += 1
        else:
            failed += 1
    bot.send_message(message.chat.id, f"\n✅ **ALL DOWNLOAD COMPLETE**\n📥 Success: **{success}**\n❌ Failed: **{failed}**\n📦 Total: **{total}**", parse_mode="Markdown")


@bot.message_handler(commands=['categories', 'category'])
def list_categories(message):
    """Show all categories fetched from the website, with test counts."""
    global all_tests_cache
    bot.send_message(message.chat.id, "🔄 Website se latest categories fetch ho rahi hain...")
    all_tests_cache = get_all_test_ids()
    if not all_tests_cache:
        bot.send_message(message.chat.id, "❌ Website master list load nahi ho paayi.")
        return

    groups = get_category_groups()
    if not groups:
        bot.send_message(message.chat.id, "❌ Koi category nahi mili.")
        return

    lines = ["📚 **TEST CATEGORIES**", ""]
    for cat, tests in groups.items():
        lines.append(f"🏷️ **{cat}** — {len(tests)} test(s)")
    lines += [
        "",
        "📥 **Category download:**",
        "`/downloadcat PRELIMS`",
        "`/downloadcat RRB`",
        "",
        "💡 Category ka exact naam upar se copy karke `/downloadcat` ke saath use karo."
    ]
    send_text_chunks(message.chat.id, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=['downloadcat', 'downloadcategory'])
def download_category_tests(message):
    global all_tests_cache
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        bot.reply_to(message, "❌ Category do. Example: `/downloadcat PRELIMS`", parse_mode="Markdown")
        return
    requested = _normalise_category(args[1])
    bot.send_message(message.chat.id, "🔄 Website se latest categories/tests fetch ho rahe hain...")
    all_tests_cache = get_all_test_ids()
    if not all_tests_cache:
        bot.send_message(message.chat.id, "❌ Master list load nahi ho paayi.")
        return
    groups = get_category_groups()
    if requested not in groups:
        available = ", ".join(groups.keys()) if groups else "None"
        bot.send_message(message.chat.id, f"❌ Category **{requested}** nahi mili.\n\nAvailable: {available}", parse_mode="Markdown")
        return
    tests = groups[requested]
    sync_test_registry(all_tests_cache, baseline=False)
    bot.send_message(message.chat.id, f"📚 **CATEGORY DOWNLOAD**\n🏷️ Category: **{requested}**\n📦 Tests: **{len(tests)}**", parse_mode="Markdown")
    success = 0
    failed = 0
    for n, (tid, name) in enumerate(tests, 1):
        bot.send_message(message.chat.id, f"📥 [{n}/{len(tests)}] {name}")
        if _send_one_extracted_test(message.chat.id, tid, name, label_prefix="⚙️"):
            success += 1
        else:
            failed += 1
    bot.send_message(message.chat.id, f"\n✅ **{requested} DOWNLOAD COMPLETE**\n📥 Success: **{success}**\n❌ Failed: **{failed}**", parse_mode="Markdown")


@bot.message_handler(commands=['download'])
def trigger_download(message):
    global all_tests_cache
    if not all_tests_cache:
        all_tests_cache = get_all_test_ids()
    if not all_tests_cache:
        bot.send_message(message.chat.id, "⚠️ Master list load nahi ho paayi. Thodi der baad retry karo.")
        return

    args = message.text.replace('/download', '', 1).strip()
    if not args:
        bot.reply_to(message, "❌ S.No daalna bhool gaye bhai! Example: `/download 12`", parse_mode="Markdown")
        return

    try:
        selected_indices = [int(x.strip()) for x in args.split(",") if x.strip().isdigit()]
        if not selected_indices:
            bot.reply_to(message, "❌ Valid S.No do. Example: `/download 1,2,3`", parse_mode="Markdown")
            return
        for index in selected_indices:
            if 1 <= index <= len(all_tests_cache):
                target_id, target_name = all_tests_cache[index - 1]
                bot.send_message(message.chat.id, f"📥 Downloading [{index}]: {target_name}...")
                ok, status = send_extracted_test(message.chat.id, target_id, target_name)
                if not ok:
                    bot.send_message(message.chat.id, f"❌ Failed [{index}]: {status}")
            else:
                bot.send_message(message.chat.id, f"⚠️ Choice [{index}] out of limit check karo.")
    except Exception as e:
        bot.send_message(message.chat.id, f"💥 Bot parsing crash error: {e}")

if __name__ == "__main__":
    print("[🚀] Telegram Bot engine started inside Pydroid 3!")
    print("[🔎] New-test scanner: ON (every 5 min)")
    print("[💡] Go to Telegram and start typing commands inside your bot chat.")
    # IMPORTANT: actually start the background scanner.  Previously the
    # function existed but was never launched, so /start falsely reported
    # auto-update as ON.
    start_auto_update_thread()
    bot.infinity_polling(skip_pending=True)
