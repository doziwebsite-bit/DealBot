import os, re, json, time, hashlib, urllib.parse, pathlib, textwrap, yaml
from datetime import datetime
import feedparser
import requests
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader, select_autoescape
import markdown

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load(open(ROOT / "config.yml", "r", encoding="utf-8"))

DIST = ROOT / "dist"
CONTENT = ROOT / "content"
TEMPLATES = ROOT / "templates"
PAGES = ROOT / "pages"
ASSETS = ROOT / "assets"
DIST.mkdir(exist_ok=True)
(CONTENT).mkdir(exist_ok=True)
(DIST / "posts").mkdir(parents=True, exist_ok=True)

def normalize_url_with_affiliate(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        aff_map = CONFIG.get("affiliate_map", {}) or {}
        for domain, qs in aff_map.items():
            if domain in host and qs:
                q = urllib.parse.parse_qs(parsed.query)
                # merge qs parameters
                for kv in qs.split("&"):
                    if not kv.strip():
                        continue
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        q[k] = [v]
                new_q = urllib.parse.urlencode({k:v[0] for k,v in q.items()})
                parsed = parsed._replace(query=new_q)
                return urllib.parse.urlunparse(parsed)
    except Exception:
        pass
    return url

def clean_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()

def extractive_summary(text: str, max_chars: int = 420) -> str:
    # Very naive fallback: first sentence up to max_chars
    text = text.strip()
    if len(text) <= max_chars:
        return text
    # split into sentences by period
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = []
    length = 0
    for p in parts:
        if length + len(p) > max_chars:
            break
        out.append(p)
        length += len(p) + 1
    return " ".join(out) or text[:max_chars]

def ai_summarize(title: str, text: str) -> tuple[str, str]:
    # If OPENAI_API_KEY is set, try to use it. Otherwise, fallback.
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return (title[:96], extractive_summary(text))
    try:
        import requests
        payload = {
            "model": CONFIG["ai"].get("model", "gpt-4o-mini"),
            "messages": [
                {"role":"system","content":"Tu es un éditeur SEO-français concis. Génére un titre accrocheur (<= 85 caractères) et un résumé (<= 180 mots) clair, sans exagération."},
                {"role":"user","content": f"Titre source: {title}\nTexte: {text[:4000]}"}
            ],
            "max_tokens": 500,
            "temperature": 0.4
        }
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          headers={"Authorization": f"Bearer {api_key}","Content-Type":"application/json"},
                          json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        # Expecting two parts separated by \n\n
        parts = content.strip().split("\n\n", 1)
        if len(parts) == 2:
            new_title, summary = parts
        else:
            # Heuristic: first line as title
            lines = content.strip().splitlines()
            new_title = lines[0][:85]
            summary = "\n".join(lines[1:])
        return (new_title.strip()[:96], summary.strip())
    except Exception as e:
        return (title[:96], extractive_summary(text))

def slugify(s: str) -> str:
    s = re.sub(r"[^\w\- ]+", "", s.lower()).strip().replace(" ", "-")
    return re.sub(r"-+", "-", s)

def fetch_posts():
    items = []
    for url in CONFIG["feeds"]:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                title = e.get("title", "").strip()
                link = e.get("link", "").strip()
                summary = e.get("summary", "") or e.get("description","") or ""
                content = clean_text(summary)
                if not title or not link:
                    continue
                # filter by keywords
                hay = (title + " " + content).lower()
                if CONFIG["keywords"]:
                    if not any(k.lower() in hay for k in CONFIG["keywords"]):
                        continue
                # fetch original page to enrich (optional)
                try:
                    resp = requests.get(link, timeout=10, headers={"User-Agent":"Mozilla/5.0 Autoblog/1.0"})
                    if resp.ok:
                        page_text = clean_text(resp.text)
                        if len(page_text) > len(content):
                            content = page_text
                except Exception:
                    pass
                items.append({
                    "title": title,
                    "link": normalize_url_with_affiliate(link),
                    "content": content,
                    "published": e.get("published", "") or e.get("updated","") or "",
                    "source": feed.feed.get("title","Unknown")
                })
        except Exception as ex:
            print("Feed error:", url, ex)
    return items

def render_site(items):
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    posts = []
    daily_limit = CONFIG.get("daily_post_limit", 12) or 12
    items = sorted(items, key=lambda x: x.get("published",""), reverse=True)[:daily_limit]
    for it in items:
        title_ai, summary_ai = ai_summarize(it["title"], it["content"])
        slug = slugify(title_ai)[:60]
        post_path = DIST / "posts" / f"{slug}.html"
        tpl = env.get_template("post.html")
        html = tpl.render(
            site=CONFIG,
            title=title_ai,
            summary=summary_ai,
            link=it["link"],
            source=it["source"],
            published=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        )
        post_path.write_text(html, encoding="utf-8")
        posts.append({
            "title": title_ai,
            "href": f"posts/{slug}.html",
            "summary": summary_ai
        })
    # index
    index_tpl = env.get_template("index.html")
    (DIST / "index.html").write_text(index_tpl.render(site=CONFIG, posts=posts), encoding="utf-8")
    # copy assets
    import shutil
    if ASSETS.exists():
        shutil.copytree(ASSETS, DIST / "assets", dirs_exist_ok=True)
    # copy pages/*.md
    if PAGES.exists():
        for p in PAGES.glob("*.md"):
            html = markdown.markdown(p.read_text(encoding="utf-8"))
            page_html = env.get_template("page.html").render(site=CONFIG, content=html, title=p.stem.title())
            (DIST / f"{p.stem}.html").write_text(page_html, encoding="utf-8")

def main():
    items = fetch_posts()
    if not items:
        print("No items after filtering; please tweak keywords/feeds in config.yml")
    render_site(items)
    print("Built site into dist/")

if __name__ == "__main__":
    main()