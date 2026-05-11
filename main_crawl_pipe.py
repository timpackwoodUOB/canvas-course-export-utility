import os
import re
import time
import shutil
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote

# =========================================================
# CONFIG
# =========================================================

ACCESS_TOKEN = os.getenv("CANVAS_API_ACCESS_TOKEN") 
CANVAS_BASE_URL = os.getenv("CANVAS_BASE_URL")
COURSE_IDS = []  # or example IDs like [12345, 67890] typically find the at https://base_canvas_url/courses/course_id

# Concurrency settings
MAX_DOWNLOAD_WORKERS = 10
MAX_FETCH_WORKERS = 6
RATE_LIMIT_THRESHOLD = 50

# Connection-pooled session
SESSION = requests.Session()
SESSION.headers.update({"Authorization": f"Bearer {ACCESS_TOKEN}"})
adapter = requests.adapters.HTTPAdapter(
    pool_connections=20,
    pool_maxsize=20,
    max_retries=3
)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)

# Rate limit lock
_rate_lock = threading.Lock()


# These globals are reset for each course
COURSE_ID = None
OUTPUT_DIR = None
DOWNLOADED_FILES = {}
SKIPPED_FILES = set()
PAGE_SLUG_TO_LOCAL = {}
ASSIGNMENT_ID_TO_LOCAL = {}
DISCUSSION_ID_TO_LOCAL = {}
QUIZ_ID_TO_LOCAL = {}
FILE_ID_TO_LOCAL = {}
HTML_FILES_TO_REWRITE = []

# Thread-safe lock for shared state
_state_lock = threading.Lock()


# =========================================================
# RESET STATE
# =========================================================

def reset_state(course_id):
    global COURSE_ID, OUTPUT_DIR
    global DOWNLOADED_FILES, SKIPPED_FILES
    global PAGE_SLUG_TO_LOCAL, ASSIGNMENT_ID_TO_LOCAL
    global DISCUSSION_ID_TO_LOCAL, QUIZ_ID_TO_LOCAL
    global FILE_ID_TO_LOCAL, HTML_FILES_TO_REWRITE

    COURSE_ID = course_id
    OUTPUT_DIR = None
    DOWNLOADED_FILES = {}
    SKIPPED_FILES = set()
    PAGE_SLUG_TO_LOCAL = {}
    ASSIGNMENT_ID_TO_LOCAL = {}
    DISCUSSION_ID_TO_LOCAL = {}
    QUIZ_ID_TO_LOCAL = {}
    FILE_ID_TO_LOCAL = {}
    HTML_FILES_TO_REWRITE = []


# =========================================================
# HELPERS
# =========================================================

def sanitize_filename(name):
    name = name.strip()
    # Replace common Unicode characters with ASCII equivalents
    replacements = {
        '\u2014': '-',   # em-dash
        '\u2013': '-',   # en-dash
        '\u2018': "'",   # left single quote
        '\u2019': "'",   # right single quote
        '\u201c': '"',   # left double quote
        '\u201d': '"',   # right double quote
        '\u2026': '...',  # ellipsis
        '\u2022': '-',   # bullet
        '\u00a9': '(c)',  # copyright
        '\u00ae': '(R)',  # registered
        '\u2122': '(TM)', # trademark
        '\u00b7': '-',   # middle dot
        '\u2010': '-',   # hyphen
        '\u2011': '-',   # non-breaking hyphen
        '\u2012': '-',   # figure dash
        '\u2015': '-',   # horizontal bar
        '\u00a0': ' ',   # non-breaking space
    }
    for unicode_char, ascii_char in replacements.items():
        name = name.replace(unicode_char, ascii_char)
    # Replace any remaining non-ASCII characters
    name = re.sub(r'[^\x00-\x7F]', '_', name)
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def save_text(path, text):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def check_rate_limit(response):
    """Check Canvas rate limit headers and sleep if needed."""
    remaining = response.headers.get("X-Rate-Limit-Remaining")
    if remaining is not None:
        try:
            remaining = float(remaining)
            if remaining < RATE_LIMIT_THRESHOLD:
                wait_time = max(0.5, (RATE_LIMIT_THRESHOLD - remaining) / RATE_LIMIT_THRESHOLD * 2)
                with _rate_lock:
                    print(f"  [Rate limit low: {remaining:.0f} remaining, sleeping {wait_time:.1f}s]")
                    time.sleep(wait_time)
        except ValueError:
            pass


def get_paginated(url):
    results = []
    while url:
        response = SESSION.get(url, timeout=30)
        response.raise_for_status()
        check_rate_limit(response)
        results.extend(response.json())
        url = response.links.get("next", {}).get("url")
    return results


def api_get(url):
    response = SESSION.get(url, timeout=30)
    response.raise_for_status()
    check_rate_limit(response)
    return response.json()


# =========================================================
# FETCH COURSE NAME
# =========================================================

def get_course_name(course_id):
    url = f"{CANVAS_BASE_URL}/api/v1/courses/{course_id}"
    response = SESSION.get(url, timeout=30)
    response.raise_for_status()
    check_rate_limit(response)
    course = response.json()
    name = course.get("name") or course.get("course_code") or f"canvas_course_{course_id}"
    return name


# =========================================================
# DOWNLOAD FILE
# =========================================================

def download_file(file_url, local_path, max_retries=3):
    ensure_dir(os.path.dirname(local_path))

    for attempt in range(max_retries):
        try:
            response = SESSION.get(
                file_url,
                stream=True,
                allow_redirects=True,
                timeout=60
            )
            response.raise_for_status()
            check_rate_limit(response)

            with open(local_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            print(f"  Downloaded: {local_path}")
            return True

        except Exception as e:
            if attempt < max_retries - 1:
                print(f"  Retry {attempt + 1}/{max_retries} for {file_url}: {e}")
                time.sleep(2)
            else:
                print(f"  FAILED to download {file_url}: {e}")
                return False

    return False


# =========================================================
# FILE INFO FROM CANVAS API
# =========================================================

def get_file_info(file_id):
    try:
        url = f"{CANVAS_BASE_URL}/api/v1/courses/{COURSE_ID}/files/{file_id}"
        response = SESSION.get(url, timeout=30)
        check_rate_limit(response)

        if response.status_code == 404:
            url = f"{CANVAS_BASE_URL}/api/v1/files/{file_id}"
            response = SESSION.get(url, timeout=30)
            check_rate_limit(response)

        if response.status_code == 200:
            return response.json()
        else:
            print(f"  Could not get info for file {file_id} (HTTP {response.status_code})")
            return None

    except Exception as e:
        print(f"  Error getting file info for {file_id}: {e}")
        return None


def is_file_published(file_info):
    if not file_info:
        return False
    """if file_info.get("hidden", False):
        return False"""
    if file_info.get("locked", False):
        return False
    if file_info.get("locked_for_user", False):
        return False
    return True


def get_filename_for_file(file_id, fallback_url=""):
    file_info = get_file_info(file_id)

    if file_info:
        published = is_file_published(file_info)
        filename = file_info.get("display_name") or file_info.get("filename")
        if filename:
            return sanitize_filename(filename), file_info.get("url"), published, file_info
        return f"file_{file_id}", file_info.get("url"), published, file_info

    if fallback_url:
        parsed = urlparse(fallback_url)
        path_parts = parsed.path.rstrip("/").split("/")
        for part in reversed(path_parts):
            decoded = unquote(part)
            if "." in decoded and decoded != "download":
                return sanitize_filename(decoded), None, True, None

    return f"file_{file_id}", None, True, None


# =========================================================
# URL CLASSIFICATION HELPERS
# =========================================================

def extract_file_id(url):
    match = re.search(r'/files/(\d+)', url)
    if match:
        return match.group(1)
    return None


def extract_page_slug(url):
    match = re.search(r'/courses/\d+/pages/([^/?#]+)', url)
    if match:
        return unquote(match.group(1))
    return None


def extract_assignment_id(url):
    match = re.search(r'/courses/\d+/assignments/(\d+)', url)
    if match:
        return match.group(1)
    return None


def extract_discussion_id(url):
    match = re.search(r'/courses/\d+/discussion_topics/(\d+)', url)
    if match:
        return match.group(1)
    return None


def extract_quiz_id(url):
    match = re.search(r'/courses/\d+/quizzes/(\d+)', url)
    if match:
        return match.group(1)
    return None


def is_canvas_url(url):
    if not url:
        return False
    if url.startswith("/"):
        return True
    parsed = urlparse(url)
    canvas_host = urlparse(CANVAS_BASE_URL).netloc
    return parsed.netloc == canvas_host


# =========================================================
# LOCALIZE HTML (PASS 1 — download assets, use placeholders)
# =========================================================

def localize_html(html, assets_dir):
    if not html:
        return html

    soup = BeautifulSoup(html, "html.parser")

    # Collect all file references first for parallel info fetching
    file_refs = []  # (tag, attr, file_id, original_url)

    tag_attr_pairs = [
        ("img", "src"),
        ("a", "href"),
        ("source", "src"),
        ("video", "src"),
        ("audio", "src"),
        ("embed", "src"),
        ("object", "data"),
        ("iframe", "src"),
    ]

    for tag_name, attr in tag_attr_pairs:
        for tag in soup.find_all(tag_name):
            if not tag.has_attr(attr):
                continue

            original_url = tag[attr]

            if not is_canvas_url(original_url):
                continue

            absolute_url = urljoin(CANVAS_BASE_URL, original_url)
            file_id = extract_file_id(absolute_url)

            if file_id:
                file_refs.append((tag, attr, file_id, absolute_url))

    # Also check for inline style background images
    for tag in soup.find_all(style=True):
        style = tag["style"]
        bg_matches = re.findall(r'url\(["\']?(.*?)["\']?\)', style)
        for bg_url in bg_matches:
            if is_canvas_url(bg_url):
                absolute_url = urljoin(CANVAS_BASE_URL, bg_url)
                file_id = extract_file_id(absolute_url)
                if file_id:
                    file_refs.append((tag, "__style_bg__", file_id, absolute_url))

    # Fetch file info in parallel
    file_info_map = {}

    def fetch_info(file_id):
        if file_id not in file_info_map:
            filename, download_url, published, finfo = get_filename_for_file(file_id)
            with _state_lock:
                file_info_map[file_id] = (filename, download_url, published, finfo)

    unique_file_ids = list(set(ref[2] for ref in file_refs))

    with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
        executor.map(fetch_info, unique_file_ids)

    # Download files in parallel
    download_tasks = []

    for tag, attr, file_id, absolute_url in file_refs:
        filename, download_url, published, finfo = file_info_map.get(
            file_id, (f"file_{file_id}", None, True, None)
        )

        # Skip unpublished files
        if not published:
            with _state_lock:
                SKIPPED_FILES.add(file_id)
            print(f"  [SKIPPED - unpublished file] {filename} (ID: {file_id})")

            if attr == "src" and tag.name == "img":
                tag["src"] = "#"
                tag["alt"] = f"[Unpublished image: {filename}]"
                tag["style"] = "opacity:0.3; border:2px dashed red; padding:10px;"
            elif attr == "href":
                tag["href"] = "#"
                tag["style"] = "text-decoration: line-through; color: gray;"
                tag["title"] = f"[Unpublished file: {filename} (ID: {file_id})]"
            elif attr == "__style_bg__":
                pass
            continue

        local_asset_path = os.path.join(assets_dir, filename)

        # Use placeholder for relative path — will be resolved in Pass 2
        if attr == "__style_bg__":
            style = tag["style"]
            tag["style"] = re.sub(
                r'url\(["\']?' + re.escape(absolute_url.split("/files/")[0]) + r'.*?["\']?\)',
                f'url("{{{{ASSET:{filename}}}}}")',
                style
            )
        else:
            tag[attr] = f"{{{{ASSET:{filename}}}}}"

        with _state_lock:
            if file_id not in DOWNLOADED_FILES:
                DOWNLOADED_FILES[file_id] = local_asset_path
                FILE_ID_TO_LOCAL[file_id] = local_asset_path
                dl_url = download_url or f"{CANVAS_BASE_URL}/files/{file_id}/download"
                download_tasks.append((dl_url, local_asset_path))

    # Execute downloads in parallel
    if download_tasks:
        with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as executor:
            futures = {
                executor.submit(download_file, url, path): (url, path)
                for url, path in download_tasks
                if not os.path.exists(path)
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    url, path = futures[future]
                    print(f"  Download error for {url}: {e}")

    return str(soup)


# =========================================================
# WRAP HTML PAGE
# =========================================================

def wrap_html(title, body_html, back_link="../index.html", extra_head=""):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            max-width: 900px;
            margin: 0 auto;
            padding: 20px;
            line-height: 1.6;
            color: #333;
        }}
        a {{ color: #0066cc; }}
        .back-link {{
            display: inline-block;
            margin-bottom: 20px;
            padding: 5px 10px;
            background: #f0f0f0;
            border-radius: 4px;
            text-decoration: none;
            font-size: 0.9em;
        }}
        .back-link:hover {{ background: #e0e0e0; }}
        h1 {{
            border-bottom: 2px solid #0066cc;
            padding-bottom: 10px;
        }}
        img {{
            max-width: 100%;
            height: auto;
        }}
        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 1em 0;
        }}
        th, td {{
            border: 1px solid #ddd;
            padding: 8px;
            text-align: left;
        }}
        th {{ background: #f5f5f5; }}
        .unresolved-canvas-link {{
            color: #cc0000;
            text-decoration: underline wavy;
        }}
        .quiz-question {{
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 15px;
            margin: 15px 0;
            background: #fafafa;
        }}
        .quiz-question h3 {{
            margin-top: 0;
            color: #0066cc;
        }}
        .answer {{
            padding: 5px 10px;
            margin: 3px 0;
            border-radius: 4px;
        }}
        .answer.correct {{
            background: #e6ffe6;
            border-left: 4px solid #00aa00;
        }}
        .answer.incorrect {{
            background: #fff;
        }}
        .feedback {{
            font-style: italic;
            color: #666;
            margin-top: 5px;
            padding: 5px;
            background: #f0f0f0;
            border-radius: 4px;
        }}
        .meta {{
            color: #666;
            font-size: 0.9em;
            margin-bottom: 15px;
        }}
    </style>
    {extra_head}
</head>
<body>
    <a class="back-link" href="{back_link}">&larr; Back to Index</a>
    <h1>{title}</h1>
    {body_html}
</body>
</html>"""


# =========================================================
# FETCH CONTENT ITEMS (for parallel fetching)
# =========================================================

def fetch_page(page_url):
    try:
        url = f"{CANVAS_BASE_URL}/api/v1/courses/{COURSE_ID}/pages/{page_url}"
        return api_get(url)
    except Exception as e:
        print(f"  Error fetching page {page_url}: {e}")
        return None


def fetch_assignment(assignment_id):
    try:
        url = f"{CANVAS_BASE_URL}/api/v1/courses/{COURSE_ID}/assignments/{assignment_id}"
        return api_get(url)
    except Exception as e:
        print(f"  Error fetching assignment {assignment_id}: {e}")
        return None


def fetch_discussion(discussion_id):
    try:
        url = f"{CANVAS_BASE_URL}/api/v1/courses/{COURSE_ID}/discussion_topics/{discussion_id}"
        return api_get(url)
    except Exception as e:
        print(f"  Error fetching discussion {discussion_id}: {e}")
        return None


def fetch_quiz(quiz_id):
    try:
        url = f"{CANVAS_BASE_URL}/api/v1/courses/{COURSE_ID}/quizzes/{quiz_id}"
        return api_get(url)
    except Exception as e:
        print(f"  Error fetching quiz {quiz_id}: {e}")
        return None


def fetch_quiz_questions(quiz_id):
    try:
        url = f"{CANVAS_BASE_URL}/api/v1/courses/{COURSE_ID}/quizzes/{quiz_id}/questions?per_page=100"
        return get_paginated(url)
    except Exception as e:
        print(f"  Error fetching quiz questions for {quiz_id}: {e}")
        return []


# =========================================================
# RENDER QUIZ QUESTIONS
# =========================================================

def render_quiz_questions(questions):
    if not questions:
        return "<p><em>No questions available (this may be a New Quizzes quiz).</em></p>"

    html_parts = []

    for i, q in enumerate(questions, 1):
        q_type = q.get("question_type", "unknown")
        q_text = q.get("question_text", "")
        q_name = q.get("question_name", f"Question {i}")
        points = q.get("points_possible", 0)
        answers = q.get("answers", [])

        part = f'<div class="quiz-question">'
        part += f'<h3>Q{i}: {q_name} <span class="meta">({points} pts — {q_type})</span></h3>'
        part += f'<div>{q_text}</div>'

        if q_type in ("multiple_choice_question", "true_false_question", "multiple_answers_question"):
            for ans in answers:
                weight = ans.get("weight", 0)
                css = "correct" if weight > 0 else "incorrect"
                icon = "&#10003;" if weight > 0 else "&#10007;"
                ans_text = ans.get("html", "") or ans.get("text", "")
                part += f'<div class="answer {css}">{icon} {ans_text}</div>'

                for fb_key in ("comments", "comments_html"):
                    fb = ans.get(fb_key, "")
                    if fb:
                        part += f'<div class="feedback">{fb}</div>'
                        break

        elif q_type == "matching_question":
            part += '<table><tr><th>Left</th><th>Right (correct match)</th></tr>'
            for ans in answers:
                left = ans.get("left", "") or ans.get("text", "")
                right = ans.get("right", "") or ans.get("match_text", "")
                part += f'<tr><td>{left}</td><td>{right}</td></tr>'
            part += '</table>'

        elif q_type in ("short_answer_question", "fill_in_multiple_blanks_question"):
            part += '<p><strong>Accepted answers:</strong></p><ul>'
            for ans in answers:
                ans_text = ans.get("text", "")
                part += f'<li>{ans_text}</li>'
            part += '</ul>'

        elif q_type == "numerical_question":
            for ans in answers:
                exact = ans.get("exact")
                margin = ans.get("margin")
                start = ans.get("start")
                end = ans.get("end")
                if exact is not None:
                    part += f'<p><strong>Answer:</strong> {exact}'
                    if margin:
                        part += f' &plusmn; {margin}'
                    part += '</p>'
                elif start is not None and end is not None:
                    part += f'<p><strong>Range:</strong> {start} to {end}</p>'

        elif q_type in ("essay_question", "file_upload_question"):
            part += '<p><em>[Open-ended — no predefined answer]</em></p>'

        elif q_type == "multiple_dropdowns_question":
            part += '<p><strong>Correct selections:</strong></p><ul>'
            for ans in answers:
                if ans.get("weight", 0) > 0:
                    blank_id = ans.get("blank_id", "")
                    ans_text = ans.get("text", "")
                    part += f'<li>{blank_id}: {ans_text}</li>'
            part += '</ul>'

        else:
            if answers:
                part += '<p><strong>Answers:</strong></p><ul>'
                for ans in answers:
                    ans_text = ans.get("html", "") or ans.get("text", str(ans))
                    part += f'<li>{ans_text}</li>'
                part += '</ul>'

        # General question feedback
        for fb_field in ("correct_comments_html", "correct_comments"):
            fb = q.get(fb_field, "")
            if fb:
                part += f'<div class="feedback"><strong>If correct:</strong> {fb}</div>'
                break

        for fb_field in ("incorrect_comments_html", "incorrect_comments"):
            fb = q.get(fb_field, "")
            if fb:
                part += f'<div class="feedback"><strong>If incorrect:</strong> {fb}</div>'
                break

        for fb_field in ("neutral_comments_html", "neutral_comments"):
            fb = q.get(fb_field, "")
            if fb:
                part += f'<div class="feedback"><strong>Note:</strong> {fb}</div>'
                break

        part += '</div>'
        html_parts.append(part)

    return "\n".join(html_parts)


# =========================================================
# PASS 2 — REWRITE LINKS (regex-based to preserve HTML)
# =========================================================

def rewrite_html_links(html_file_path):
    """Rewrite Canvas URLs and asset placeholders in a saved HTML file.
    Uses regex-based replacement to preserve original HTML formatting."""

    try:
        with open(html_file_path, "r", encoding="utf-8") as f:
            html = f.read()
    except Exception as e:
        print(f"  Error reading {html_file_path}: {e}")
        return

    original_html = html
    page_dir = os.path.dirname(html_file_path)
    assets_dir = os.path.join(OUTPUT_DIR, "assets")

    # --- 1. Resolve {{ASSET:filename}} placeholders ---
    def resolve_asset_placeholder(match):
        filename = match.group(1)
        asset_path = os.path.join(assets_dir, filename)
        rel_path = os.path.relpath(asset_path, page_dir).replace("\\", "/")
        return rel_path

    html = re.sub(r'\{\{ASSET:(.*?)\}\}', resolve_asset_placeholder, html)

    # --- 2. Rewrite Canvas page links ---
    # Match href="https://institution.instructure.com/courses/XXXXX/pages/slug"
    def rewrite_page_link(match):
        prefix = match.group(1)  # href=" or src="
        url = match.group(2)
        suffix = match.group(3)  # closing quote

        slug = extract_page_slug(url)
        if slug and slug in PAGE_SLUG_TO_LOCAL:
            local_path = PAGE_SLUG_TO_LOCAL[slug]
            rel_path = os.path.relpath(local_path, page_dir).replace("\\", "/")
            return f'{prefix}{rel_path}{suffix}'

        assignment_id = extract_assignment_id(url)
        if assignment_id and assignment_id in ASSIGNMENT_ID_TO_LOCAL:
            local_path = ASSIGNMENT_ID_TO_LOCAL[assignment_id]
            rel_path = os.path.relpath(local_path, page_dir).replace("\\", "/")
            return f'{prefix}{rel_path}{suffix}'

        discussion_id = extract_discussion_id(url)
        if discussion_id and discussion_id in DISCUSSION_ID_TO_LOCAL:
            local_path = DISCUSSION_ID_TO_LOCAL[discussion_id]
            rel_path = os.path.relpath(local_path, page_dir).replace("\\", "/")
            return f'{prefix}{rel_path}{suffix}'

        quiz_id = extract_quiz_id(url)
        if quiz_id and quiz_id in QUIZ_ID_TO_LOCAL:
            local_path = QUIZ_ID_TO_LOCAL[quiz_id]
            rel_path = os.path.relpath(local_path, page_dir).replace("\\", "/")
            return f'{prefix}{rel_path}{suffix}'

        file_id = extract_file_id(url)
        if file_id and file_id in FILE_ID_TO_LOCAL:
            local_path = FILE_ID_TO_LOCAL[file_id]
            rel_path = os.path.relpath(local_path, page_dir).replace("\\", "/")
            return f'{prefix}{rel_path}{suffix}'

        if file_id and file_id in SKIPPED_FILES:
            return f'{prefix}#{suffix}'

        # Unresolved — mark it
        return match.group(0)

    # Match href="..." or src="..." containing Canvas URLs
    canvas_host = urlparse(CANVAS_BASE_URL).netloc.replace(".", r"\.")
    canvas_url_pattern = re.compile(
        r'((?:href|src|data)=["\'])'
        r'((?:https?://(?:' + canvas_host + r')|/(?!/))[^"\']*)'
        r'(["\'])',
        re.IGNORECASE
    )
    html = canvas_url_pattern.sub(rewrite_page_link, html)

    # --- 3. Handle data-api-endpoint attributes ---
    # Extract page slugs from data-api-endpoint and rewrite the corresponding href
    def rewrite_api_endpoint_block(match):
        full_match = match.group(0)
        endpoint_url = match.group(1)

        # Try to extract a page slug from the endpoint
        slug = extract_page_slug(endpoint_url)
        if slug and slug in PAGE_SLUG_TO_LOCAL:
            local_path = PAGE_SLUG_TO_LOCAL[slug]
            rel_path = os.path.relpath(local_path, page_dir).replace("\\", "/")
            # Replace the href in this tag if it still points to Canvas
            full_match = re.sub(
                r'href=["\'][^"\']*["\']',
                f'href="{rel_path}"',
                full_match
            )
            return full_match

        assignment_id = extract_assignment_id(endpoint_url)
        if assignment_id and assignment_id in ASSIGNMENT_ID_TO_LOCAL:
            local_path = ASSIGNMENT_ID_TO_LOCAL[assignment_id]
            rel_path = os.path.relpath(local_path, page_dir).replace("\\", "/")
            full_match = re.sub(
                r'href=["\'][^"\']*["\']',
                f'href="{rel_path}"',
                full_match
            )
            return full_match

        discussion_id = extract_discussion_id(endpoint_url)
        if discussion_id and discussion_id in DISCUSSION_ID_TO_LOCAL:
            local_path = DISCUSSION_ID_TO_LOCAL[discussion_id]
            rel_path = os.path.relpath(local_path, page_dir).replace("\\", "/")
            full_match = re.sub(
                r'href=["\'][^"\']*["\']',
                f'href="{rel_path}"',
                full_match
            )
            return full_match

        quiz_id = extract_quiz_id(endpoint_url)
        if quiz_id and quiz_id in QUIZ_ID_TO_LOCAL:
            local_path = QUIZ_ID_TO_LOCAL[quiz_id]
            rel_path = os.path.relpath(local_path, page_dir).replace("\\", "/")
            full_match = re.sub(
                r'href=["\'][^"\']*["\']',
                f'href="{rel_path}"',
                full_match
            )
            return full_match

        return full_match

    # Match tags with data-api-endpoint attribute
    api_endpoint_pattern = re.compile(
        r'<[^>]*data-api-endpoint=["\']([^"\']*)["\'][^>]*>',
        re.IGNORECASE
    )
    html = api_endpoint_pattern.sub(rewrite_api_endpoint_block, html)

    # --- 4. Mark remaining unresolved Canvas links ---
    def mark_unresolved(match):
        prefix = match.group(1)
        url = match.group(2)
        suffix = match.group(3)
        # Only mark if it's still pointing to Canvas
        if is_canvas_url(url):
            # Add class for visual indication
            return f'{prefix}{url}{suffix}'
        return match.group(0)

    # Only write if changed
    if html != original_html:
        with open(html_file_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  Rewrote links in: {html_file_path}")


# =========================================================
# PROCESS MODULE ITEMS
# =========================================================

def process_page_item(item, module_dir, assets_dir):
    """Process a Page module item. Returns (filename, title) or None."""
    page_url = item.get("page_url")
    if not page_url:
        return None

    page = fetch_page(page_url)
    if not page:
        return None

    if not page.get("published", True):
        print(f"  [SKIPPED - unpublished] Page: {page.get('title', page_url)}")
        return None

    page_title = page["title"]
    page_body = page.get("body", "") or ""

    localized_html = localize_html(page_body, assets_dir)

    page_filename = sanitize_filename(page_title) + ".html"
    page_path = os.path.join(module_dir, page_filename)

    root_index = os.path.join(OUTPUT_DIR, "index.html")
    back_link = os.path.relpath(root_index, module_dir).replace("\\", "/")

    full_html = wrap_html(page_title, localized_html, back_link=back_link)
    save_text(page_path, full_html)

    slug = page.get("url") or page_url
    with _state_lock:
        PAGE_SLUG_TO_LOCAL[slug] = page_path
        HTML_FILES_TO_REWRITE.append(page_path)

    return page_filename, page_title


def process_assignment_item(item, module_dir, assets_dir):
    """Process an Assignment module item. Returns (filename, title) or None."""
    content_id = item.get("content_id")
    if not content_id:
        return None

    assignment = fetch_assignment(content_id)
    if not assignment:
        return None

    if not assignment.get("published", True):
        print(f"  [SKIPPED - unpublished] Assignment: {assignment.get('name', content_id)}")
        return None

    title = assignment.get("name", f"Assignment {content_id}")
    description = assignment.get("description", "") or ""

    localized_html = localize_html(description, assets_dir)

    meta_parts = []
    if assignment.get("due_at"):
        meta_parts.append(f"<strong>Due:</strong> {assignment['due_at']}")
    if assignment.get("points_possible") is not None:
        meta_parts.append(f"<strong>Points:</strong> {assignment['points_possible']}")
    if assignment.get("submission_types"):
        meta_parts.append(f"<strong>Submission:</strong> {', '.join(assignment['submission_types'])}")

    meta_html = ""
    if meta_parts:
        meta_html = '<div class="meta">' + " | ".join(meta_parts) + '</div>'

    body = meta_html + localized_html

    filename = sanitize_filename(title) + ".html"
    filepath = os.path.join(module_dir, filename)

    root_index = os.path.join(OUTPUT_DIR, "index.html")
    back_link = os.path.relpath(root_index, module_dir).replace("\\", "/")

    full_html = wrap_html(title, body, back_link=back_link)
    save_text(filepath, full_html)

    with _state_lock:
        ASSIGNMENT_ID_TO_LOCAL[str(content_id)] = filepath
        HTML_FILES_TO_REWRITE.append(filepath)

    return filename, title


def process_discussion_item(item, module_dir, assets_dir):
    """Process a Discussion module item. Returns (filename, title) or None."""
    content_id = item.get("content_id")
    if not content_id:
        return None

    discussion = fetch_discussion(content_id)
    if not discussion:
        return None

    if not discussion.get("published", True):
        print(f"  [SKIPPED - unpublished] Discussion: {discussion.get('title', content_id)}")
        return None

    title = discussion.get("title", f"Discussion {content_id}")
    message = discussion.get("message", "") or ""

    localized_html = localize_html(message, assets_dir)

    filename = sanitize_filename(title) + ".html"
    filepath = os.path.join(module_dir, filename)

    root_index = os.path.join(OUTPUT_DIR, "index.html")
    back_link = os.path.relpath(root_index, module_dir).replace("\\", "/")

    full_html = wrap_html(title, localized_html, back_link=back_link)
    save_text(filepath, full_html)

    with _state_lock:
        DISCUSSION_ID_TO_LOCAL[str(content_id)] = filepath
        HTML_FILES_TO_REWRITE.append(filepath)

    return filename, title


def process_quiz_item(item, module_dir, assets_dir):
    """Process a Quiz module item. Returns (filename, title) or None."""
    content_id = item.get("content_id")
    if not content_id:
        return None

    quiz = fetch_quiz(content_id)
    if not quiz:
        return None

    if not quiz.get("published", True):
        print(f"  [SKIPPED - unpublished] Quiz: {quiz.get('title', content_id)}")
        return None

    title = quiz.get("title", f"Quiz {content_id}")
    description = quiz.get("description", "") or ""

    localized_desc = localize_html(description, assets_dir)

    meta_parts = []
    if quiz.get("due_at"):
        meta_parts.append(f"<strong>Due:</strong> {quiz['due_at']}")
    if quiz.get("points_possible") is not None:
        meta_parts.append(f"<strong>Points:</strong> {quiz['points_possible']}")
    if quiz.get("time_limit"):
        meta_parts.append(f"<strong>Time limit:</strong> {quiz['time_limit']} min")
    if quiz.get("allowed_attempts"):
        attempts = quiz["allowed_attempts"]
        meta_parts.append(f"<strong>Attempts:</strong> {'Unlimited' if attempts == -1 else attempts}")
    if quiz.get("quiz_type"):
        meta_parts.append(f"<strong>Type:</strong> {quiz['quiz_type']}")

    meta_html = ""
    if meta_parts:
        meta_html = '<div class="meta">' + " | ".join(meta_parts) + '</div>'

    # Fetch questions
    questions = fetch_quiz_questions(content_id)
    questions_html = ""
    if questions:
        # Localize any images in questions
        raw_q_html = render_quiz_questions(questions)
        questions_html = localize_html(raw_q_html, assets_dir)
    else:
        questions_html = "<p><em>No questions available (this may be a New Quizzes quiz, or the quiz has no questions yet).</em></p>"

    body = meta_html + localized_desc + "<hr>" + questions_html

    filename = sanitize_filename(title) + ".html"
    filepath = os.path.join(module_dir, filename)

    root_index = os.path.join(OUTPUT_DIR, "index.html")
    back_link = os.path.relpath(root_index, module_dir).replace("\\", "/")

    full_html = wrap_html(title, body, back_link=back_link)
    save_text(filepath, full_html)

    with _state_lock:
        QUIZ_ID_TO_LOCAL[str(content_id)] = filepath
        HTML_FILES_TO_REWRITE.append(filepath)

    return filename, title


def process_file_item(item, module_dir, assets_dir):
    """Process a File module item. Returns (filename, title, is_link_to_asset) or None."""
    content_id = item.get("content_id")
    if not content_id:
        return None

    if not item.get("published", True):
        print(f"  [SKIPPED - unpublished] File: {item.get('title', content_id)}")
        return None

    filename, download_url, published, finfo = get_filename_for_file(str(content_id))

    if not published:
        with _state_lock:
            SKIPPED_FILES.add(str(content_id))
        print(f"  [SKIPPED - unpublished] File: {filename} (ID: {content_id})")
        return None

    local_path = os.path.join(assets_dir, filename)

    if not os.path.exists(local_path):
        dl_url = download_url or f"{CANVAS_BASE_URL}/files/{content_id}/download"
        download_file(dl_url, local_path)

    with _state_lock:
        DOWNLOADED_FILES[str(content_id)] = local_path
        FILE_ID_TO_LOCAL[str(content_id)] = local_path

    # Return info for module index — link directly to the asset
    rel_path = os.path.relpath(local_path, module_dir).replace("\\", "/")
    return rel_path, filename, True


def process_external_url_item(item, module_dir):
    """Process an ExternalUrl module item. Returns (url, title) or None."""
    if not item.get("published", True):
        print(f"  [SKIPPED - unpublished] ExternalUrl: {item.get('title', 'Unknown')}")
        return None

    title = item.get("title", "External Link")
    url = item.get("external_url", "")

    if not url:
        return None

    return url, title


# =========================================================
# EXPORT COURSE
# =========================================================

def export_course(course_id):
    global OUTPUT_DIR

    reset_state(course_id)

    # Get course name for folder
    try:
        course_name = get_course_name(course_id)
    except Exception as e:
        print(f"  Could not fetch course name: {e}")
        course_name = f"canvas_course_{course_id}"

    OUTPUT_DIR = sanitize_filename(course_name)
    ensure_dir(OUTPUT_DIR)

    assets_dir = os.path.join(OUTPUT_DIR, "assets")
    ensure_dir(assets_dir)

    print(f"\n{'='*60}")
    print(f"EXPORTING: {course_name} (ID: {course_id})")
    print(f"Output: {OUTPUT_DIR}/")
    print(f"{'='*60}")

    # ---------------------------------------------------------
    # FETCH ALL PAGES (for slug mapping and orphan detection)
    # ---------------------------------------------------------

    print("\nFetching all course pages...")
    all_pages_url = f"{CANVAS_BASE_URL}/api/v1/courses/{course_id}/pages?per_page=100"
    all_pages = get_paginated(all_pages_url)

    # Track which page slugs appear in modules
    module_page_slugs = set()

    # Build a lookup of slug -> page summary
    all_page_lookup = {}
    for p in all_pages:
        slug = p.get("url", "")
        if slug:
            all_page_lookup[slug] = p

    print(f"  Found {len(all_pages)} total pages in course")

    # ---------------------------------------------------------
    # FETCH MODULES
    # ---------------------------------------------------------

    print("\nFetching modules...")
    modules_url = f"{CANVAS_BASE_URL}/api/v1/courses/{course_id}/modules?per_page=100"
    modules = get_paginated(modules_url)
    print(f"  Found {len(modules)} modules")

    # Root index HTML
    root_index_path = os.path.join(OUTPUT_DIR, "index.html")

    index_entries = []

    # ---------------------------------------------------------
    # PROCESS MODULES
    # ---------------------------------------------------------

    for module in modules:
        module_name = module["name"]

        if not module.get("published", True):
            print(f"\n[SKIPPED - unpublished] MODULE: {module_name}")
            continue

        print(f"\n=== MODULE: {module_name} ===")

        module_items_url = (
            f"{CANVAS_BASE_URL}/api/v1/courses/{course_id}"
            f"/modules/{module['id']}/items?per_page=100"
        )
        items = get_paginated(module_items_url)

        module_slug = sanitize_filename(module_name)
        module_dir = os.path.join(OUTPUT_DIR, "modules", module_slug)
        ensure_dir(module_dir)

        module_entries = []  # (link, title, is_external)

        for item in items:
            item_type = item.get("type")
            item_title = item.get("title", "Untitled")

            if not item.get("published", True):
                print(f"  [SKIPPED - unpublished] {item_type}: {item_title}")
                continue

            print(f"  - {item_type}: {item_title}")

            try:
                if item_type == "Page":
                    page_url = item.get("page_url", "")
                    if page_url:
                        module_page_slugs.add(page_url)
                    result = process_page_item(item, module_dir, assets_dir)
                    if result:
                        module_entries.append((result[0], result[1], False))

                elif item_type == "Assignment":
                    result = process_assignment_item(item, module_dir, assets_dir)
                    if result:
                        module_entries.append((result[0], result[1], False))

                elif item_type == "Discussion":
                    result = process_discussion_item(item, module_dir, assets_dir)
                    if result:
                        module_entries.append((result[0], result[1], False))

                elif item_type == "Quiz":
                    result = process_quiz_item(item, module_dir, assets_dir)
                    if result:
                        module_entries.append((result[0], result[1], False))

                elif item_type == "File":
                    result = process_file_item(item, module_dir, assets_dir)
                    if result:
                        module_entries.append((result[0], result[1], False))

                elif item_type == "ExternalUrl":
                    result = process_external_url_item(item, module_dir)
                    if result:
                        module_entries.append((result[0], result[1], True))

                elif item_type == "ExternalTool":
                    if item.get("published", True):
                        ext_url = item.get("external_url") or item.get("url") or "#"
                        module_entries.append((ext_url, item_title + " (External Tool)", True))

                elif item_type == "SubHeader":
                    module_entries.append((None, item_title, False))

            except Exception as e:
                print(f"    Error processing item: {e}")

        # Build module index
        module_items_html = "<ul>\n"
        for link, title, is_external in module_entries:
            if link is None:
                # SubHeader
                module_items_html += f'</ul>\n<h3>{title}</h3>\n<ul>\n'
            elif is_external:
                module_items_html += f'<li><a href="{link}" target="_blank">{title} &#8599;</a></li>\n'
            else:
                module_items_html += f'<li><a href="{link}">{title}</a></li>\n'
        module_items_html += "</ul>"

        module_index_path = os.path.join(module_dir, "index.html")
        back_link = os.path.relpath(root_index_path, module_dir).replace("\\", "/")
        module_index_html = wrap_html(module_name, module_items_html, back_link=back_link)
        save_text(module_index_path, module_index_html)

        rel_module_index = os.path.relpath(module_index_path, OUTPUT_DIR).replace("\\", "/")
        index_entries.append((rel_module_index, module_name))

    # ---------------------------------------------------------
    # ORPHAN PAGES (pages not in any module)
    # ---------------------------------------------------------

    orphan_slugs = [
        slug for slug in all_page_lookup
        if slug not in module_page_slugs
    ]

    if orphan_slugs:
        print(f"\n=== PAGES NOT IN MODULES ({len(orphan_slugs)}) ===")

        orphan_dir = os.path.join(OUTPUT_DIR, "pages_not_in_modules")
        ensure_dir(orphan_dir)

        orphan_entries = []  # (filename, title)

        def process_orphan_page(slug):
            page_summary = all_page_lookup[slug]

            if not page_summary.get("published", True):
                print(f"  [SKIPPED - unpublished] Orphan page: {slug}")
                return None

            page = fetch_page(slug)
            if not page:
                return None

            if not page.get("published", True):
                print(f"  [SKIPPED - unpublished] Orphan page: {slug}")
                return None

            title = page.get("title", slug)
            body = page.get("body", "") or ""

            print(f"  - Orphan page: {title}")

            localized = localize_html(body, assets_dir)

            filename = sanitize_filename(title) + ".html"
            filepath = os.path.join(orphan_dir, filename)

            back_link = os.path.relpath(root_index_path, orphan_dir).replace("\\", "/")
            full_html = wrap_html(title, localized, back_link=back_link)
            save_text(filepath, full_html)

            page_slug = page.get("url") or slug
            with _state_lock:
                PAGE_SLUG_TO_LOCAL[page_slug] = filepath
                HTML_FILES_TO_REWRITE.append(filepath)

            return filename, title

        with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
            futures = {
                executor.submit(process_orphan_page, slug): slug
                for slug in orphan_slugs
            }
            for future in as_completed(futures):
                result = future.result()
                if result:
                    orphan_entries.append(result)

        # Sort alphabetically by title (case-insensitive)
        orphan_entries.sort(key=lambda x: x[1].lower())

        if orphan_entries:
            orphan_items_html = "<ul>\n"
            for filename, title in orphan_entries:
                orphan_items_html += f'<li><a href="{filename}">{title}</a></li>\n'
            orphan_items_html += "</ul>"

            orphan_index_path = os.path.join(orphan_dir, "index.html")
            back_link = os.path.relpath(root_index_path, orphan_dir).replace("\\", "/")
            orphan_index_html = wrap_html("Pages Not In Modules", orphan_items_html, back_link=back_link)
            save_text(orphan_index_path, orphan_index_html)

            rel_orphan_index = os.path.relpath(orphan_index_path, OUTPUT_DIR).replace("\\", "/")
            index_entries.append((rel_orphan_index, "Pages Not In Modules"))

    # ---------------------------------------------------------
    # PASS 2 — REWRITE ALL INTERNAL LINKS
    # ---------------------------------------------------------

    print(f"\n=== PASS 2: Rewriting links in {len(HTML_FILES_TO_REWRITE)} files ===")

    with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
        futures = [
            executor.submit(rewrite_html_links, fp)
            for fp in HTML_FILES_TO_REWRITE
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"  Error in link rewriting: {e}")

    # ---------------------------------------------------------
    # BUILD ROOT INDEX
    # ---------------------------------------------------------

    index_items_html = "<ul>\n"
    for link, title in index_entries:
        index_items_html += f'<li><a href="{link}">{title}</a></li>\n'
    index_items_html += "</ul>"

    index_html = wrap_html(
        f"{course_name} — Course Export",
        index_items_html,
        back_link="#"
    )
    save_text(root_index_path, index_html)

    # ---------------------------------------------------------
    # ZIP THE COURSE FOLDER
    # ---------------------------------------------------------

    print(f"\nZipping course folder...")
    try:
        zip_path = shutil.make_archive(OUTPUT_DIR, 'zip', '.', OUTPUT_DIR)
        print(f"  Created: {zip_path}")
    except Exception as e:
        print(f"  Error creating zip: {e}")

    # ---------------------------------------------------------
    # SUMMARY
    # ---------------------------------------------------------

    print(f"\n{'='*60}")
    print(f"EXPORT COMPLETE: {course_name}")
    print(f"  Output folder: {OUTPUT_DIR}/")
    print(f"  Files downloaded: {len(DOWNLOADED_FILES)}")
    print(f"  Files skipped (unpublished): {len(SKIPPED_FILES)}")
    print(f"  HTML pages saved: {len(HTML_FILES_TO_REWRITE)}")
    print(f"{'='*60}")


# =========================================================
# RUN
# =========================================================

for cid in COURSE_IDS:
    try:
        export_course(cid)
    except Exception as e:
        import traceback
        print(f"\nERROR exporting course {cid}: {e}")
        traceback.print_exc()

print(f"\n\nALL DONE — processed {len(COURSE_IDS)} course(s)")
