from __future__ import annotations

CATALOGS = {
    "en": {
        "background.digest_title": "{product} Daily Digest \u00b7 {date}",
        "background.digest_header": "{product} Daily Digest",
        "background.digest_empty_state": "No new items today \u2014 already checked.",
        "background.source_fetch_warning": "{source_type} source fetch failed: {error}",
        "background.llm_failure_subject": "{product}: AI ranking failed for {channel}",
        "background.llm_failure_body": (
            'Channel "{channel}" AI ranking/summary call failed this cycle and was '
            "skipped; other Channels are unaffected.\n\nError: {error}"
        ),
    },
    "zh-CN": {
        "background.digest_title": "{product}每日摘要 · {date}",
        "background.digest_header": "{product}每日摘要",
        "background.digest_empty_state": "今天没有新内容，已确认检查过。",
        "background.source_fetch_warning": "{source_type} 信源抓取失败：{error}",
        "background.llm_failure_subject": "{product}：{channel} AI 排序失败",
        "background.llm_failure_body": (
            "Channel「{channel}」这一轮 AI 排序/摘要调用失败，本轮跳过，"
            "其余 Channel 正常。\n\n错误信息：{error}"
        ),
    },
    "ja": {
        "background.digest_title": "{product}デイリーダイジェスト · {date}",
        "background.digest_header": "{product}デイリーダイジェスト",
        "background.digest_empty_state": "本日新着はありません。確認済みです。",
        "background.source_fetch_warning": "{source_type} ソースの取得に失敗しました：{error}",
        "background.llm_failure_subject": "{product}：{channel} のAIランキングに失敗しました",
        "background.llm_failure_body": (
            "チャンネル「{channel}」の今回のAIランキング/要約呼び出しが失敗したため、"
            "このサイクルはスキップされました。他のチャンネルには影響ありません。"
            "\n\nエラー：{error}"
        ),
    },
    "ko": {
        "background.digest_title": "{product} 일일 다이제스트 · {date}",
        "background.digest_header": "{product} 일일 다이제스트",
        "background.digest_empty_state": "오늘은 새로운 항목이 없습니다. 이미 확인했습니다.",
        "background.source_fetch_warning": "{source_type} 소스 수집 실패: {error}",
        "background.llm_failure_subject": "{product}: {channel} AI 순위 지정 실패",
        "background.llm_failure_body": (
            "채널 「{channel}」의 이번 AI 순위 지정/요약 호출이 실패하여 이번 주기는 "
            "건너뜁니다. 다른 채널에는 영향이 없습니다.\n\n오류: {error}"
        ),
    },
    "es": {
        "background.digest_title": "Resumen diario de {product} \u00b7 {date}",
        "background.digest_header": "Resumen diario de {product}",
        "background.digest_empty_state": "Hoy no hay contenido nuevo, ya se ha comprobado.",
        "background.source_fetch_warning": "Error al obtener la fuente {source_type}: {error}",
        "background.llm_failure_subject": (
            "{product}: fallo en la clasificaci\u00f3n de IA de {channel}"
        ),
        "background.llm_failure_body": (
            'El canal "{channel}" fall\u00f3 en la llamada de clasificaci\u00f3n/resumen '
            "de IA en este ciclo y se omiti\u00f3; los dem\u00e1s canales no se ven "
            "afectados.\n\nError: {error}"
        ),
    },
    "fr": {
        "background.digest_title": "R\u00e9sum\u00e9 quotidien {product} \u00b7 {date}",
        "background.digest_header": "R\u00e9sum\u00e9 quotidien {product}",
        "background.digest_empty_state": (
            "Rien de nouveau aujourd'hui, d\u00e9j\u00e0 v\u00e9rifi\u00e9."
        ),
        "background.source_fetch_warning": (
            "\u00c9chec de la r\u00e9cup\u00e9ration de la source {source_type} : {error}"
        ),
        "background.llm_failure_subject": (
            "{product} : \u00e9chec du classement IA pour {channel}"
        ),
        "background.llm_failure_body": (
            "Le canal \u00ab {channel} \u00bb a \u00e9chou\u00e9 lors de cet appel de "
            "classement/r\u00e9sum\u00e9 IA et a \u00e9t\u00e9 ignor\u00e9 ; les autres "
            "canaux ne sont pas affect\u00e9s.\n\nErreur : {error}"
        ),
    },
    "de": {
        "background.digest_title": "{product} Tages\u00fcberblick \u00b7 {date}",
        "background.digest_header": "{product} Tages\u00fcberblick",
        "background.digest_empty_state": "Heute keine neuen Inhalte, bereits gepr\u00fcft.",
        "background.source_fetch_warning": (
            "Abruf der Quelle {source_type} fehlgeschlagen: {error}"
        ),
        "background.llm_failure_subject": (
            "{product}: KI-Ranking f\u00fcr {channel} fehlgeschlagen"
        ),
        "background.llm_failure_body": (
            "Kanal \u201e{channel}\u201c: Der KI-Ranking-/Zusammenfassungsaufruf ist in "
            "diesem Zyklus fehlgeschlagen und wurde \u00fcbersprungen; andere Kan\u00e4le "
            "sind nicht betroffen.\n\nFehler: {error}"
        ),
    },
}
