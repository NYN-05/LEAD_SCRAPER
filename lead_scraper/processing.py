"""Post normalization, classification, scoring, and shared field extraction."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import re
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")

HOT_PATTERNS = [
    r"\b(?:need|needing|seeking|looking for|hiring|looking to hire|want|wanting)\s+"
    r"(?:a|an|some|the|help with|to hire)?\s*"
    r"(?:professional\s+|freelance\s+|wedding\s+|event\s+|product\s+|food\s+|"
    r"portrait\s+|family\s+|corporate\s+)?"
    r"(?:photographer|photography|photoshoot|photo shoot)\b",
    r"\b(?:photo|photographer|photography|photoshoot|photo shoot)\b[^.!?\n]{0,60}"
    r"\b(?:needed|required|wanted)\b",
    r"\b(?:photographer|photography)\b[^.!?\n]{0,60}"
    r"\b(?:recommend(?:ation|ations)?|suggestions?|anyone know)\b",
    r"\b(?:recommend|suggest)\b[^.!?\n]{0,50}\b(?:photographer|photography)\b",
    r"\b(?:how much|quote|quotation|price|pricing|cost)\b[^.!?\n]{0,60}"
    r"\b(?:photograph|photoshoot|photo shoot|photographer)\b",
]
WARM_PATTERNS = [
    r"\b(?:next month|this month|upcoming|coming up|planning|plan to|book(?:ing)?)\b"
    r"[^.!?\n]{0,100}\b(?:wedding|engagement|birthday|anniversary|conference|event|"
    r"photoshoot|photo shoot|photograph)\b",
    r"\b(?:wedding|engagement|birthday|anniversary|conference|event)\b"
    r"[^.!?\n]{0,100}\b(?:photographer|photography|photoshoot|photo shoot|"
    r"photo coverage|photography coverage)\b",
]
EVENT_TERMS = [
    "wedding", "engagement", "birthday", "anniversary", "conference",
    "corporate event", "restaurant opening", "product launch", "new menu",
    "family event", "graduation", "baby shower",
]
PHOTO_TERMS = [
    "photograph", "photographer", "photography", "photoshoot", "photo shoot",
    "product photos", "food photos", "wedding photos", "event photos",
]
PROMO_PATTERNS = [
    r"\b(?:book me|book now|dm me|message me|contact me|hire me|"
    r"available for bookings|open for bookings|taking bookings)\b[^.!?\n]{0,100}"
    r"\b(?:photo|photograph|photoshoot|photo shoot|shoot|session|portrait|wedding|event)\b",
    r"\b(?:photo|photograph|photography|photoshoot|photo shoot|shoot|portrait)\b"
    r"[^.!?\n]{0,100}\b(?:dm me|message me|contact me|hire me|book me)\b",
    r"\b(?:we offer|i offer|our services include|my photography services|"
    r"our photography services|our packages|my packages)\b[^.!?\n]{0,100}"
    r"\b(?:photo|photograph|photoshoot|photo shoot|shoot)\b",
    r"\b(?:i am|i'm|we are|we're)\b[^.!?\n]{0,40}"
    r"\b(?:a |an )?(?:professional |freelance )?"
    r"(?:photographer|photography (?:studio|company|business))\b",
    r"\b(?:i|we)\b[^.!?\n]{0,20}\b(?:can|could|would love to|will)\b[^.!?\n]{0,30}"
    r"\b(?:help|capture|photograph|shoot)\b[^.!?\n]{0,60}"
    r"\b(?:photo|event|wedding|product|portrait|shoot)\b",
    r"\b(?:let us|we'll|we can)\b[^.!?\n]{0,40}\b(?:capture|photograph|shoot)\b",
    r"\b(?:iso|seeking|accepting|booking|taking)\b[^.!?\n]{0,30}\b(?:new )?clients\b",
    r"\b(?:explore|discover|browse)\b[^.!?\n]{0,60}"
    r"\b(?:photoshoot|photography|photo shoot|packages?)\b",
    r"\b(?:seeking|looking for|need(?:ing)?)\b[^.!?\n]{0,40}"
    r"\b(?:photography )?(?:gigs?|clients?|paid work|freelance work)\b",
    r"\b(?:photography|photographer)\b[^.!?\n]{0,30}\bclients?\b",
    r"\b(?:[0-9]+|several|multiple)\s+photographers?\s+wanted\b",
    r"\bphoto[-\s]?sharing (?:app|platform|tool)\b",
]
DISCUSSION_TERMS = ["my hobby", "photography tips", "camera settings", "photo editing tutorial"]
OFF_TOPIC_PATTERNS = [
    r"\b(?:lens|lenses|camera body|dslr|mirrorless|canon|nikon|sony|fujifilm|fuji|"
    r"lightroom|photoshop|presets?|editing|settings|tutorial)\b",
    r"\bteach me\b[^.!?\n]{0,20}\bphotography\b",
    r"\b(?:agents|photographers|vendors) of reddit\b",
    r"\b\d{2}\s?[mf]\b(?=\s+(?:looking|seeking|need|want|here)\b)",
    r"\b(?:photo|picture)s? missing\b",
]

# Explicit place phrases that do not follow an "in/at/near" preposition.
KNOWN_LOCATION_PHRASES = (
    "Bay Area",
    "Outer Sunset",
    "Big Bend NP",
)

LOCATION_REJECT_TERMS = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday", "week", "weeks",
    "weekend", "today", "tomorrow", "night", "lights", "need", "needs",
    "needed", "looking", "seeking", "hiring", "search", "searching",
    "photographer", "photographers", "photography", "photo", "photos",
    "photoshoot", "shoot", "session", "sessions", "wedding", "weddings",
    "event", "events", "corporate", "product", "family", "maternity",
    "newborn", "portrait", "portraits", "headshot", "headshots", "birthday",
    "engagement", "proposal", "couple", "couples", "baby", "baptism", "saree",
    "durga", "pujo", "pandal", "christmas", "holiday", "next", "this", "last",
    "few", "one", "two", "three", "my", "our", "the", "area",
    "region", "venue", "budget", "professional", "freelance", "local",
    "affordable", "urgent", "indian", "american", "night", "surprise",
}

LOCATION_PREP_RE = re.compile(
    r"(?:\b(?:in|at|near|around|from)\s+|\b(?:[Bb]ased|[Ll]ocated)\s+in\s+)"
    r"([A-Z][\w'’.-]+(?:\s+[A-Z][\w'’.-]+){0,3}"
    r"(?:,\s*[A-Z][\w'’.-]+(?:\s+[A-Z][\w'’.-]+){0,2})?)"
)
LOCATION_SUFFIX_RE = re.compile(
    r"\b([A-Z][\w'’.-]+(?:\s+[A-Z][\w'’.-]+){0,2})\s+(?:[Bb]ased|[Aa]rea|[Rr]egion)\b"
)


def extract_location(text: str) -> str:
    """Return an explicitly stated place, or blank when none is named."""
    if not text:
        return ""
    for phrase in KNOWN_LOCATION_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", text):
            return phrase
    for pattern in (LOCATION_PREP_RE, LOCATION_SUFFIX_RE):
        for match in pattern.finditer(text):
            candidate = match.group(1).strip(" ,.-")
            tokens = [token.strip(".,'-").casefold() for token in candidate.split()]
            if (
                candidate
                and len(candidate) <= 60
                and tokens
                and not any(token in LOCATION_REJECT_TERMS for token in tokens)
            ):
                return candidate
    return ""



def normalize_post(post: dict, requested_location: str = "") -> dict:
    text = BeautifulSoup(str(post.get("post_text", "")), "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    title = BeautifulSoup(
        str(post.get("post_title") or post.get("title") or ""), "html.parser"
    ).get_text(" ", strip=True)
    title = re.sub(r"\s+", " ", title).strip()
    email = EMAIL_RE.search(text)
    phone = PHONE_RE.search(text)
    contact = post.get("public_contact") or (email.group(0) if email else "")
    if not contact and phone:
        digits = re.sub(r"\D", "", phone.group(0))
        if 10 <= len(digits) <= 15:
            contact = phone.group(0).strip()
    location = post.get("location_text") or ""
    if not location:
        # Use only a place the author explicitly names; do not infer a city
        # from subreddit names or unrelated body text.
        location = extract_location(title)
    if not location and requested_location and requested_location.casefold() in text.casefold():
        location = requested_location
    return {
        "source": str(post.get("source", "")).lower(),
        "source_post_id": str(post.get("source_post_id", "")),
        "author_name": post.get("author_name", "") or "",
        "author_handle": post.get("author_handle", "") or "",
        "post_url": post.get("post_url", "") or "",
        "post_title": title,
        "post_text": text,
        "created_at": post.get("created_at", ""),
        "location": location,
        "public_contact": contact,
        "query": post.get("query", ""),
        "collected_at": post.get("collected_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "intent_evidence": post.get("intent_evidence", "") or "",
    }


def deduplicate_posts(posts: list[dict]) -> list[dict]:
    """Keep the first normalized post for each source identity or content fingerprint."""
    unique: list[dict] = []
    seen: set[str] = set()
    for post in posts:
        source = post.get("source", "").casefold()
        source_id = post.get("source_post_id", "").strip()
        parsed = urlsplit(post.get("post_url", ""))
        canonical_url = urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))
        identity = f"id:{source}:{source_id}" if source_id else f"url:{source}:{canonical_url}"
        author = (post.get("author_handle") or post.get("author_name") or "").casefold().strip()
        title = re.sub(r"\s+", " ", post.get("post_title", "").casefold()).strip()
        text = re.sub(r"\s+", " ", post.get("post_text", "").casefold()).strip()
        fingerprint = sha256(f"{source}|{author}|{text}".encode("utf-8")).hexdigest()
        # The same author often crossposts one request to several subreddits;
        # count that as a single lead.
        crosspost = f"title:{source}:{author}:{title}" if author and title else ""
        for key in (identity, crosspost, f"text:{fingerprint}"):
            if key and key in seen:
                break
        else:
            seen.update(key for key in (identity, crosspost, f"text:{fingerprint}") if key)
            unique.append(post)
    return unique


def classify_post(post: dict) -> dict:
    # A Reddit/RSS title states the actual topic; long bodies frequently
    # mention photography in passing (stories, novels, unrelated advice).
    # Classify on the title when one exists, otherwise on the post text.
    title = (post.get("post_title") or "").strip()
    text = (title or post.get("post_text") or "").lower()
    is_photo = any(term in text for term in PHOTO_TERMS)
    hot_match = next(
        (match for pattern in HOT_PATTERNS if (match := re.search(pattern, text, re.I))),
        None,
    )
    warm_match = next(
        (match for pattern in WARM_PATTERNS if (match := re.search(pattern, text, re.I))),
        None,
    )
    strong_hot = re.search(
        r"\b(looking to hire|looking for|hiring|recommend(?:ation|ations)?|"
        r"anyone know|how much|quote|quotation|price|pricing|cost)\b",
        text,
        re.I,
    )
    promo = any(re.search(pattern, text, re.I) for pattern in PROMO_PATTERNS)
    off_topic = any(re.search(pattern, text, re.I) for pattern in OFF_TOPIC_PATTERNS)
    testimonial = re.search(
        r"\b(?i:recommendation|recommendations)\s*!?\s*(?:[A-Z][\w&'-]*\s*){1,4}"
        r"(?:Studios?|Photography|Films?)\b",
        title,
    )
    event = any(term in text for term in EVENT_TERMS)
    discussion = any(term in text for term in DISCUSSION_TERMS)
    request_signal = re.search(
        r"\b(?:planning|plan to|upcoming|next month|this month|soon|book(?:ing)?|"
        r"looking|need(?:ed)?|want|seeking|hire|recommend|suggest|quote|"
        r"price|pricing|cost)\b",
        text,
    )
    personal_signal = re.search(r"\b(?:i|i'm|we|we're|my|our|me|us)\b", text)
    if promo:
        category, temperature, qualified, reason, evidence = "provider_promotion", "Cold", False, "Author appears to be advertising photography services.", ""
    elif off_topic:
        category, temperature, qualified, reason, evidence = "general_discussion", "Cold", False, "Post is about photography gear, learning, or unrelated topics rather than hiring.", ""
    elif testimonial:
        category, temperature, qualified, reason, evidence = "third_party_testimonial", "Cold", False, "Post recommends a named studio rather than requesting photography services.", ""
    elif hot_match and is_photo and (strong_hot or not warm_match):
        category, temperature, qualified, reason, evidence = "direct_request", "Hot", True, "Post explicitly asks to hire, price, or get recommendations for photography.", hot_match.group(0)
    elif warm_match and is_photo and request_signal and personal_signal:
        category, temperature, qualified, reason, evidence = "potential_requirement", "Warm", True, "Post describes an upcoming photography-related requirement.", warm_match.group(0)
    elif discussion:
        category, temperature, qualified, reason, evidence = "general_discussion", "Cold", False, "Post appears informational or hobby-related.", ""
    elif is_photo:
        category, temperature, qualified, reason, evidence = "general_interest", "Cold", False, "Photography is mentioned without a clear service requirement.", ""
    else:
        category, temperature, qualified, reason, evidence = "irrelevant", "Cold", False, "No sufficiently clear photography buying-intent signal.", ""
    intent_score = 60 if temperature == "Hot" else 35 if temperature == "Warm" else 0
    return {
        "intent_category": category,
        "qualified": qualified,
        "classification_reason": reason,
        "intent_evidence": evidence,
        "intent_points": intent_score,
        "lead_temperature": temperature,
    }


def _parse_date(value):
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        try:
            dt = parsedate_to_datetime(str(value))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None


def score_lead(post: dict, classification: dict, requested_location: str = "") -> dict:
    intent = int(classification.get("intent_points", 0))
    text = (post.get("post_text") or "").lower()
    service_points = 20 if any(x in text for x in [
        "wedding", "product", "food", "event", "engagement", "portrait",
        "birthday", "corporate", "family", "restaurant",
    ]) else 8
    created = _parse_date(post.get("created_at"))
    recency = 0
    if created:
        age_days = max(0, (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 86400)
        recency = 15 if age_days <= 2 else 10 if age_days <= 7 else 5 if age_days <= 30 else 0
    location = post.get("location", "")
    location_points = 15 if requested_location and location and requested_location.casefold() in location.casefold() else 8 if location else 0
    contact_points = 10 if post.get("public_contact") else 0
    total = min(100, intent + service_points + recency + location_points + contact_points)
    label = classification.get("lead_temperature") or ("Hot" if total >= 75 else "Warm" if total >= 45 else "Cold")
    return {
        "score": total,
        "lead_temperature": label,
        "score_breakdown": {
            "intent": intent,
            "service_specificity": service_points,
            "recency": recency,
            "location": location_points,
            "public_contact": contact_points,
        },
    }
