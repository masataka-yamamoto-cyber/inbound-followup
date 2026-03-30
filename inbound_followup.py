#!/usr/bin/env python3
"""
インバウンド架電後メール下書き自動生成ツール

Salesforceの活動履歴（amptalk Zoom Phone）を分析し、
着地分類に応じたメール下書きをSlackに投稿する。

使い方:
  python3 inbound_followup.py                          # 当日の全リードを処理（ドライラン）
  python3 inbound_followup.py --execute                # Slack投稿を実行
  python3 inbound_followup.py --lead-id 00QxxxxMAO     # 特定リードのみ処理
  python3 inbound_followup.py --dry-run                # ドライラン（デフォルト）

前提:
  - sf CLI がインストール・認証済み（target-org: prod）
  - 環境変数: GROQ_API_KEY, SLACK_BOT_TOKEN
"""

import json
import subprocess
import sys
import os
import urllib.request
import urllib.error
from datetime import datetime, date
from pathlib import Path

# === 設定 ===
SF_TARGET_ORG = "prod"
SLACK_CHANNEL_ID = "C0AQF82TFT2"
SPIR_URL = "https://app.spirinc.com/t/uiTqW2_1OZRpnsmbBahd5/as/ltKFNrejb9TJO_70dDTO8/confirm"
SENDER_NAME = "小和瀬"
SENDER_COMPANY = "株式会社SalesNow"
# インバウンド系のLeadSourceコード（SF上の実値）
LEAD_SOURCES = (
    "sn_lis_list", "sn_lis_brand_single", "sn_lis_brand_multi",
    "sn_lis_competitor", "sn_lis_database", "sn_in_seo1",
    "sn_db_user", "db_seo", "corp_form", "sn_meta_dis",
    "sn_mlis_brand_single",
)
LOOKBACK_DAYS = 5
# Gmail下書き作成対象のメールアドレス（この人が架電した場合のみ下書きを作成）
GMAIL_DRAFT_USERS = {
    "miki-owase@salesnow.jp",
    "shinya-ohno@salesnow.jp",
    "tomoya-takeuchi@salesnow.jp",
    "yuki-okayasu@salesnow.jp",
    "hiroki-matsubara@salesnow.jp",
}

# 担当者別メール署名
EMAIL_SIGNATURES = {
    "yuki-okayasu@salesnow.jp": (
        "\n\n====================================\n"
        "株式会社SalesNow\n"
        "岡安 勇樹（おかやす ゆうき）\n"
        "AI搭載 企業データベースクラウド『SalesNow』\n"
        "業界最大級の企業データベースメディア『SalesNow DB』\n"
        "===================================="
    ),
    "tomoya-takeuchi@salesnow.jp": (
        "\n\n====================================\n"
        "株式会社SalesNow\n"
        "竹内 智康 (たけうち ともや）\n"
        "TEL：050-1782-7611\n"
        "Email：tomoya-takeuchi@salesnow.jp\n"
        "AI搭載 企業データベースクラウド『SalesNow』\n"
        "業界最大級の企業データベースメディア『SalesNow DB』\n"
        "===================================="
    ),
    "hiroki-matsubara@salesnow.jp": (
        "\n\n---------------------------------------------------------------------------------------\n"
        "株式会社SalesNow\n"
        "〒150-6207 東京都渋谷区桜丘町1-4　渋谷サクラステージ\n"
        "SHIBUYAサイド SHIBUYAタワー7F\n"
        "---------------------------------------------------------------------------------------\n"
        "アカウントデベロップメント\n"
        "松原 弘樹\n"
        "TEL ：090-4640-1822\n"
        "Email：hiroki-matsubara@salesnow.jp\n"
        "---------------------------------------------------------------------------------------\n"
        "セールスチームの武器となるデータベース『SalesNow』\n"
        "日本最大級の企業データベースメディア『SalesNow DB』\n"
        "---------------------------------------------------------------------------------------"
    ),
    "shinya-ohno@salesnow.jp": (
        "\n\n===========================================\n"
        "株式会社SalesNow\n"
        "〒150-6207 東京都渋谷区桜丘町1-4　渋谷サクラステージ\n"
        "SHIBUYAサイド SHIBUYAタワー7F\n"
        "----------------------------------------------------------------------\n"
        "アカウントディベロップメントG\n"
        "大野 晋也（Shniya Ohno）\n"
        "\n"
        "TEL ：070-6645-8278\n"
        "Email：shinya-ohno@salesnow.jp\n"
        "------------------------------------------------------------------------\n"
        "セールスチームの武器となるデータベース『SalesNow』\n"
        "日本最大級の企業データベースメディア『SalesNow DB』\n"
        "==========================================="
    ),
}

# 処理済みTask IDの記録ファイル（重複処理防止）
PROCESSED_FILE = Path(__file__).parent / ".inbound_followup_processed.json"

# === 着地分類 ===
CATEGORY_VOICEMAIL = "留守電"
CATEGORY_APPOINTMENT = "日程調整完了"
CATEGORY_CONNECTED_NO_APPT = "通電・未アポ"
CATEGORY_NOT_CALLED = "未架電"

# === SalesNow Lite 定型文面 ===
SALESNOW_LITE_BLOCK = (
    "\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "■ SalesNow Lite とは？\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "月額なし・1件50円から、SalesNowのデータを\n"
    "スポットでご利用いただけるプランです。\n"
    "\n"
    "「まずデータの精度を確認したい」\n"
    "「特定のプロジェクトだけリストが必要」\n"
    "\n"
    "そうした場面でお使いいただけます。\n"
    "\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "■ 主な特徴\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "✓ 580万社以上のデータベース（国内法人100%網羅）\n"
    "✓ 業種・規模・地域・売上など細かい条件での絞り込みが可能\n"
    "✓ クレジットカード決済で、5分で利用開始\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "▼ SalesNow Lite の詳細はこちら\n"
    "https://top.salesnow.jp/lite/\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━"
)


# ── Salesforce クエリ ──────────────────────────────────────

def run_sf_query(soql):
    """sf CLI でSOQLクエリを実行しJSONを返す"""
    cmd = [
        "sf", "data", "query",
        "--query", soql,
        "--target-org", SF_TARGET_ORG,
        "--json"
    ]
    env = os.environ.copy()
    env["SF_DISABLE_TELEMETRY"] = "true"
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    if result.returncode != 0:
        print(f"[ERROR] sf query failed: {result.stderr}")
        return []
    # sf CLI のWarning行を除去してJSON解析
    stdout = result.stdout
    lines = stdout.split("\n")
    json_lines = []
    started = False
    for line in lines:
        if line.strip().startswith("{"):
            started = True
        if started:
            json_lines.append(line)
    data = json.loads("\n".join(json_lines))
    return data.get("result", {}).get("records", [])


def fetch_leads(lead_id=None):
    """直近N日以内のインバウンドリードを取得"""
    if lead_id:
        soql = (
            f"SELECT Id, Name, FirstName, LastName, Email, Phone, Company, "
            f"LeadSource, CreatedDate, Status "
            f"FROM Lead WHERE Id = '{lead_id}'"
        )
    else:
        sources = ", ".join(f"'{s}'" for s in LEAD_SOURCES)
        soql = (
            f"SELECT Id, Name, FirstName, LastName, Email, Phone, Company, "
            f"LeadSource, CreatedDate, Status "
            f"FROM Lead "
            f"WHERE CreatedDate = LAST_N_DAYS:{LOOKBACK_DAYS} "
            f"AND LeadSource IN ({sources}) "
            f"ORDER BY CreatedDate DESC"
        )
    return fetch_leads_raw(soql)


def fetch_leads_raw(soql):
    """SOQLでリードを取得"""
    records = run_sf_query(soql)
    leads = []
    for r in records:
        leads.append({
            "id": r.get("Id"),
            "name": r.get("Name", ""),
            "first_name": r.get("FirstName", ""),
            "last_name": r.get("LastName", ""),
            "email": r.get("Email", ""),
            "phone": r.get("Phone", ""),
            "company": r.get("Company", ""),
            "lead_source": r.get("LeadSource", ""),
            "created_date": r.get("CreatedDate", ""),
            "status": r.get("Status", ""),
        })
    return leads


def fetch_amptalk_tasks(lead_ids):
    """リードに紐づくamptalk活動履歴を取得"""
    if not lead_ids:
        return {}
    ids_str = ", ".join(f"'{lid}'" for lid in lead_ids)
    soql = (
        f"SELECT Id, WhoId, Subject, Description, ActivityDate, CreatedDate, "
        f"OwnerId, Owner.Name, Owner.Email "
        f"FROM Task "
        f"WHERE WhoId IN ({ids_str}) "
        f"AND Subject LIKE 'amptalk Zoom Phone:%' "
        f"ORDER BY CreatedDate DESC"
    )
    records = run_sf_query(soql)
    # リードIDごとにグループ化（最新のものを先頭に）
    tasks_by_lead = {}
    for r in records:
        who_id = r.get("WhoId")
        owner = r.get("Owner") or {}
        task = {
            "id": r.get("Id"),
            "subject": r.get("Subject", ""),
            "description": r.get("Description", ""),
            "activity_date": r.get("ActivityDate", ""),
            "created_date": r.get("CreatedDate", ""),
            "owner_name": owner.get("Name", ""),
            "owner_email": owner.get("Email", ""),
        }
        tasks_by_lead.setdefault(who_id, []).append(task)
    return tasks_by_lead


# ── AI分析（claude CLI経由）──────────────────────────────

def analyze_call_log(lead, tasks):
    """通話ログをclaude CLI経由で分析し、着地分類とメール下書きを生成"""

    # 架電担当者名を取得（最新Taskの所有者）
    caller_name = tasks[0].get("owner_name", "") if tasks else ""
    # 姓のみ抽出（"小和瀬 美紀" → "小和瀬"）
    caller_last_name = caller_name.split()[0] if caller_name else SENDER_NAME

    # 通話ログを整形
    call_logs = []
    for t in tasks:
        call_logs.append(
            f"件名: {t['subject']}\n"
            f"日時: {t['activity_date']}\n"
            f"架電担当: {t.get('owner_name', '不明')}\n"
            f"内容:\n{t['description'] or '（記載なし）'}"
        )
    call_logs_text = "\n---\n".join(call_logs)

    prompt = f"""あなたはSalesNowのインサイドセールス担当のアシスタントです。
以下の通話ログを分析し、JSON形式で回答してください。

## リード情報
- 会社名: {lead['company']}
- 担当者名: {lead['name']}
- メール: {lead['email']}
- 電話番号: {lead['phone']}
- 流入経路: {lead['lead_source']}
- 問い合わせ日: {lead['created_date']}

## 通話ログ（amptalk記録）
{call_logs_text}

## 分析タスク

1. **着地分類**: 以下のいずれかに分類してください
   - "留守電": 不在・留守電・応答なし・つながらなかった場合
   - "日程調整完了": 打ち合わせ日時が確定した場合
   - "通電・未アポ": 電話はつながったが日程調整に至らなかった場合

2. **通話ログ要約**: 1-2文で要約

3. **SalesNow Lite提案フラグ**: 通話ログの中でSalesNow Liteの提案・案内・紹介をしていた場合は true、していなければ false

4. **メール下書き**: 着地分類に応じたメール文面を生成
   - 留守電の場合: お電話した旨 + 日程調整リンク（{SPIR_URL}）を案内
   - 日程調整完了の場合: 日時確認 + Google Meet案内のアポ確定メール
   - 通電・未アポの場合: 通話のお礼 + 未アポ理由の分析 + 次アクション提案

5. **未アポ理由**（通電・未アポの場合のみ）: なぜ日程調整に至らなかったか分析

## 回答フォーマット（JSON）
{{
  "category": "留守電 | 日程調整完了 | 通電・未アポ",
  "summary": "通話ログの要約",
  "lite_proposed": true | false,
  "no_appt_reason": "未アポの理由（該当する場合のみ、それ以外はnull）",
  "email_subject": "【先ほどのお電話のお礼】{SENDER_COMPANY} {caller_last_name}",
  "email_body": "メールの本文（{lead['last_name']}様 宛、差出人は{SENDER_COMPANY}の{caller_last_name}）"
}}

重要:
- 「セールスナウ」「Salesnow」等の表記は絶対に使わず、必ず「SalesNow」と表記してください
- メール件名は必ず「【先ほどのお電話のお礼】{SENDER_COMPANY} {caller_last_name}」にしてください
- メール文面は丁寧かつ簡潔に
- 流入経路に応じてデモ案内や資料フォローを含める
- JSONのみ回答してください（マークダウンのコードブロック不要）
- SalesNow Liteの文面はemail_bodyに含めないでください（後工程で自動付与します）
- **メール本文のフォーマットは以下の構造を厳守してください**:
  - 冒頭の宛名（{lead['last_name']}様）の後に必ず空行を入れる
  - 「お世話になっております。」の挨拶ブロックの後に空行を入れる
  - 本題（お電話のお礼・内容の振り返り）の後に空行を入れる
  - 次のアクション提案の後に空行を入れる
  - 改行は必ず \\n を使い、段落ごとに \\n\\n で区切る
  - 一文ごとに \\n で改行を入れ、長い文章を一行にまとめない
  - そのままコピー&ペーストで送信できるビジネスメール形式にする"""

    # Groq API で実行（curl経由）
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        print("[ERROR] GROQ_API_KEY が設定されていません。")
        sys.exit(1)

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-X", "POST",
                "https://api.groq.com/openai/v1/chat/completions",
                "-H", "Content-Type: application/json",
                "-H", f"Authorization: Bearer {api_key}",
                "-d", json.dumps(payload, ensure_ascii=False),
            ],
            capture_output=True, text=True, timeout=180,
        )
        body = json.loads(result.stdout)
        response_text = body["choices"][0]["message"]["content"].strip()
    except subprocess.TimeoutExpired:
        print(f"[ERROR] Groq API タイムアウト: {lead['company']}")
        return None
    except Exception as e:
        print(f"[ERROR] Groq API エラー: {lead['company']} - {e}")
        return None

    if not response_text:
        print(f"[WARN] Groq API 空レスポンス: {lead['company']}")
        return None

    # コードブロックが含まれている場合は除去
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
    if response_text.endswith("```"):
        response_text = response_text[:-3]
    response_text = response_text.strip()

    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        print(f"[WARN] JSON parse failed for lead {lead['id']}: {response_text[:200]}")
        return None


def _error_result(summary, lead=None, caller_last_name=None):
    last_name = (lead or {}).get("last_name", "")
    c_last = caller_last_name or SENDER_NAME
    email_body = ""
    if last_name:
        email_body = (
            f"{last_name}様\n\n"
            f"お世話になっております。\n"
            f"株式会社SalesNowの{c_last}でございます。\n\n"
            f"先ほどはお電話を差し上げましたが、ご不在のようでしたのでメールにてご連絡いたしました。\n\n"
            f"{last_name}様が現在お感じになられている営業活動やターゲティングに関する課題感に沿って、\n"
            f"お力になれる部分があればぜひご案内させていただければと思いご連絡いたしました。\n\n"
            f"弊社SalesNowは、全国580万社以上の企業データベースを活用し、ターゲット企業の選定からアプローチの優先順位付けまでを支援する企業データベースです。\n"
            f"部署直通の電話番号やキーマン情報などもご活用いただけるため、営業活動の効率化に多くの企業様にご活用いただいております。\n\n"
            f"もしよろしければ、オンラインでのお打ち合わせで貴社の状況に合わせたご提案をさせていただければ幸いです。\n"
            f"下記より、ご都合のよろしい日時をお選びいただけますと幸いです。\n"
            f"https://app.spirinc.com/t/uiTqW2_1OZRpnsmbBahd5/as/5jKI5ibsK4niAlxb1iQux/confirm\n\n"
            f"ご不明な点がございましたら、お気軽にご返信くださいませ。\n"
            f"何卒よろしくお願いいたします。"
        )
    return {
        "category": "留守電",
        "summary": summary,
        "no_appt_reason": None,
        "email_subject": f"【先ほどのお電話のお礼】{SENDER_COMPANY} {c_last}" if last_name else "",
        "email_body": email_body,
    }


# ── Gmail下書き作成（サービスアカウント＋ドメイン全体委任）────────

GMAIL_CREDENTIALS_DIR = Path(__file__).parent / ".gmail_credentials"
GMAIL_SERVICE_ACCOUNT_FILE = GMAIL_CREDENTIALS_DIR / "service_account.json"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


def get_gmail_service(user_email):
    """サービスアカウントで指定ユーザーのGmail APIサービスを取得"""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    if not GMAIL_SERVICE_ACCOUNT_FILE.exists():
        print(f"[ERROR] サービスアカウントキーが見つかりません: {GMAIL_SERVICE_ACCOUNT_FILE}")
        print("  -> Gmail認証セットアップ手順書.md を参照してセットアップしてください。")
        return None

    creds = service_account.Credentials.from_service_account_file(
        str(GMAIL_SERVICE_ACCOUNT_FILE),
        scopes=GMAIL_SCOPES,
    )
    # 指定ユーザーに成り代わる（ドメイン全体委任）
    delegated_creds = creds.with_subject(user_email)

    return build("gmail", "v1", credentials=delegated_creds)


def create_gmail_draft(lead, analysis, caller_email=None):
    """架電担当者のGmailに下書きを作成"""
    import base64
    from email.mime.text import MIMEText as EmailMIMEText

    subject = analysis.get("email_subject", "")
    body = analysis.get("email_body", "")

    if not subject or not body:
        print(f"[WARN] メール内容が空のためGmail下書きをスキップ: {lead['company']}")
        return False

    if not caller_email:
        print(f"[WARN] 架電担当者のメールアドレスが不明のためGmail下書きをスキップ: {lead['company']}")
        return False

    service = get_gmail_service(caller_email)
    if not service:
        return False

    # 担当者別署名を追加
    signature = EMAIL_SIGNATURES.get(caller_email, "")
    if signature:
        body += signature

    msg = EmailMIMEText(body, "plain", "utf-8")
    msg["to"] = lead["email"]
    msg["subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    try:
        service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw}}
        ).execute()
        print(f"[OK] Gmail下書き作成完了 ({caller_email}): {lead['company']}({lead['name']}) -> {lead['email']}")
        return True
    except Exception as e:
        print(f"[ERROR] Gmail下書き作成失敗 ({caller_email}): {e}")
        return False


# ── Slack投稿 ─────────────────────────────────────────

def post_to_slack(lead, analysis, caller_email=None, dry_run=True):
    """親メッセージに会社名・名前・着地情報、スレッドにメール文面を投稿"""
    category = analysis.get("category", "不明")
    summary = analysis.get("summary", "")
    reason = analysis.get("no_appt_reason")
    subject = analysis.get("email_subject", "")
    body = analysis.get("email_body", "")

    # 署名を付与（Gmailと同じ内容をSlackにも表示）
    signature = EMAIL_SIGNATURES.get(caller_email or "", "")
    full_body = body + signature if signature else body

    sf_url = f"https://salesnow-jp.lightning.force.com/lightning/r/Lead/{lead['id']}/view"

    category_emoji = {
        CATEGORY_VOICEMAIL: "📵",
        CATEGORY_APPOINTMENT: "✅",
        CATEGORY_CONNECTED_NO_APPT: "⚠️",
    }.get(category, "❓")

    # ── 親メッセージ：会社名　名前のみ ──
    parent_text = f"{lead['company']}　{lead['name']}"
    parent_blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": parent_text[:150]}
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": (
                    f"📧 {lead['email']}　📱 {lead['phone']}　"
                    f"<{sf_url}|Salesforceで開く>"
                )}
            ]
        },
    ]

    # ── スレッド：着地情報 + メール下書き ──
    thread_blocks = []

    # 着地分類・要約
    info_text = (
        f"{category_emoji} *着地：{category}*\n"
        f"📋 {summary}"
    )
    if reason:
        info_text += f"\n\n*💡 未アポ理由分析：*\n{reason}"
    if len(info_text) > 2900:
        info_text = info_text[:2900] + "..."
    thread_blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": info_text}
    })
    thread_blocks.append({"type": "divider"})

    # メール下書き（件名 + 本文 + 署名のフル文面）
    email_text = f"*件名：{subject}*\n━━━━━━━━━━━━━━━━━━━━\n\n{full_body}"
    if len(email_text) > 2900:
        email_text = email_text[:2900] + "..."
    thread_blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": email_text}
    })

    if dry_run:
        print(f"\n{'='*60}")
        print(f"【親メッセージ】{parent_text}")
        print(f"  {category_emoji} 着地：{category}")
        print(f"  📋 {summary}")
        print(f"\n【スレッド】")
        if reason:
            print(f"  💡 未アポ理由：{reason}")
        print(f"  📧 件名：{subject}")
        print(f"\n{body}")
        print(f"\n  🔗 SF: {sf_url}")
        print(f"{'='*60}")
        return True

    # Slack API で投稿
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("[ERROR] SLACK_BOT_TOKEN が設定されていません")
        return False

    client = WebClient(token=token)
    try:
        # 1. 親メッセージ投稿
        parent_response = client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            blocks=parent_blocks,
            text=parent_text,
        )
        thread_ts = parent_response["ts"]

        # 2. スレッドにメール下書きを投稿
        client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            thread_ts=thread_ts,
            blocks=thread_blocks,
            text=f"📧 メール下書き：{subject}",
        )
        print(f"[OK] Slack投稿完了: {lead['company']}（{lead['name']}）")
        return True
    except SlackApiError as e:
        print(f"[ERROR] Slack投稿失敗: {e.response['error']}")
        return False


def handle_not_called(lead, dry_run=True):
    """未架電リードのリマインドをSlack投稿"""
    sf_url = f"https://salesnow-jp.lightning.force.com/lightning/r/Lead/{lead['id']}/view"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"⏰ 未架電リマインド：{lead['company']}（{lead['name']}様）"[:150]}
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*問い合わせ日：* {lead['created_date'][:10]}\n"
                    f"*流入経路：* {lead['lead_source']}\n"
                    f"*電話番号：* {lead['phone']}\n"
                    f"*メール：* {lead['email']}\n\n"
                    f"📌 amptalk活動履歴がありません。架電をお願いします。"
                )
            }
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"<{sf_url}|Salesforceで開く>"}
            ]
        },
    ]

    if dry_run:
        print(f"\n{'='*60}")
        print(f"⏰ 未架電リマインド：{lead['company']}（{lead['name']}様）")
        print(f"   問い合わせ日: {lead['created_date'][:10]}")
        print(f"   流入経路: {lead['lead_source']}")
        print(f"   📌 amptalk活動履歴なし → 架電が必要")
        print(f"{'='*60}")
        return True

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("[ERROR] SLACK_BOT_TOKEN が設定されていません")
        return False

    client = WebClient(token=token)
    try:
        client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            blocks=blocks,
            text=f"⏰ 未架電リマインド：{lead['company']}",
        )
        return True
    except SlackApiError as e:
        print(f"[ERROR] Slack投稿失敗: {e.response['error']}")
        return False


# ── 処理済みTask ID管理（重複防止）────────────────────────

def load_processed_ids():
    """処理済みTask IDセットを読み込む"""
    if PROCESSED_FILE.exists():
        try:
            data = json.loads(PROCESSED_FILE.read_text())
            # 7日以上前のエントリは自動削除
            cutoff = datetime.now().timestamp() - (7 * 86400)
            return {
                tid: ts for tid, ts in data.items()
                if ts > cutoff
            }
        except (json.JSONDecodeError, KeyError):
            return {}
    return {}


def save_processed_ids(processed):
    """処理済みTask IDセットを保存"""
    PROCESSED_FILE.write_text(json.dumps(processed, indent=2))


def mark_processed(processed, task_ids):
    """Task IDを処理済みとして記録"""
    now = datetime.now().timestamp()
    for tid in task_ids:
        processed[tid] = now


# ── メイン ──────────────────────────────────────────────

def main():
    # 引数パース
    lead_id = None
    task_id = None
    dry_run = True

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i].startswith("--lead-id="):
            lead_id = args[i].split("=", 1)[1]
        elif args[i] == "--lead-id" and i + 1 < len(args):
            i += 1
            lead_id = args[i]
        elif args[i].startswith("--task-id="):
            task_id = args[i].split("=", 1)[1]
        elif args[i] == "--task-id" and i + 1 < len(args):
            i += 1
            task_id = args[i]
        elif args[i] == "--execute":
            dry_run = False
        elif args[i] == "--dry-run":
            dry_run = True
        i += 1

    # 自動実行時は平日9-19時のみ（手動指定時はスキップ）
    if not lead_id and not dry_run:
        now = datetime.now()
        if now.weekday() >= 5 or now.hour < 8 or now.hour >= 20:
            return

    mode = "ドライラン" if dry_run else "実行"
    print(f"🚀 インバウンドフォローアップ自動生成 [{mode}]")
    print(f"   日付: {date.today()}")
    if lead_id:
        print(f"   対象: リードID {lead_id}")
    else:
        print(f"   対象: 直近{LOOKBACK_DAYS}日のインバウンドリード")
    print()

    # 処理済みID読み込み
    processed = load_processed_ids()

    # 1. リード取得
    print("📥 Salesforceからリードを取得中...")
    leads = fetch_leads(lead_id)
    if not leads:
        print("ℹ️  対象リードが見つかりませんでした。")
        return

    print(f"   → {len(leads)}件のリードを取得")

    # 2. amptalk活動履歴を取得
    print("📞 amptalk活動履歴を取得中...")
    lead_ids = [l["id"] for l in leads]
    tasks_by_lead = fetch_amptalk_tasks(lead_ids)
    called_count = sum(1 for lid in lead_ids if lid in tasks_by_lead)
    print(f"   → 架電済み: {called_count}件 / 未架電: {len(leads) - called_count}件")
    print()

    # 3. 各リードを処理
    results = {
        CATEGORY_VOICEMAIL: 0,
        CATEGORY_APPOINTMENT: 0,
        CATEGORY_CONNECTED_NO_APPT: 0,
        CATEGORY_NOT_CALLED: 0,
    }
    new_processed_count = 0

    for lead in leads:
        lid = lead["id"]
        tasks = tasks_by_lead.get(lid, [])

        if not tasks:
            # 未架電はリマインドしない（定期実行時はノイズになるため）
            if lead_id:
                # 手動指定時のみリマインド表示
                results[CATEGORY_NOT_CALLED] += 1
                handle_not_called(lead, dry_run)
            continue

        # task_id指定時：そのTask 1件のみに絞る
        if task_id:
            tasks = [t for t in tasks if t["id"] == task_id]
            if not tasks:
                continue
            # amptalk架電記録のみ対象（手動メモ等は除外）
            if not tasks[0].get("subject", "").startswith("amptalk Zoom Phone:"):
                print(f"[SKIP] amptalk架電記録ではありません: {tasks[0].get('subject', '')}")
                continue
            # 処理済みならスキップ（1活動履歴 = 1下書きを厳守）
            if task_id in processed:
                print(f"[SKIP] Task {task_id} は処理済みです: {lead['company']}")
                continue
        else:
            # 本日作成されたTaskのみに絞る（過去の架電は対象外）
            today_str = date.today().isoformat()
            tasks = [t for t in tasks if t.get("created_date", "").startswith(today_str)]
            if not tasks:
                continue
            # 処理済みチェック：最新のTask IDが処理済みならスキップ
            latest_task_id = tasks[0]["id"]
            if latest_task_id in processed:
                continue

        # AI分析
        print(f"🤖 分析中: {lead['company']}（{lead['name']}）...")
        analysis = analyze_call_log(lead, tasks)
        if analysis is None:
            print(f"[SKIP] AI分析失敗のためスキップ: {lead['company']}（{lead['name']}）")
            continue
        category = analysis.get("category", "不明")

        # 日程調整完了の場合はテンプレートで上書き
        if category == CATEGORY_APPOINTMENT:
            caller_name = tasks[0].get("owner_name", "") if tasks else ""
            c_last = caller_name.split()[0] if caller_name else SENDER_NAME
            analysis["email_subject"] = "次回お打ち合わせの詳細"
            analysis["email_body"] = (
                f"{lead['last_name']}様\n\n"
                f"お世話になっております。\n"
                f"株式会社SalesNowの{c_last}でございます。\n\n"
                f"先ほどはお電話にてありがとうございました。\n"
                f"早速ですが、お打ち合わせ当日の詳細を送付いたします。\n\n"
                f"■お打ち合わせ詳細\n"
                f"日時：\n"
                f"URL：\n\n"
                f"※万が一、都合が悪くなってしまった際は、以下URLより代替日のご登録をお願いいたします。\n"
                f"https://app.spirinc.com/t/uiTqW2_1OZRpnsmbBahd5/as/ltKFNrejb9TJO_70dDTO8/confirm\n\n"
                f"\n当日も何卒よろしくお願いいたします。"
            )

        # SalesNow Lite提案時は定型文面を本文末尾に付与
        if analysis.get("lite_proposed"):
            analysis["email_body"] = analysis.get("email_body", "") + SALESNOW_LITE_BLOCK

        if category in results:
            results[category] += 1

        caller_email = tasks[0].get("owner_email", "") if tasks else ""
        success = post_to_slack(lead, analysis, caller_email=caller_email, dry_run=dry_run)

        # Gmail下書き作成（実行モードかつ対象ユーザーの場合のみ）
        if not dry_run:
            if caller_email and caller_email in GMAIL_DRAFT_USERS:
                create_gmail_draft(lead, analysis, caller_email=caller_email)
            elif caller_email:
                print(f"[SKIP] Gmail下書き対象外: {caller_email}（{lead['company']}）")

        # 処理済みとして記録（実行モードの場合のみ）
        if success and not dry_run:
            mark_processed(processed, [t["id"] for t in tasks])
            new_processed_count += 1

    # 処理済みIDを保存
    if not dry_run and new_processed_count > 0:
        save_processed_ids(processed)

    # サマリー
    total = sum(results.values())
    if total > 0:
        print(f"\n{'='*60}")
        print("📊 処理サマリー")
        print(f"{'='*60}")
        for cat, count in results.items():
            if count > 0:
                print(f"   {cat}: {count}件")
        print(f"   合計: {total}件")
    else:
        print("ℹ️  新しい処理対象はありませんでした。")

    if dry_run and total > 0:
        print(f"\n💡 Slack投稿するには --execute オプションを付けて再実行してください")


if __name__ == "__main__":
    main()
