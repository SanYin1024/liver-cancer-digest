#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
set -a; . "$DIR/.env"; set +a
LOCAL_TZ="$(printenv SCHEDULE_TZ || true)"
[[ -n "$LOCAL_TZ" ]] || LOCAL_TZ="Asia/Shanghai"
EMAIL1="$(printenv EMAIL1 || true)"
EMAIL2="$(printenv EMAIL2 || true)"
[[ -n "$EMAIL1" ]] || EMAIL1="$(printenv GMAIL_TO || true)"
for v in curl jq xmllint base64; do command -v "$v" >/dev/null || { echo "Missing $v" >&2; exit 1; }; done
for v in AI_API_KEY AI_BASE_URL AI_MODEL AI_TIMEOUT GMAIL_CLIENT_ID GMAIL_CLIENT_SECRET GMAIL_REFRESH_TOKEN GMAIL_FROM PUBMED_RETMAX; do [[ -n "$(printenv "$v")" ]] || { echo "Missing $v" >&2; exit 1; }; done
[[ -n "$EMAIL1" ]] || { echo "Missing EMAIL1" >&2; exit 1; }
[[ -n "$EMAIL2" ]] || { echo "Missing EMAIL2" >&2; exit 1; }
RECIPIENTS="$EMAIL1, $EMAIL2"
[[ -n "${LOG_FILE:-}" ]] || LOG_FILE="$DIR/log.txt"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail() { echo "error: $1" >&2; exit 1; }
PROXY_OPT=""
if command -v scutil >/dev/null && [[ "$(scutil --proxy | awk '/HTTPSEnable/{print $3}')" == 1 ]]; then
  PROXY_HOST="$(scutil --proxy | awk '/HTTPSProxy/{print $3}')"
  PROXY_PORT="$(scutil --proxy | awk '/HTTPSPort/{print $3}')"
  [[ -n "$PROXY_HOST" && -n "$PROXY_PORT" ]] && PROXY_OPT="--proxy http://$PROXY_HOST:$PROXY_PORT"
fi
CURL_RETRY_ALL=""
curl --help all 2>/dev/null | grep -q -- '--retry-all-errors' && CURL_RETRY_ALL="--retry-all-errors"
get() { curl $PROXY_OPT --http1.1 --fail --silent --show-error --retry 3 $CURL_RETRY_ALL --retry-delay 2 --max-time 90 "$@"; }
format_digest_html() {
  printf '%s' "$1" |
    perl -0pe 's/^\s*#{1,6}\s*//mg; s/\*\*(.+?)\*\*/$1/g; s/\*(.+?)\*/$1/g; s/^\s*[-*]\s+//mg; s/&/&amp;/g; s/</&lt;/g; s/>/&gt;/g; s/\n{2,}/<\/p><p>/g; s/\n/<br>/g' |
    sed '1s/^/<p>/; $s/$/<\/p>/'
}
QUERY='("Liver Neoplasms"[MeSH Terms] OR hepatocellular carcinoma[Title/Abstract] OR HCC[Title/Abstract] OR cholangiocarcinoma[Title/Abstract] OR "liver cancer"[Title/Abstract] OR "hepatic cancer"[Title/Abstract])'
SCHEDULE_TIME="${SCHEDULE_TIME:-08:00}"
TODAY_LABEL="$(TZ="$LOCAL_TZ" date '+%Y-%m-%d')"
if TZ="$LOCAL_TZ" date -j -f '%Y-%m-%d %H:%M' "$TODAY_LABEL $SCHEDULE_TIME" '+%s' >/dev/null 2>&1; then
  BOUNDARY_EPOCH="$(TZ="$LOCAL_TZ" date -j -f '%Y-%m-%d %H:%M' "$TODAY_LABEL $SCHEDULE_TIME" '+%s')"
  NOW_EPOCH="$(TZ="$LOCAL_TZ" date '+%s')"
  if (( NOW_EPOCH < BOUNDARY_EPOCH )); then BOUNDARY_EPOCH=$((BOUNDARY_EPOCH - 86400)); fi
  START_EPOCH=$((BOUNDARY_EPOCH - 86400))
  START="$(TZ="$LOCAL_TZ" date -r "$START_EPOCH" '+%Y/%m/%d')"
  END="$(TZ="$LOCAL_TZ" date -r "$BOUNDARY_EPOCH" '+%Y/%m/%d')"
  START_LABEL="$(TZ="$LOCAL_TZ" date -r "$START_EPOCH" '+%Y-%m-%d 08:00')"
  END_LABEL="$(TZ="$LOCAL_TZ" date -r "$BOUNDARY_EPOCH" '+%Y-%m-%d 08:00')"
else
  BOUNDARY_EPOCH="$(TZ="$LOCAL_TZ" date -d "$TODAY_LABEL $SCHEDULE_TIME" '+%s')"
  NOW_EPOCH="$(TZ="$LOCAL_TZ" date '+%s')"
  if (( NOW_EPOCH < BOUNDARY_EPOCH )); then BOUNDARY_EPOCH=$((BOUNDARY_EPOCH - 86400)); fi
  START_EPOCH=$((BOUNDARY_EPOCH - 86400))
  START="$(TZ="$LOCAL_TZ" date -d "@$START_EPOCH" '+%Y/%m/%d')"
  END="$(TZ="$LOCAL_TZ" date -d "@$BOUNDARY_EPOCH" '+%Y/%m/%d')"
  START_LABEL="$(TZ="$LOCAL_TZ" date -d "@$START_EPOCH" '+%Y-%m-%d 08:00')"
  END_LABEL="$(TZ="$LOCAL_TZ" date -d "@$BOUNDARY_EPOCH" '+%Y-%m-%d 08:00')"
fi
LABEL="${END_LABEL%% *}"
TERM="($QUERY) AND ($START:$END[Date - Publication])"; URLTERM="$(printf '%s' "$TERM" | jq -sRr @uri)"
get "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=$URLTERM&retmax=$PUBMED_RETMAX&retmode=json&sort=pub_date" > "$TMP/search.json" || fail "PubMed search failed"
IDS="$(jq -r '.esearchresult.idlist[]?' "$TMP/search.json")"; COUNT="$(printf '%s\n' "$IDS" | sed '/^$/d' | wc -l | tr -d ' ')"; SUBJECT="肝癌简报|$LABEL"
if [[ "$COUNT" == 0 ]]; then BODY='今天没有检索到新的肝癌相关 PubMed 记录。'; SELECTED=0
else
  IDLIST="$(printf '%s\n' "$IDS" | paste -sd, -)"
  get -X POST https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi -d "db=pubmed&id=$IDLIST&retmode=json" > "$TMP/summary.json"
  jq '[.result.uids[] as $id|.result[$id]|{pmid:.uid,title:(.title//""),journal:(.fulljournalname//.source//""),pubdate:(.pubdate//""),doi:((.articleids//[])|map(select(.idtype=="doi")|.value)|first//""),url:("https://pubmed.ncbi.nlm.nih.gov/"+.uid)}]' "$TMP/summary.json" > "$TMP/papers.json"
  get -X POST https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi -d "db=pubmed&id=$IDLIST&retmode=xml" > "$TMP/abstracts.xml"
  jq -c '.[]' "$TMP/papers.json" | while read -r p; do
    id="$(jq -r .pmid <<< "$p")"; a="$(xmllint --xpath "string((//PubmedArticle[MedlineCitation/PMID='$id']//Abstract/AbstractText))" "$TMP/abstracts.xml" 2>/dev/null || true)"
    jq --arg a "$a" '.+{abstract:$a,text:((.title+" "+$a)|ascii_downcase)}' <<< "$p"
  done | jq -s 'map(. + {score: ((if (.text | test("hepatocellular carcinoma|hcc|cholangiocarcinoma|liver cancer|hepatic cancer")) then 6 else 0 end) + (if (.text | test("immunotherapy|immune|tace|tki|targeted|metastasis|fibrosis|tumor|biomarker|prognosis|transcript|epigen|metabolic")) then 1 else 0 end))}) | sort_by([.score,.pubdate]) | reverse | map(select(.score >= 5))[:20]' > "$TMP/selected.json"
  [[ "$(jq length "$TMP/selected.json")" != 0 ]] || jq '.[0:10]' "$TMP/papers.json" > "$TMP/selected.json"
  SELECTED="$(jq length "$TMP/selected.json")"
  jq -n --arg model "$AI_MODEL" --arg term "$TERM" --slurpfile p "$TMP/selected.json" '{model:$model,temperature:0.2,messages:[{role:"system",content:"你只根据给定 PubMed 元数据写中文肝癌文献简报，不编造信息。"},{role:"user",content:("检索式："+$term+"。按直接和间接相关分组，每篇写标题、期刊、日期、PMID、DOI、链接和值得看原因；最后给3条热点。文献："+($p[0]|tostring))}]}' > "$TMP/request.json"
  AI_ENDPOINT="$AI_BASE_URL"
  [[ "$AI_ENDPOINT" == */chat/completions ]] || AI_ENDPOINT="${AI_ENDPOINT%/}/chat/completions"
  curl $PROXY_OPT --http1.1 --fail --silent --show-error --retry 3 $CURL_RETRY_ALL --retry-delay 2 --max-time "$AI_TIMEOUT" "$AI_ENDPOINT" -H "Authorization: Bearer $AI_API_KEY" -H 'Content-Type: application/json' --data-binary "@$TMP/request.json" > "$TMP/ai.json" || fail "AI request failed"
  BODY="$(jq -r '.choices[0].message.content//empty' "$TMP/ai.json")"; [[ -n "$BODY" ]] || exit 1
fi
if (( $# )) && [[ "$1" == --dry-run ]]; then echo "dry run: $SELECTED/$COUNT"; exit 0; fi
BODY_HTML="$(format_digest_html "$BODY")"
SEARCH_URL="https://pubmed.ncbi.nlm.nih.gov/?term=$URLTERM&sort=date"
HTML="$(cat <<EOF
<div style="margin:0;background:#f3f4f6;padding:24px 0;">
  <div style="max-width:860px;margin:0 auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
    <div style="padding:24px 28px;border-bottom:1px solid #e5e7eb;background:#fafafa;">
      <div style="font-family:Arial,sans-serif;font-size:12px;letter-spacing:0.04em;color:#6b7280;text-transform:uppercase;">Daily Research Brief</div>
      <div style="font-family:Arial,sans-serif;font-size:24px;line-height:1.2;font-weight:700;color:#111827;margin-top:6px;">$SUBJECT</div>
      <div style="font-family:Arial,sans-serif;font-size:13px;color:#6b7280;margin-top:8px;">$START_LABEL 至 $END_LABEL · $COUNT papers fetched · $SELECTED screened in</div>
    </div>
    <div style="padding:28px;font-family:Arial,sans-serif;color:#111827;line-height:1.72;font-size:14px;">
      <div style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:10px;padding:18px 20px;white-space:normal;">$BODY_HTML</div>
      <div style="margin-top:20px;padding-top:16px;border-top:1px solid #e5e7eb;font-size:12px;color:#6b7280;">PubMed search: <a href="$SEARCH_URL" style="color:#2563eb;text-decoration:none;">$SEARCH_URL</a></div>
    </div>
  </div>
</div>
EOF
)"
get -X POST https://oauth2.googleapis.com/token -d "client_id=$GMAIL_CLIENT_ID&client_secret=$GMAIL_CLIENT_SECRET&refresh_token=$GMAIL_REFRESH_TOKEN&grant_type=refresh_token" > "$TMP/token.json"
TOKEN="$(jq -r '.access_token//empty' "$TMP/token.json")"; [[ -n "$TOKEN" ]] || exit 1
S64="$(printf '%s' "$SUBJECT" | base64 | tr -d '\n')"
FROM_NAME_B64="$(printf '%s' '今日文献推送' | base64 | tr -d '\n')"
printf 'From: =?UTF-8?B?%s?= <%s>\r\nTo: %s\r\nSubject: =?UTF-8?B?%s?=\r\nMIME-Version: 1.0\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n%s\r\n' "$FROM_NAME_B64" "$GMAIL_FROM" "$RECIPIENTS" "$S64" "$HTML" > "$TMP/mail.eml"
RAW="$(base64 < "$TMP/mail.eml" | tr -d '\n' | tr '/+' '_-' | tr -d '=')"
curl $PROXY_OPT --http1.1 --fail --silent --show-error --retry 3 $CURL_RETRY_ALL --retry-delay 2 --max-time 90 -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --arg raw "$RAW" '{raw:$raw}')" https://gmail.googleapis.com/gmail/v1/users/me/messages/send > "$TMP/sent.json" || fail "Gmail send failed"
[[ -n "$(jq -r '.id//empty' "$TMP/sent.json")" ]] || exit 1
printf '%s | 收件人: %s | 条数: %s/%s | 成功 | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$RECIPIENTS" "$SELECTED" "$COUNT" "$SUBJECT" >> "$LOG_FILE"
echo "sent digest for $SELECTED screened papers out of $COUNT fetched"
