# myproject/api/views.py
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from decouple import config
from django.http import JsonResponse, HttpRequest
from django.views.decorators.csrf import csrf_exempt

# -----------------------------------------------------------------------------
# Logging (dev-friendly defaults). In production, rely on settings.py.
# -----------------------------------------------------------------------------
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
#                OpenRouter (generate-review) configuration
# -----------------------------------------------------------------------------
DEBUG_MODE: bool = config("DEBUG", default="false").lower() == "true"

OPENROUTER_API_KEY: str = config("OPENROUTER_API_KEY", default="")
OPENROUTER_MODEL_PRIMARY: str = config("OPENROUTER_MODEL_PRIMARY", default="openai/gpt-4o-mini")
OPENROUTER_MODEL_FALLBACK: str = config("OPENROUTER_MODEL_FALLBACK", default="meta-llama/llama-3.1-70b-instruct")
APP_PUBLIC_URL: str = config("APP_PUBLIC_URL", default="http://127.0.0.1:8000")

CONNECT_TIMEOUT: float = float(config("LLM_CONNECT_TIMEOUT", default="5"))
READ_TIMEOUT: float = float(config("LLM_READ_TIMEOUT", default="60"))
MAX_RETRIES: int = int(config("LLM_MAX_RETRIES", default="2"))
RETRY_BACKOFF_SECONDS: float = float(config("LLM_RETRY_BACKOFF_SECONDS", default="0.8"))
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

MODEL_TEMPERATURE: float = float(config("MODEL_TEMPERATURE", default="0.7"))
MODEL_TOP_P: float = float(config("MODEL_TOP_P", default="0.9"))
MODEL_MAX_TOKENS: int = int(config("MODEL_MAX_TOKENS", default="450"))

SYSTEM_PROMPT = (
    "You are a professional Instagram Reels scriptwriter. "
    "Write emotionally engaging, high-retention scripts optimized for 30–60 seconds. "
    "You MUST return ONLY the three labeled sections, exactly in this order, with a blank line between each:\n\n"
    "Hook: ...\n\n"
    "Body: ...\n\n"
    "CTA: ...\n\n"
    "Rules:\n"
    "- Voice: spoken, natural English with contractions and plain words. No corporate or robotic phrasing.\n"
    "- Ban AI-scented words: embark, realm, transcend, unveil, discourse, insight, leverage (as a noun), "
    "unlock your potential, embrace, journey, harness, thus, hereby.\n"
    "- Hook: ONE sentence, bold/specific/curiosity-driving. No generic questions like “Are you a content creator…?”. "
    "Prefer claims, numbers, or pattern breaks; speak to one viewer using 'you/your'.\n"
    "- Body: short sentences (spoken cadence). Show one emotional shift (e.g., overwhelm→control). "
    "Provide a tiny framework, example, or 2–3 concrete steps with benefit-first phrasing; talk to ONE viewer ('you/your').\n"
    "- Tight match to niche, follower level, tone, and topic.\n"
    "- Format: no extra sections, no preamble/postscript, no hashtags, no emoji spam, no markdown formatting.\n"
    "- Length: ~80–110 words TOTAL across Hook+Body+CTA (do not exceed 110).\n"
    "- CTA: 1–2 lines, imperative and highly clickable, tied to the content, speaking to ONE viewer ('you/your').\n"
    "- If you produce anything outside the exact Hook/Body/CTA structure, FIX IT and produce only the three sections.\n"
)

# --- Stepwise (Premium-only) prompts -----------------------------------------
HOOK_SYSTEM_PROMPT = (
    "You are a professional IG Reels HOOK specialist.\n"
    "Return ONLY one sentence labeled exactly 'Hook:' (8–16 words).\n"
    "Absolute rules:\n"
    "- Speak directly to ONE viewer using second person ('you/your').\n"
    "- Name a concrete pain, desire, or moment in the first 3–5 words.\n"
    "- Use present tense and everyday words; sound human and conversational.\n"
    "- Create a curiosity gap or pattern break (micro-contrast or counterintuitive angle).\n"
    "- NO generic question templates (e.g., 'Ever wonder…', 'Are you…', 'Have you ever…', 'Did you know…').\n"
    "- NO bullets, NO markdown, NO bold, NO emojis, NO quotes, NO hashtags, NO extra text.\n"
    "Format example (style, not content):\n"
    "Hook: You open Instagram and your ideas vanish—try this 10-second prompt.\n"
)
BODY_SYSTEM_PROMPT = (
    "You are a professional IG Reels BODY writer. "
    "Return ONLY the Body labeled exactly 'Body:' in around 60–90 words. "
    "Talk directly to ONE viewer using 'you/your'. Start with empathy (pain/desire), then give a micro-story "
    "or vivid moment, then 2–3 concrete, easy steps (benefit-first phrasing). Use spoken cadence and contractions. "
    "Do NOT restate the Hook. No extra sections."
)
CTA_SYSTEM_PROMPT = (
    "You are a professional IG Reels CTA copywriter. "
    "Return ONLY the CTA labeled exactly 'CTA:' in 1–2 short lines. "
    "Use an imperative verb directed at ONE viewer ('you/your'), tie it to the Body's promise, "
    "and keep it frictionless (e.g., 'Save this', 'Comment \"me\"', 'Follow for X'). No extra sections."
)

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def build_user_prompt(
    niche: str,
    sub_category: str,
    follower_count: str,
    tone: str,
    more_specific: str
) -> str:
    parts = [
        f"I'm creating Reels in the '{niche}' niche" if niche else "I'm creating Reels",
        f"focused on '{sub_category}'" if sub_category else None,
        f"with a follower level of '{follower_count}'" if follower_count else None,
        f"in a '{tone}' tone" if tone else None,
    ]
    base = ", ".join([p for p in parts if p]) + "."
    specific = f" The script should revolve around: '{more_specific}'." if more_specific else ""
    guidance = (
        " Make it sound like spoken language, not an essay. "
        "Include one emotional shift and at least one concrete example or 2–3 steps "
        "relevant to my audience level. Keep total length ~80–110 words."
    )
    return base + specific + guidance


def call_openrouter(messages: List[Dict[str, str]], model: str) -> Tuple[Optional[str], Dict[str, Any], Optional[Dict[str, Any]]]:
    if not OPENROUTER_API_KEY:
        return None, {"status": 0, "tries": 0, "latency_ms": 0, "model": model}, {
            "code": "SERVER_CONFIG", "message": "Missing OPENROUTER_API_KEY."
        }

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "Referer": APP_PUBLIC_URL",
        "X-Title": "CreatorFlowAI",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": MODEL_TEMPERATURE,
        "top_p": MODEL_TOP_P,
        "max_tokens": MODEL_MAX_TOKENS,
        "stop": ["</script>"],
    }

    tries = 0
    start = time.time()
    last_status = None
    content = None
    error = None

    for attempt in range(MAX_RETRIES + 1):
        tries = attempt + 1
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            last_status = resp.status_code

            if resp.status_code >= 400:
                try:
                    upstream = resp.json()
                except Exception:
                    upstream = {"raw": resp.text[:1000]}
                if resp.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS * tries)
                    continue
                error = {
                    "code": "UPSTREAM_ERROR",
                    "message": f"{resp.status_code} from OpenRouter",
                    "upstream": upstream if DEBUG_MODE else {"hint": "Enable DEBUG to see upstream body"},
                }
                break

            data = resp.json()
            choices = data.get("choices") or []
            msg = choices[0].get("message") if choices else None
            content = msg.get("content") if msg else None

            if not content:
                error = {
                    "code": "BAD_RESPONSE",
                    "message": "No content returned from model.",
                    "upstream": data if DEBUG_MODE else {"hint": "Enable DEBUG to see upstream body"},
                }
            break

        except requests.Timeout:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * tries)
                continue
            error = {"code": "UPSTREAM_TIMEOUT", "message": "Model request timed out."}
        except requests.RequestException as e:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * tries)
                continue
            error = {"code": "UPSTREAM_REQUEST_ERROR", "message": str(e)[:300]}
        except Exception as e:
            error = {"code": "SERVER_ERROR", "message": str(e)[:300]}
            break

    latency_ms = int((time.time() - start) * 1000)
    meta = {"status": last_status or 0, "tries": tries, "latency_ms": latency_ms, "model": model}
    return content, meta, error


def generate_with_fallback(messages: List[Dict[str, str]]) -> Tuple[Optional[str], Dict[str, Any], Optional[Dict[str, Any]]]:
    content, meta, err = call_openrouter(messages, OPENROUTER_MODEL_PRIMARY)
    if content:
        return content, meta, None
    logger.warning("Primary model failed: %s | Falling back to %s", err, OPENROUTER_MODEL_FALLBACK)
    content2, meta2, err2 = call_openrouter(messages, OPENROUTER_MODEL_FALLBACK)
    if content2:
        return content2, meta2, None
    combined_err = {"primary": err, "fallback": err2}
    return None, meta2, combined_err


def _norm(s: Any) -> str:
    if not s:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return s.replace("\n", " ").replace("\r", " ").strip()


def _limit_words(text: str, max_words: int = 40) -> str:
    words = text.split()
    return text if len(words) <= max_words else " ".join(words[:max_words])


def _normalize_sections(text: str) -> str:
    """
    Fix common model slip-ups:
    - 'Hok:' -> 'Hook:'
    - normalize case of 'hook/body/cta' at line starts
    - remove trailing 'undefined' tokens
    - trim stray backticks/markdown fences if any
    """
    if not text:
        return text

    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"^```(?:\w+)?\n", "", t)
    t = re.sub(r"\n```$", "", t)
    t = re.sub(r"(?im)^\s*hok\s*:", "Hook:", t)
    t = re.sub(r"(?im)^\s*hook\s*:", "Hook:", t)
    t = re.sub(r"(?im)^\s*body\s*:", "Body:", t)
    t = re.sub(r"(?im)^\s*cta\s*:",  "CTA:",  t)
    t = re.sub(r"(?is)(?:\s*\bundefined\b\s*)+\Z", "", t).rstrip()
    return t

# --- Premium output sanitizers & quality gates --------------------------------
GENERIC_Q_RE = re.compile(r"(?i)\b(ever wonder|have you ever|are you( a)?|did you know|in today'?s (video|reel)|in this video|let'?s dive in)\b")
YOU_RE = re.compile(r"(?i)\byou(?:r|’re|'re|\b)")
ACTION_VERBS_RE = re.compile(r"(?i)\b(save|comment|follow|dm|share|tap|try|use|apply|post|record|write|build|launch|fix|download|grab|join|watch|bookmark)\b")

def _strip_md_noise(s: str) -> str:
    return re.sub(r"[*_`>#~]+", "", (s or "")).strip()

def _ensure_end_punct(s: str) -> str:
    return s if re.search(r"[.!?]$", s or "") else (s + "." if s else s)

def _first_sentence_or_line(s: str) -> str:
    s = (s or "").splitlines()[0].strip()
    m = re.search(r"^(.+?[.!?])(\s|$)", s)
    return (m.group(1).strip() if m else s)

def _cap_words(s: str, max_words: int) -> str:
    words = (s or "").split()
    if len(words) <= max_words:
        return s
    return " ".join(words[:max_words]).rstrip(",;:–-") + "…"

def _extract_after_label(text: str, label: str) -> str:
    t = (text or "").strip()
    m = re.search(rf"(?is)\b{re.escape(label)}\s*:\s*(.*)", t)
    if m:
        t = m.group(1).strip()
    t = re.sub(r"(?is)(?:\s*\bundefined\b\s*)+\Z", "", t).strip()
    return t

# --- Hook: tighten + gate + (optional) reforge --------------------------------
def _tighten_hook(raw: str) -> str:
    t = _extract_after_label(raw, "Hook")
    t = _strip_md_noise(t)
    t = _first_sentence_or_line(t)
    t = _cap_words(t, 16)  # target 8–16 words
    return _ensure_end_punct(t)

def _hook_needs_reforge(h: str) -> bool:
    if not h:
        return True
    words = h.split()
    too_short = len(words) < 8
    too_long  = len(words) > 18
    lacks_you = YOU_RE.search(h) is None
    is_generic_q = GENERIC_Q_RE.search(h) is not None
    return too_short or too_long or lacks_you or is_generic_q

REFORGE_HOOK_SYSTEM_PROMPT = (
    "You rewrite hooks to be direct, human, and relatable.\n"
    "Return ONLY one sentence labeled exactly 'Hook:' (8–16 words) addressing ONE viewer using 'you/your'.\n"
    "Name a concrete pain/desire early; avoid generic questions; no markdown/quotes/emojis.\n"
)

# --- Body: tighten + gate + (optional) reforge --------------------------------
def _tighten_body(raw: str) -> str:
    t = _extract_after_label(raw, "Body")
    t = _strip_md_noise(t)
    t = re.sub(r"^[\-\*\u2022]\s*", "", t, flags=re.MULTILINE)  # drop bullets
    t = re.sub(r"\s+", " ", t).strip()
    t = _cap_words(t, 80)  # average reel body 60–90 words; cap ~80
    return _ensure_end_punct(t)

def _body_needs_reforge(b: str) -> bool:
    if not b:
        return True
    words = b.split()
    too_short = len(words) < 50
    too_long  = len(words) > 100
    lacks_you = YOU_RE.search(b) is None
    has_generic = GENERIC_Q_RE.search(b) is not None
    lacks_action = ACTION_VERBS_RE.search(b) is None
    return too_short or too_long or lacks_you or has_generic or lacks_action

REFORGE_BODY_SYSTEM_PROMPT = (
    "You rewrite the BODY of an IG Reel to be persuasive and human.\n"
    "Return ONLY 'Body:' followed by ~60–90 words addressing ONE viewer using 'you/your'.\n"
    "Start with empathy (pain/desire), include a vivid mini-moment or micro-story, "
    "then give 2–3 concrete steps or a tiny framework with benefit-first phrasing. "
    "Use short spoken sentences and contractions. Avoid generic openers and buzzwords. No extra sections."
)

# --- CTA: tighten + gate + (optional) reforge ---------------------------------
def _tighten_cta(raw: str) -> str:
    t = _extract_after_label(raw, "CTA")
    t = _strip_md_noise(t)
    t = _first_sentence_or_line(t)
    t = _cap_words(t, 20)
    return _ensure_end_punct(t)

def _cta_needs_reforge(c: str) -> bool:
    if not c:
        return True
    too_long = len(c.split()) > 24
    lacks_imperative = ACTION_VERBS_RE.search(c) is None
    # prefer addressing the viewer
    lacks_you_hint = YOU_RE.search(c) is None
    return too_long or lacks_imperative or lacks_you_hint

REFORGE_CTA_SYSTEM_PROMPT = (
    "You rewrite CTAs to be clear, human, and high-converting.\n"
    "Return ONLY 'CTA:' followed by 1 short line (max ~16 words) speaking to ONE viewer using 'you/your'.\n"
    "Start with an imperative verb aligned to the Body (e.g., Save, Comment 'me', Follow, DM, Share). "
    "Keep it frictionless and specific. No emojis, no hashtags, no extra sections."
)

# --- Firestore (best-effort) --------------------------------------------------
def _get_user_doc(uid: Optional[str], email: Optional[str]) -> Optional[dict]:
    """
    Best-effort Firestore fetch. If firebase_admin is not configured, returns None gracefully.
    Looks up by uid first, then by email.
    """
    try:
        import firebase_admin
        from firebase_admin import firestore as fs

        if not firebase_admin._apps:
            firebase_admin.initialize_app()

        db = fs.client()

        if uid:
            snap = db.collection("users").document(uid.strip()).get()
            if snap and snap.exists:
                return snap.to_dict()

        if email:
            q = db.collection("users").where("email", "==", email.strip()).limit(1).get()
            if q:
                return q[0].to_dict()
    except Exception:
        logger.exception("Firestore not available or query failed")
    return None

# -----------------------------------------------------------------------------
# View: generate_review
# -----------------------------------------------------------------------------
@csrf_exempt
def generate_review(request: HttpRequest):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST allowed."}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    niche = _norm(body.get("niche", "Instagram"))
    sub_category = _norm(body.get("subCategory", ""))
    follower_count = _norm(body.get("followerCount", ""))
    tone = _norm(body.get("tone", ""))
    more_specific = _limit_words(_norm(body.get("moreSpecific", "")), 30)

    # Optional identity to detect Premium
    uid = _norm(body.get("uid", ""))
    email = _norm(body.get("email", ""))

    # Try to read user doc; if not found, plan stays empty → one-shot path
    user_doc = _get_user_doc(uid or None, email or None)
    plan = (user_doc or {}).get("subscriptionPlan", "").strip().title()

    user_prompt = build_user_prompt(niche, sub_category, follower_count, tone, more_specific)

    # ---- Premium stepwise: Hook -> Body -> CTA ----
    if plan == "Premium":
        body_err = None
        cta_err = None

        # 1) HOOK
        hook_msgs = [
            {"role": "system", "content": HOOK_SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ]
        hook_raw, hook_meta, hook_err = generate_with_fallback(hook_msgs)
        if hook_raw:
            hook = _tighten_hook(hook_raw)

            # Hook quality gate (second-person, non-generic, punchy)
            if _hook_needs_reforge(hook):
                reforged_raw, _, _ = generate_with_fallback([
                    {"role": "system", "content": REFORGE_HOOK_SYSTEM_PROMPT},
                    {"role": "user", "content": (
                        f"{user_prompt}\n\n"
                        f"Rewrite this into a direct, second-person, highly relatable one-liner (8–16 words), "
                        f"no generic questions:\nHook: {hook}"
                    )},
                ])
                if reforged_raw:
                    hook = _tighten_hook(reforged_raw)

            # 2) BODY (conditioned on Hook)
            body_msgs = [
                {"role": "system", "content": BODY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"{user_prompt}\n\nUse this Hook (do not repeat it verbatim):\n{hook}",
                },
            ]
            body_raw, body_meta, body_err = generate_with_fallback(body_msgs)

            if body_raw:
                body_txt = _tighten_body(body_raw)

                # Body quality gate (human, persuasive, concrete)
                if _body_needs_reforge(body_txt):
                    reforged_body_raw, _, _ = generate_with_fallback([
                        {"role": "system", "content": REFORGE_BODY_SYSTEM_PROMPT},
                        {"role": "user", "content": (
                            f"{user_prompt}\n\n"
                            f"Rewrite this Body to be more human, persuasive, and concrete (60–90 words):\n"
                            f"Body: {body_txt}"
                        )},
                    ])
                    if reforged_body_raw:
                        body_txt = _tighten_body(reforged_body_raw)

                # 3) CTA (conditioned on Body)
                cta_msgs = [
                    {"role": "system", "content": CTA_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"{user_prompt}\n\nHere is the Body to build a CTA for:\n{body_txt}",
                    },
                ]
                cta_raw, cta_meta, cta_err = generate_with_fallback(cta_msgs)

                if cta_raw:
                    cta_txt = _tighten_cta(cta_raw)

                    # CTA quality gate (imperative, viewer-directed, concise)
                    if _cta_needs_reforge(cta_txt):
                        reforged_cta_raw, _, _ = generate_with_fallback([
                            {"role": "system", "content": REFORGE_CTA_SYSTEM_PROMPT},
                            {"role": "user", "content": (
                                f"{user_prompt}\n\n"
                                f"Rewrite this CTA to be imperative, viewer-directed, and specific:\nCTA: {cta_txt}"
                            )},
                        ])
                        if reforged_cta_raw:
                            cta_txt = _tighten_cta(reforged_cta_raw)

                    combined = f"Hook: {hook}\n\nBody: {body_txt}\n\nCTA: {cta_txt}"
                    cleaned = _normalize_sections(combined.strip())
                    meta = {
                        "mode": "premium_stepwise",
                        "hook": hook_meta,
                        "body": body_meta,
                        "cta": cta_meta,
                    }
                    logger.info("Premium stepwise content: %r", cleaned[:2000])
                    return JsonResponse({"response": cleaned, "meta": meta}, status=200)

            # If Body or CTA failed, fall through to one-shot fallback
            logger.warning(
                "Stepwise failed (Body/CTA). Falling back to one-shot. hook_err=%s body_err=%s cta_err=%s",
                hook_err, body_err, cta_err
            )
        else:
            logger.warning("Stepwise failed (Hook). Falling back to one-shot. err=%s", hook_err)

    # ---- Default / Fallback: one-shot Hook+Body+CTA ----
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_prompt},
    ]
    content, meta, upstream_err = generate_with_fallback(messages)
    if content:
        cleaned = _normalize_sections(content.strip())
        logger.info("Final cleaned content (single-shot): %r", cleaned[:2000])
        return JsonResponse({"response": cleaned, "meta": {"mode": "single_shot", **meta}}, status=200)

    status_code = 502
    payload = {"error": "Upstream model failed", "meta": meta}
    if DEBUG_MODE and upstream_err:
        payload["upstream"] = upstream_err
    return JsonResponse(payload, status=status_code)

# -----------------------------------------------------------------------------
# Health endpoint
# -----------------------------------------------------------------------------
def health(_request):
    return JsonResponse({"ok": True})


# -----------------------------------------------------------------------------
#                       Firestore (admin SDK) initialization
# -----------------------------------------------------------------------------
# --- Firebase / Firestore bootstrap ------------------------------------------
import os
import json
import logging

logger = logging.getLogger(__name__)

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except Exception as _imp_err:
    firebase_admin = None
    firestore = None
    logger.warning("firebase_admin not installed or import failed: %s", _imp_err)

fs_db = None  # global Firestore client

# -------------------------------------------------------------------
# Plan constants
# -------------------------------------------------------------------
PLAN_CREDITS = {
    "Basic": 50,
    "Pro": 200,
    "Premium": 1000,
}
PAID_PLANS = {"Pro", "Premium"}

def init_firestore():
    """
    Initialize Firebase Admin using env vars and return a Firestore client.

    Priority:
      1) GOOGLE_APPLICATION_CREDENTIALS_JSON (raw JSON string in env var)
      2) FIREBASE_SERVICE_ACCOUNT (JSON one-liner OR file path)
      3) GOOGLE_APPLICATION_CREDENTIALS (handled by default initialize_app)
    """
    global fs_db

    if not firebase_admin or not firestore:
        logger.warning("Firebase Admin SDK unavailable; Firestore disabled.")
        fs_db = None
        return None

    if firebase_admin._apps:
        # Already initialized
        fs_db = firestore.client()
        return fs_db

    try:
        # Preferred: raw JSON pasted into env var
        sa_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "").strip()
        if sa_json:
            cred = credentials.Certificate(json.loads(sa_json))
            firebase_admin.initialize_app(cred)

        else:
            # Back-compat: FIREBASE_SERVICE_ACCOUNT can be JSON or file path
            sa_env = os.getenv("FIREBASE_SERVICE_ACCOUNT", "").strip()
            if sa_env:
                if sa_env.startswith("{"):
                    cred = credentials.Certificate(json.loads(sa_env))
                else:
                    with open(sa_env, "r", encoding="utf-8") as f:
                        cred = credentials.Certificate(json.load(f))
                firebase_admin.initialize_app(cred)
            else:
                # Last fallback: GOOGLE_APPLICATION_CREDENTIALS path
                firebase_admin.initialize_app()

        fs_db = firestore.client()
        logger.info("Firestore initialized successfully.")
        return fs_db

    except Exception as e:
        fs_db = None
        logger.warning("Firestore not available in backend: %s", e)
        return None


# Initialize on import
init_firestore()



# ---------------------------------------------------------------------------
# Example Firestore helper
def _fs_set_user(uid_or_email: str, plan: str, *,
                 credits_to_grant: int | None,
                 by_uid: bool = True,
                 transaction_id: str | None = None) -> bool:
    """
    Safely set a user's subscription state and credits.
    - Basic grants its free pack only once per account (freeBasicGranted).
    - Paid plans grant credits only once per unique transaction_id.
    - Always sets subscriptionSelected=True and subscriptionPlan=<plan>.
    """
    if fs_db is None:
        logger.warning("Firestore not initialized")
        return False

    try:
        # locate doc
        if by_uid:
            ref = fs_db.collection("users").document(uid_or_email)
        else:
            q = fs_db.collection("users").where("email", "==", uid_or_email).limit(1).get()
            if not q:
                return False
            ref = q[0].reference

        snap = ref.get()
        doc = snap.to_dict() or {}

        updates = {
            "subscriptionSelected": True,
            "subscriptionPlan": plan,
            "creditDepletedAt": None,
        }

        # ---- BASIC (free pack once)
        if plan == "Basic":
            if not doc.get("freeBasicGranted"):
                updates["credits"] = PLAN_CREDITS["Basic"]
                updates["freeBasicGranted"] = True
                logger.info("Granted Basic free pack")
            else:
                logger.info("User already claimed Basic free pack, no regrant")

        # ---- PAID (idempotent by transaction_id)
        elif plan in PAID_PLANS:
            if transaction_id:
                already = (doc.get("processedTxns") or {}).get(transaction_id)
                if already:
                    logger.info("Txn %s already processed, skipping", transaction_id)
                else:
                    if credits_to_grant is None:
                        credits_to_grant = PLAN_CREDITS.get(plan, 0)
                    updates["credits"] = credits_to_grant
                    updates[f"processedTxns.{transaction_id}"] = True
                    updates["lastPaidTxn"] = transaction_id
                    logger.info("Granted %s plan (txn=%s)", plan, transaction_id)
            else:
                # fallback: no txn id
                if credits_to_grant is None:
                    credits_to_grant = PLAN_CREDITS.get(plan, 0)
                updates["credits"] = credits_to_grant

        ref.set(updates, merge=True)
        return True
    except Exception as e:
        logger.exception("fs_set_user failed: %s", e)
        return False

# -----------------------------------------------------------------------------
#                    Paddle webhook helpers & endpoint
# -----------------------------------------------------------------------------
def _decode_passthrough(value) -> dict:
    """
    Handle passthrough being either JSON or base64(JSON) or already a dict.
    """
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        # try base64(JSON)
        try:
            decoded = base64.b64decode(value).decode("utf-8")
            return json.loads(decoded)
        except Exception:
            # try direct JSON
            try:
                return json.loads(value)
            except Exception:
                return {}
    return {}

def _get_identity_and_plan(data: dict) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Return (uid, email, plan_label) from various Paddle spots.
    Priority: custom_data/metadata.plan -> passthrough.plan; uid from passthrough.
    """
    plan_label = (
        (data.get("custom_data") or {}).get("plan")
        or (data.get("metadata") or {}).get("plan")
        or None
    )

    passthrough_raw = (
        data.get("passthrough")
        or (data.get("checkout", {}) or {}).get("passthrough")
        or (data.get("transaction", {}) or {}).get("passthrough")
    )
    pt = _decode_passthrough(passthrough_raw)
    uid = pt.get("uid")
    if not plan_label:
        plan_label = pt.get("plan")

    email = (
        (data.get("customer") or {}).get("email")
        or data.get("customer_email")
        or (data.get("billing_details") or {}).get("email")
        or pt.get("email")
    )

    return uid, email, (plan_label.title() if plan_label else None)

def _infer_plan_from_names(data: dict) -> Optional[str]:
    """
    Fallback: infer plan from price/product names when metadata is missing.
    Looks into:
      - data.items[0].price.name
      - data.details.line_items[0].product.name
    """
    try:
        items = data.get("items") or []
        if items:
            price = (items[0].get("price") or {})
            name = (price.get("name") or "").lower()
            if "premium" in name:
                return "Premium"
            if "pro" in name:
                return "Pro"

        li = ((data.get("details") or {}).get("line_items") or [])
        if li:
            prod_name = (li[0].get("product") or {}).get("name", "").lower()
            if "premium" in prod_name:
                return "Premium"
            if "pro" in prod_name:
                return "Pro"
    except Exception:
        pass
    return None

def _detect_credits(plan_label: Optional[str], data: dict) -> int:
    """
    Map plan to credits; supports amount fallbacks (minor units or strings).
    """
    if plan_label == "Pro":
        return 200
    if plan_label == "Premium":
        return 1000

    amount = (data.get("details") or {}).get("totals", {}).get("grand_total")
    if amount in (646, "6.46", 6.46, "646"):
        return 200
    if amount in (1669, "16.69", 16.69, "1669"):
        return 1000

    # Your sandbox payload showed 575 (5.00 + 0.75 tax). If you want to treat that as Pro:
    if amount in (575, "5.75", 5.75, "575"):
        return 200

    return 0

@csrf_exempt
def paddle_webhook(request):
    """
    Receives Paddle Billing webhooks.
    NOTE: In production, VERIFY the signature from Paddle before trusting payload.
    """
    if request.method != "POST":
        return HttpResponseBadRequest("POST only")

    try:
        body_text = request.body.decode("utf-8") or "{}"
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        logger.warning("Webhook: invalid JSON")
        return HttpResponseBadRequest("Invalid JSON")

    # Log up to 4000 chars—good for debugging
    logger.info("Paddle webhook payload: %s", body_text[:4000])

    # Paddle Billing wraps: { "event": { "type": "...", "data": {...} } }
    event = payload.get("event") or payload
    event_type = event.get("type") or event.get("name") or "unknown"
    data = event.get("data") or event.get("object") or payload

    interesting = {
        "transaction.completed",
        "subscription.activated",
        "subscription.payment_succeeded",
        "checkout.completed",  # just in case
    }
    if event_type not in interesting:
        return JsonResponse({"ok": True, "ignored": event_type})

    uid, email, plan = _get_identity_and_plan(data)
    if not plan:
        plan = _infer_plan_from_names(data)

    credits = _detect_credits(plan, data)

    logger.info(
        "Parsed -> event=%s uid=%s email=%s plan=%s credits=%s",
        event_type, uid, email, plan, credits
    )

    if not plan:
        logger.warning("Webhook: could not detect plan from payload; skipping update.")
        return JsonResponse({"ok": True, "ignored": "unknown_plan"})

    updated = False
    if uid:
        updated = _fs_set_user(uid, plan, credits, by_uid=True)
    if not updated and email:
        updated = _fs_set_user(email, plan, credits, by_uid=False)

    if not updated:
        logger.warning("Webhook: no matching Firestore user for uid=%s email=%s", uid, email)
        # Still return 200 to avoid infinite retries during dev.
        return JsonResponse({"ok": True, "no_user": True})

    return JsonResponse({"ok": True})

# -----------------------------------------------------------------------------
#            Client-side confirmation (Success page) endpoint
# -----------------------------------------------------------------------------
@csrf_exempt
def confirm_plan(request):
    """
    Allows your /checkout/success page to confirm a plan for the *current Firebase UID*.
    Body: { "uid": "...", "plan": "Pro"|"Premium"|"Basic" }
    """
    if request.method != "POST":
        return HttpResponseBadRequest("POST only")
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponseBadRequest("Invalid JSON")

    uid = body.get("uid")
    plan = body.get("plan")
    if not uid or plan not in ("Pro", "Premium", "Basic"):
        return HttpResponseBadRequest("Bad uid/plan")

    credits = 200 if plan == "Pro" else (1000 if plan == "Premium" else 50)
    updated = _fs_set_user(uid, plan, credits, by_uid=True)
    return JsonResponse({"ok": bool(updated)})

# --- Finalize checkout from success page -------------------------------------
from django.views.decorators.http import require_POST

PLAN_CREDITS = {"Basic": 50, "Pro": 200, "Premium": 1000}

def _apply_plan_to_user(uid: str | None, email: str | None, plan: str) -> bool:
    """
    Tries Firestore by uid first, then by email. Returns True if something was written.
    """
    credits = PLAN_CREDITS.get(plan, 0)
    updated = False
    if uid:
        updated = _fs_set_user(uid, plan, credits, by_uid=True)
    if not updated and email:
        updated = _fs_set_user(email, plan, credits, by_uid=False)
    return updated

@csrf_exempt
@require_POST
def finalize_checkout(request):
    """
    Called from SuccessPage.jsx after Paddle checkout success.
    Marks subscriptionSelected=True and grants plan credits.
    """
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

    uid   = (body.get("uid") or "").strip()
    email = (body.get("email") or "").strip()
    plan  = (body.get("plan") or "").strip().title()
    ptxn  = (body.get("transaction_id") or "").strip()

    if plan not in PLAN_CREDITS:
        return JsonResponse({"ok": False, "error": "invalid_plan"}, status=400)

    ident = uid or email
    if not ident:
        return JsonResponse({"ok": False, "error": "uid_or_email_required"}, status=400)

    ok = _fs_set_user(
        ident,
        plan,
        credits_to_grant=PLAN_CREDITS.get(plan),
        by_uid=bool(uid),
        transaction_id=ptxn or None,
    )
    return JsonResponse({"ok": ok})



# === Auto top-up utilities ===
# --- Auto top-up after 24h ----------------------------------------------------


import json
import logging
import datetime as _dt
from typing import Optional, Tuple

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

# If you have a Firestore client elsewhere, import it here:
# from .firestore import fs_db
# Ensure fs_db is a global pointing to google.cloud.firestore.Client
# Example:
#   from google.cloud import firestore
#   fs_db = firestore.Client()

logger = logging.getLogger(__name__)

# Map plans to monthly pack sizes
PLAN_DEFAULTS = {
    "basic": 50,
    "pro": 200,
    "premium": 1000,
}


def _normalize_plan(p: Optional[str]) -> str:
    return (p or "").strip().lower()


def _now_utc() -> str:
    # Not currently used, but handy for logs/debug
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _as_dt(v) -> Optional[_dt.datetime]:
    """
    Convert Firestore value to timezone-aware datetime (UTC).
    Accepts:
      - None
      - datetime (naive or tz-aware)
      - ISO strings like '2025-08-23T07:47:00Z' or with offset.
    """
    if not v:
        return None
    if isinstance(v, _dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=_dt.timezone.utc)
    try:
        return _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def _hours_since(ts: Optional[_dt.datetime]) -> float:
    """
    Return hours since 'ts'. If ts is None, return a sentinel value.
    We keep -1.0 for introspection (and include it in the response),
    but we won't block refills on a missing timestamp anymore.
    """
    if not ts:
        return -1.0
    now = _dt.datetime.now(_dt.timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return (now - ts).total_seconds() / 3600.0


def _plan_pack(plan_label: str) -> int:
    return PLAN_DEFAULTS.get(_normalize_plan(plan_label), 0)


def _fs_get_user_doc(uid_or_email: str, by_uid: bool = True):
    """
    Return (ref, snap) or (None, None).
    Expects global `fs_db` to be a Firestore client.
    """
    if fs_db is None:
        return None, None
    try:
        if by_uid:
            ref = fs_db.collection("users").document(uid_or_email)
            snap = ref.get()
            return (ref, snap) if snap.exists else (None, None)
        # by email
        qry = fs_db.collection("users").where("email", "==", uid_or_email).limit(1).get()
        if qry:
            snap = qry[0]
            return snap.reference, snap
    except Exception as e:
        logger.exception("Firestore read failed: %s", e)
    return None, None



@csrf_exempt
@require_POST
def select_basic(request):
    """
    Downgrade to Basic plan. 
    Does not regrant free credits if already claimed.
    """
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

    uid   = (body.get("uid") or "").strip()
    email = (body.get("email") or "").strip()

    ident = uid or email
    if not ident:
        return JsonResponse({"ok": False, "error": "uid_or_email_required"}, status=400)

    ok = _fs_set_user(
        ident,
        "Basic",
        credits_to_grant=None,
        by_uid=bool(uid),
        transaction_id=None,
    )
    return JsonResponse({"ok": ok})

@csrf_exempt
@require_POST
def refresh_credits(request):
    """
    Auto-refill credits after 24h if paid plan balance is 0.
    Also self-heals subscriptionSelected flag for paid users.
    """
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

    uid   = (body.get("uid") or "").strip()
    email = (body.get("email") or "").strip()

    ident = uid or email
    if not ident:
        return JsonResponse({"ok": False, "error": "uid_or_email_required"}, status=400)

    if fs_db is None:
        return JsonResponse({"ok": False, "error": "fs_unavailable"}, status=500)

    # locate doc
    if uid:
        ref = fs_db.collection("users").document(uid)
    else:
        q = fs_db.collection("users").where("email", "==", email).limit(1).get()
        if not q:
            return JsonResponse({"ok": False, "updated": False})
        ref = q[0].reference

    snap = ref.get()
    doc = snap.to_dict() or {}

    plan = (doc.get("subscriptionPlan") or "").title()
    credits = int(doc.get("credits") or 0)

    updates = {}

    # self-heal flag
    if plan in PAID_PLANS and not doc.get("subscriptionSelected"):
        updates["subscriptionSelected"] = True

    # refill if paid & empty & 24h passed
    if plan in PAID_PLANS and credits <= 0:
        depleted_at = doc.get("creditDepletedAt")
        now = datetime.utcnow()

        if not depleted_at:
            updates["creditDepletedAt"] = now.isoformat() + "Z"
        else:
            try:
                last = datetime.fromisoformat(depleted_at.replace("Z", ""))
                if now - last >= timedelta(hours=24):
                    updates["credits"] = PLAN_CREDITS.get(plan, 0)
                    updates["creditDepletedAt"] = None
            except Exception:
                updates["credits"] = PLAN_CREDITS.get(plan, 0)
                updates["creditDepletedAt"] = None

    if updates:
        ref.set(updates, merge=True)
        return JsonResponse({"ok": True, "updated": True})
    return JsonResponse({"ok": True, "updated": False})
