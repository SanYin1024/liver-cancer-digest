#!/usr/bin/env python3
import json
import os
import base64
import webbrowser
import ssl
import sys
import threading
import xml.etree.ElementTree as ET
import argparse
import re
import time
from email.utils import formataddr
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

try:
    import certifi
except Exception:  # pragma: no cover
    certifi = None

try:
    import markdown as mdlib
except Exception:  # pragma: no cover
    mdlib = None


DEFAULT_QUERY = '("Liver Neoplasms"[MeSH Terms] OR hepatocellular carcinoma[Title/Abstract] OR HCC[Title/Abstract] OR cholangiocarcinoma[Title/Abstract] OR "liver cancer"[Title/Abstract] OR "hepatic cancer"[Title/Abstract])'
ENV_LOADED = False


def load_dotenv_file(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def load_env():
    global ENV_LOADED
    if ENV_LOADED:
        return
    load_dotenv_file(Path(__file__).with_name(".env"))
    load_dotenv_file(Path.cwd() / ".env")
    ENV_LOADED = True


def env(name, default=None, required=False):
    load_env()
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def utc_today():
    return datetime.now(timezone.utc).date()


def iso_date(d):
    return d.strftime("%Y/%m/%d")


def state_path():
    return Path(env("STATE_FILE", str(Path.home() / ".liver_cancer_digest_state.json")))


def log_path():
    return Path(env("LOG_FILE", str(Path.home() / ".liver_cancer_digest.log")))


def load_state():
    path = state_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def save_state(state):
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def append_log(entry):
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = entry.get("ts") or datetime.now(timezone.utc).isoformat()
    try:
        ts_text = datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        ts_text = ts
    status_map = {
        "sent": "成功",
        "skipped": "跳过",
        "error": "失败",
    }
    status = status_map.get(entry.get("status"), entry.get("status", "未知"))
    recipient = entry.get("recipient") or env("GMAIL_TO", env("GMAIL_FROM", "未设置"))
    fetched = entry.get("fetched", "-")
    selected = entry.get("selected", "-")
    subject = entry.get("subject", "")
    reason = entry.get("reason") or entry.get("error") or ""
    parts = [
        ts_text,
        f"收件人: {recipient}",
        f"条数: {selected}/{fetched}",
        status,
    ]
    if subject:
        parts.append(subject)
    if reason:
        parts.append(str(reason))
    with path.open("a", encoding="utf-8") as f:
        f.write(" | ".join(parts) + "\n")


def http_json(url, method="GET", data=None, headers=None, timeout=30):
    req = Request(url, data=data, method=method, headers=headers or {})
    context = ssl.create_default_context(cafile=certifi.where()) if certifi else ssl.create_default_context()
    last_error = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout, context=context) as resp:
                raw = resp.read()
                text = raw.decode("utf-8", errors="replace")
                try:
                    return json.loads(text), text
                except json.JSONDecodeError:
                    return None, text
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                continue
            raise
    raise last_error


def pubmed_search(query, start_date, end_date, retmax=100):
    term = f'({query}) AND ({start_date}:{end_date}[Date - Publication])'
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pubmed&term={quote(term)}&retmax={retmax}&retmode=json&sort=pub_date&tool=liver_cancer_digest"
    )
    payload, raw = http_json(url, timeout=30)
    if not payload:
        raise RuntimeError(f"PubMed esearch returned invalid JSON: {raw[:300]}")
    result = payload.get("esearchresult", {})
    return result.get("idlist", []), term


def pubmed_summaries(pmids):
    papers = []
    chunk_size = int(env("PUBMED_BATCH_SIZE", "100"))
    for i in range(0, len(pmids), chunk_size):
        batch = pmids[i : i + chunk_size]
        body = f"db=pubmed&id={quote(','.join(batch))}&retmode=json&tool=liver_cancer_digest".encode("utf-8")
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        payload, raw = http_json(
            url,
            method="POST",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if not payload:
            raise RuntimeError(f"PubMed esummary returned invalid JSON: {raw[:300]}")
        result = payload.get("result", {})
        for uid in result.get("uids", []):
            p = result.get(uid, {})
            doi = ""
            for item in p.get("articleids", []):
                if item.get("idtype") == "doi":
                    doi = item.get("value", "")
                    break
            authors = ", ".join(a.get("name", "") for a in p.get("authors", [])[:6] if a.get("name"))
            papers.append(
                {
                    "pmid": uid,
                    "title": p.get("title", ""),
                    "journal": p.get("fulljournalname", "") or p.get("source", ""),
                    "pubdate": p.get("pubdate", ""),
                    "authors": authors,
                    "doi": doi,
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                }
            )
    return papers


def pubmed_abstracts(pmids):
    abstracts = {}
    chunk_size = int(env("PUBMED_BATCH_SIZE", "100"))
    for i in range(0, len(pmids), chunk_size):
        batch = pmids[i : i + chunk_size]
        body = f"db=pubmed&id={quote(','.join(batch))}&retmode=xml&tool=liver_cancer_digest".encode("utf-8")
        payload, raw = http_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            method="POST",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=60,
        )
        xml_text = raw if raw else ""
        if not xml_text.strip().startswith("<"):
            raise RuntimeError(f"PubMed efetch returned unexpected content: {xml_text[:300]}")
        root = ET.fromstring(xml_text)
        for article in root.findall(".//PubmedArticle"):
            pmid = article.findtext(".//MedlineCitation/PMID") or ""
            title = article.findtext(".//Article/ArticleTitle") or ""
            abstract_parts = [
                (node.text or "").strip()
                for node in article.findall(".//Article/Abstract/AbstractText")
                if (node.text or "").strip()
            ]
            abstracts[pmid] = {
                "title": title.strip(),
                "abstract": " ".join(abstract_parts).strip(),
            }
    return abstracts


def score_paper(paper):
    text = f"{paper.get('title', '')} {paper.get('abstract', '')}".lower()
    score = 0
    direct_hits = [
        "hepatocellular carcinoma",
        "hcc",
        "cholangiocarcinoma",
        "liver cancer",
        "hepatic cancer",
        "liver neoplasm",
    ]
    topical_hits = [
        "immunotherapy",
        "immune",
        "tace",
        "tki",
        "targeted",
        "metastasis",
        "fibrosis",
        "mash",
        "tumor",
        "tumour",
        "biomarker",
        "prognosis",
        "transcript",
        "epigen",
        "metabolic",
        "microenvironment",
        "gpc3",
        "fgf",
        "fgfr",
    ]
    for token in direct_hits:
        if token in text:
            score += 3
    for token in topical_hits:
        if token in text:
            score += 1
    if len(text) < 120:
        score -= 1
    return max(score, 0)


def build_prompt(start_label, end_label, query, papers):
    return f"""你是一名医学文献助理。请用中文生成一封简洁但有信息密度的每日肝癌文献简报。
不要使用 emoji。
不要输出 Markdown 里的 #、* 之类原始符号，直接用自然标题和段落表达。

日期范围：{start_label} 至 {end_label}（PubMed Date - Publication）
检索式：{query}

请输出：
1. 开头一句总结今日新增数量。
2. 按“直接相关：HCC/胆管癌/其他原发性肝癌”和“间接相关：肝转移/肝纤维化/背景机制”等分组。
3. 每篇包括标题、期刊、日期、PMID、DOI、链接、为什么值得看。
4. 最后给出3条今日研究热点。
5. 结构清晰，使用简洁小标题和短段落，不要花哨排版，不要 emoji，不要输出原始 Markdown 符号。

文献元数据 JSON：
{json.dumps(papers, ensure_ascii=False, indent=2)}
"""


def build_relevance_prompt(query, papers):
    return f"""你在做 PubMed 文献初筛。请根据标题和摘要，判断哪些文献最值得看。

检索式：{query}

规则：
- 7-10 分：高度相关，建议保留
- 5-6 分：可能相关，备注即可
- 0-4 分：跳过

请输出 JSON 数组，每项包含：
pmid, score, decision, reason

文献：
{json.dumps(papers, ensure_ascii=False, indent=2)}
"""


def deepseek_digest(prompt):
    api_key = env("AI_API_KEY", required=True)
    model = env("AI_MODEL", "deepseek-ai/deepseek-v4-pro-0813")
    base_url = env("AI_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
    timeout = int(env("AI_TIMEOUT", "300"))
    body = json.dumps(
        {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": "你只根据用户给定的 PubMed 元数据写文献简报。不要编造摘要中未提供的信息；如果只有标题和期刊，就明确说“基于题名判断”。",
                },
                {"role": "user", "content": prompt},
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    payload, raw = http_json(
        f"{base_url}/chat/completions",
        method="POST",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    if not payload:
        raise RuntimeError(f"AI provider returned invalid JSON: {raw[:300]}")
    return payload["choices"][0]["message"]["content"]


def html_escape(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )


def strip_ai_markdown(text):
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "", text)
    return text


def markdown_to_html(text):
    text = strip_ai_markdown(text)
    lines = [line.rstrip() for line in text.splitlines()]
    html_parts = []
    in_list = False
    paragraph = []

    def flush_paragraph():
        nonlocal paragraph
        if paragraph:
            html_parts.append(f"<p>{html_escape(' '.join(paragraph))}</p>")
            paragraph = []

    def flush_list():
        nonlocal in_list
        if in_list:
            html_parts.append("</ul>")
            in_list = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        heading = re.match(r"^(?:第[一二三四五六七八九十]+[：:.]?\s*)?(.*)$", stripped)
        if stripped and len(stripped) <= 60 and (stripped.endswith("：") or stripped.endswith(":")):
            flush_paragraph()
            flush_list()
            html_parts.append(f"<h2>{html_escape(stripped.rstrip('：:'))}</h2>")
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            flush_paragraph()
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            html_parts.append(f"<li>{html_escape(stripped[2:].strip())}</li>")
            continue
        paragraph.append(stripped)
    flush_paragraph()
    flush_list()
    return "".join(html_parts) if html_parts else "<p></p>"


def render_digest_html(subject, digest, search_url, start_label, end_label, paper_count, selected_count):
    digest_html = markdown_to_html(digest)
    return f"""
    <div style="margin:0;background:#f3f4f6;padding:24px 0;">
      <div style="max-width:860px;margin:0 auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
        <div style="padding:24px 28px;border-bottom:1px solid #e5e7eb;background:#fafafa;">
          <div style="font-family:Arial,sans-serif;font-size:12px;letter-spacing:0.04em;color:#6b7280;text-transform:uppercase;">Daily Research Brief</div>
          <div style="font-family:Arial,sans-serif;font-size:24px;line-height:1.2;font-weight:700;color:#111827;margin-top:6px;">{subject}</div>
          <div style="font-family:Arial,sans-serif;font-size:13px;color:#6b7280;margin-top:8px;">
            {start_label} to {end_label} · {paper_count} papers fetched · {selected_count} screened in
          </div>
        </div>
        <div style="padding:28px;font-family:Arial,sans-serif;color:#111827;line-height:1.72;font-size:14px;">
          <div style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:10px;padding:18px 20px;white-space:normal;">
            {digest_html}
          </div>
          <div style="margin-top:20px;padding-top:16px;border-top:1px solid #e5e7eb;font-size:12px;color:#6b7280;">
            PubMed search:
            <a href="{search_url}" style="color:#2563eb;text-decoration:none;">{search_url}</a>
          </div>
        </div>
      </div>
    </div>
    """


def build_subject(date_label, suffix=None):
    subject = f"肝癌简报|{date_label}"
    if suffix:
        subject = f"{subject} | {suffix}"
    return subject


def build_email(subject, body_html):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("文献推送", env("GMAIL_FROM", required=True)))
    msg["To"] = env("GMAIL_TO", env("GMAIL_FROM", required=True))
    msg.set_content(body_html)
    msg.add_alternative(
        f"<div style='font-family: Arial, sans-serif; line-height: 1.55; color: #111;'>{body_html}</div>",
        subtype="html",
    )
    return msg


def gmail_access_token():
    client_id = env("GMAIL_CLIENT_ID", required=True)
    client_secret = env("GMAIL_CLIENT_SECRET", required=True)
    refresh_token = env("GMAIL_REFRESH_TOKEN", required=True)
    data = urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    payload, raw = http_json(
        "https://oauth2.googleapis.com/token",
        method="POST",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if not payload:
        raise RuntimeError(f"OAuth token refresh failed: {raw[:300]}")
    return payload["access_token"]


def send_gmail_api(msg):
    access_token = gmail_access_token()
    raw_msg = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    body = json.dumps({"raw": raw_msg}).encode("utf-8")
    payload, raw = http_json(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        method="POST",
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        timeout=60,
    )
    if not payload:
        raise RuntimeError(f"Gmail send failed: {raw[:300]}")
    return payload


def send_gmail(msg):
    send_gmail_api(msg)


def parse_args():
    parser = argparse.ArgumentParser(description="Daily liver cancer literature digest")
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Ignore deduplication and send the current digest even if the papers were already sent",
    )
    parser.add_argument(
        "--gmail-oauth-setup",
        action="store_true",
        help="Run the one-time Gmail OAuth setup flow",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Keep running and send once per day at SCHEDULE_TIME",
    )
    return parser.parse_args()


def schedule_timezone():
    return ZoneInfo(env("SCHEDULE_TZ", "Asia/Shanghai"))


def parse_schedule_time():
    value = env("SCHEDULE_TIME", "08:00").strip()
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        raise SystemExit("SCHEDULE_TIME must use HH:MM format, for example 08:00")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise SystemExit("SCHEDULE_TIME must be a valid 24-hour time, for example 08:00")
    return hour, minute


def next_scheduled_run(now=None):
    tz = schedule_timezone()
    hour, minute = parse_schedule_time()
    now = now or datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def run_daemon(force_send=False):
    print(f"scheduler started; daily time={env('SCHEDULE_TIME', '08:00')} tz={env('SCHEDULE_TZ', 'Asia/Shanghai')}")
    while True:
        target = next_scheduled_run()
        sleep_seconds = max(1, int((target - datetime.now(target.tzinfo)).total_seconds()))
        print(f"next run at {target.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        time.sleep(sleep_seconds)
        try:
            main(force_send=force_send)
        except Exception as exc:
            try:
                append_log(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "status": "error",
                        "error": str(exc),
                    }
                )
            except Exception:
                pass
            print(f"scheduled run failed: {exc}", file=sys.stderr)


def main(force_send=False):
    run_started_at = datetime.now(timezone.utc)
    state = load_state()
    lookback_hours = int(env("LOOKBACK_HOURS", "24"))
    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(hours=lookback_hours)
    start_date = iso_date(start_dt.date())
    end_date = iso_date(now.date())

    query = env("PUBMED_QUERY", DEFAULT_QUERY)
    pmids, resolved_query = pubmed_search(query, start_date, end_date, retmax=int(env("PUBMED_RETMAX", "100")))

    start_label = start_dt.strftime("%Y-%m-%d")
    end_label = now.strftime("%Y-%m-%d")
    search_url = f"https://pubmed.ncbi.nlm.nih.gov/?term={quote(resolved_query)}&sort=date"

    if not pmids:
        subject = build_subject(end_label, "无新增")
        html = render_digest_html(
            subject,
            f"今天没有检索到新的肝癌相关 PubMed 记录。\n\n日期范围：{start_label} 至 {end_label}",
            search_url,
            start_label,
            end_label,
            0,
            0,
        )
        msg = build_email(subject, html)
        send_gmail(msg)
        save_state({"last_run": now.isoformat(), "pmids": []})
        append_log(
            {
                "ts": run_started_at.isoformat(),
                "status": "sent",
                "mode": "no_results",
                "subject": subject,
                "recipient": env("GMAIL_TO", env("GMAIL_FROM", "")),
                "fetched": 0,
                "selected": 0,
            }
        )
        print("sent no-result email")
        return

    seen = set(state.get("pmids", []))
    fresh_pmids = [p for p in pmids if p not in seen]
    if not force_send and state.get("last_run") and not fresh_pmids:
        append_log(
            {
                "ts": run_started_at.isoformat(),
                "status": "skipped",
                "mode": "dedup",
                "reason": "no new pmids since last run",
                "recipient": env("GMAIL_TO", env("GMAIL_FROM", "")),
                "fetched": len(pmids),
                "selected": 0,
            }
        )
        print("no new pmids since last run")
        return

    papers = pubmed_summaries(fresh_pmids or pmids)
    abstract_map = pubmed_abstracts([p["pmid"] for p in papers])
    for paper in papers:
        paper.update(abstract_map.get(paper["pmid"], {"abstract": ""}))
        paper["relevance_score"] = score_paper(paper)

    papers = sorted(papers, key=lambda p: (p["relevance_score"], p.get("pubdate", "")), reverse=True)
    selected = [p for p in papers if p["relevance_score"] >= 5][:20] or papers[:10]
    save_state(
        {
            "last_run": now.isoformat(),
            "pmids": sorted(set(seen).union(pmids)),
            "last_search": {
                "query": resolved_query,
                "count": len(pmids),
                "selected": [p["pmid"] for p in selected],
            },
        }
    )

    prompt = build_prompt(start_label, end_label, resolved_query, selected)
    digest = deepseek_digest(prompt)
    subject = build_subject(end_label)
    html = render_digest_html(
        subject,
        digest,
        search_url,
        start_label,
        end_label,
        len(papers),
        len(selected),
    )
    msg = build_email(subject, html)
    send_gmail(msg)
    append_log(
        {
            "ts": run_started_at.isoformat(),
            "status": "sent",
            "mode": "digest",
            "subject": subject,
            "recipient": env("GMAIL_TO", env("GMAIL_FROM", "")),
            "fetched": len(papers),
            "selected": len(selected),
            "pmids": [p["pmid"] for p in selected],
        }
    )
    print(f"sent digest for {len(selected)} screened papers out of {len(papers)} fetched")


def oauth_setup():
    client_id = env("GMAIL_CLIENT_ID", required=True)
    client_secret = env("GMAIL_CLIENT_SECRET", required=True)
    scope = "https://www.googleapis.com/auth/gmail.send"
    redirect_uri = env("GMAIL_REDIRECT_URI", "http://localhost:8765")
    print(f"Using redirect URI: {redirect_uri}")
    params = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{params}"
    parsed_redirect = redirect_uri.rstrip("/")
    if not parsed_redirect.startswith("http://localhost:"):
        raise SystemExit("GMAIL_REDIRECT_URI must use a localhost HTTP callback for the built-in OAuth helper.")
    callback_port = int(parsed_redirect.rsplit(":", 1)[1])

    captured = {"code": None, "error": None}

    class OAuthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import urlparse, parse_qs

            parsed = urlparse(self.path)
            if parsed.path not in ("", "/"):
                self.send_response(404)
                self.end_headers()
                return
            qs = parse_qs(parsed.query)
            if "error" in qs:
                captured["error"] = qs["error"][0]
            elif "code" in qs:
                captured["code"] = qs["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h3>Authorization received. You can close this tab.</h3></body></html>")

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", callback_port), OAuthHandler)

    def serve_once():
        server.handle_request()

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    print(auth_url)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
    thread.join(timeout=300)
    if captured["error"]:
        raise SystemExit(f"OAuth authorization failed: {captured['error']}")
    code = captured["code"]
    if not code:
        raise SystemExit("Did not receive an authorization code on the local callback server.")
    data = urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    payload, raw = http_json(
        "https://oauth2.googleapis.com/token",
        method="POST",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if not payload:
        raise RuntimeError(f"OAuth exchange failed: {raw[:300]}")
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("No refresh_token returned. Re-run with prompt=consent and a fresh account approval.")
    print(refresh_token)


if __name__ == "__main__":
    try:
        args = parse_args()
        if args.gmail_oauth_setup:
            oauth_setup()
        elif args.daemon:
            run_daemon(force_send=args.all)
        else:
            main(force_send=args.all)
    except Exception as exc:
        try:
            append_log(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "status": "error",
                    "error": str(exc),
                }
            )
        except Exception:
            pass
        print(f"error: {exc}", file=sys.stderr)
        raise
