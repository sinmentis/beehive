from __future__ import annotations

CATALOGS = {
    "en": {
        "background.digest_header": "{product} Daily Digest",
        "background.digest_empty_state": "No new items today \u2014 already checked.",
        "background.source_fetch_warning": "{source_type} source fetch failed: {error}",
        "background.digest_event_new": "New",
        "background.digest_event_price_drop": "Price drop",
        "background.digest_event_price_detail": "{old} \u2192 {new}",
        "background.digest_event_back_in_stock": "Back in stock",
        "background.digest_event_tracked_new": "New tracked item",
        "background.digest_event_closing": "Closes: {time}",
        "background.llm_failure_subject": "{product}: AI ranking failed for {channel}",
        "background.llm_failure_body": (
            'Channel "{channel}" AI ranking/summary call failed this cycle and was '
            "skipped; other Channels are unaffected.\n\nError: {error}"
        ),
        "background.tracker_reminder.subject": {
            "one": "{product} Tracker reminder · {count} watched item needs attention",
            "other": "{product} Tracker reminder · {count} watched items need attention",
        },
        "background.tracker_reminder.intro": {
            "one": "One watched item has reached its follow-up window.",
            "other": "{count} watched items have reached their follow-up window.",
        },
        "background.tracker_reminder.context": "Listing: {context}",
        "background.tracker_reminder.deadline": "Deadline: {time}",
        "background.tracker_reminder.view_item": "View item: {url}",
        "background.tracker_reminder.view_item_link": "View item",
    },
    "zh-CN": {
        "background.digest_header": "{product}每日摘要",
        "background.digest_empty_state": "今天没有新内容，已确认检查过。",
        "background.source_fetch_warning": "{source_type} 信源抓取失败：{error}",
        "background.digest_event_new": "新增",
        "background.digest_event_price_drop": "降价",
        "background.digest_event_price_detail": "{old} \u2192 {new}",
        "background.digest_event_back_in_stock": "重新有货",
        "background.digest_event_tracked_new": "新增追踪项",
        "background.digest_event_closing": "截止：{time}",
        "background.llm_failure_subject": "{product}：{channel} AI 排序失败",
        "background.llm_failure_body": (
            "Channel「{channel}」这一轮 AI 排序/摘要调用失败，本轮跳过，"
            "其余 Channel 正常。\n\n错误信息：{error}"
        ),
        "background.tracker_reminder.subject": {
            "one": "{product} 追踪提醒 · {count} 个关注项目需要处理",
            "other": "{product} 追踪提醒 · {count} 个关注项目需要处理",
        },
        "background.tracker_reminder.intro": {
            "one": "一个关注项目已进入提醒时间。",
            "other": "{count} 个关注项目已进入提醒时间。",
        },
        "background.tracker_reminder.context": "项目：{context}",
        "background.tracker_reminder.deadline": "截止时间：{time}",
        "background.tracker_reminder.view_item": "查看项目：{url}",
        "background.tracker_reminder.view_item_link": "查看项目",
    },
    "ja": {
        "background.digest_header": "{product}デイリーダイジェスト",
        "background.digest_empty_state": "本日新着はありません。確認済みです。",
        "background.source_fetch_warning": "{source_type} ソースの取得に失敗しました：{error}",
        "background.digest_event_new": "新着",
        "background.digest_event_price_drop": "値下げ",
        "background.digest_event_price_detail": "{old} \u2192 {new}",
        "background.digest_event_back_in_stock": "再入荷",
        "background.digest_event_tracked_new": "新規トラッキング項目",
        "background.digest_event_closing": "終了：{time}",
        "background.llm_failure_subject": "{product}：{channel} のAIランキングに失敗しました",
        "background.llm_failure_body": (
            "チャンネル「{channel}」の今回のAIランキング/要約呼び出しが失敗したため、"
            "このサイクルはスキップされました。他のチャンネルには影響ありません。"
            "\n\nエラー：{error}"
        ),
        "background.tracker_reminder.subject": {
            "one": "{product} トラッカー通知 · ウォッチ中の{count}件を確認してください",
            "other": "{product} トラッカー通知 · ウォッチ中の{count}件を確認してください",
        },
        "background.tracker_reminder.intro": {
            "one": "ウォッチ中の1件が通知タイミングに達しました。",
            "other": "ウォッチ中の{count}件が通知タイミングに達しました。",
        },
        "background.tracker_reminder.context": "項目：{context}",
        "background.tracker_reminder.deadline": "期限：{time}",
        "background.tracker_reminder.view_item": "項目を見る：{url}",
        "background.tracker_reminder.view_item_link": "項目を見る",
    },
    "ko": {
        "background.digest_header": "{product} 일일 다이제스트",
        "background.digest_empty_state": "오늘은 새로운 항목이 없습니다. 이미 확인했습니다.",
        "background.source_fetch_warning": "{source_type} 소스 수집 실패: {error}",
        "background.digest_event_new": "신규",
        "background.digest_event_price_drop": "가격 인하",
        "background.digest_event_price_detail": "{old} \u2192 {new}",
        "background.digest_event_back_in_stock": "재입고",
        "background.digest_event_tracked_new": "새 추적 항목",
        "background.digest_event_closing": "마감: {time}",
        "background.llm_failure_subject": "{product}: {channel} AI 순위 지정 실패",
        "background.llm_failure_body": (
            "채널 「{channel}」의 이번 AI 순위 지정/요약 호출이 실패하여 이번 주기는 "
            "건너뜁니다. 다른 채널에는 영향이 없습니다.\n\n오류: {error}"
        ),
        "background.tracker_reminder.subject": {
            "one": "{product} 추적 알림 · 관심 항목 {count}개를 확인하세요",
            "other": "{product} 추적 알림 · 관심 항목 {count}개를 확인하세요",
        },
        "background.tracker_reminder.intro": {
            "one": "관심 항목 1개가 알림 시점에 도달했습니다.",
            "other": "관심 항목 {count}개가 알림 시점에 도달했습니다.",
        },
        "background.tracker_reminder.context": "항목: {context}",
        "background.tracker_reminder.deadline": "기한: {time}",
        "background.tracker_reminder.view_item": "항목 보기: {url}",
        "background.tracker_reminder.view_item_link": "항목 보기",
    },
    "es": {
        "background.digest_header": "Resumen diario de {product}",
        "background.digest_empty_state": "Hoy no hay contenido nuevo, ya se ha comprobado.",
        "background.source_fetch_warning": "Error al obtener la fuente {source_type}: {error}",
        "background.digest_event_new": "Nuevo",
        "background.digest_event_price_drop": "Bajada de precio",
        "background.digest_event_price_detail": "{old} \u2192 {new}",
        "background.digest_event_back_in_stock": "De nuevo en stock",
        "background.digest_event_tracked_new": "Nuevo elemento seguido",
        "background.digest_event_closing": "Cierra: {time}",
        "background.llm_failure_subject": (
            "{product}: fallo en la clasificaci\u00f3n de IA de {channel}"
        ),
        "background.llm_failure_body": (
            'El canal "{channel}" fall\u00f3 en la llamada de clasificaci\u00f3n/resumen '
            "de IA en este ciclo y se omiti\u00f3; los dem\u00e1s canales no se ven "
            "afectados.\n\nError: {error}"
        ),
        "background.tracker_reminder.subject": {
            "one": "{product} · recordatorio de seguimiento: {count} elemento requiere atención",
            "other": "{product} · recordatorio de seguimiento: {count} elementos requieren atención",
        },
        "background.tracker_reminder.intro": {
            "one": "Un elemento vigilado ha llegado a su ventana de seguimiento.",
            "other": "{count} elementos vigilados han llegado a su ventana de seguimiento.",
        },
        "background.tracker_reminder.context": "Elemento: {context}",
        "background.tracker_reminder.deadline": "Fecha límite: {time}",
        "background.tracker_reminder.view_item": "Ver elemento: {url}",
        "background.tracker_reminder.view_item_link": "Ver elemento",
    },
    "fr": {
        "background.digest_header": "R\u00e9sum\u00e9 quotidien {product}",
        "background.digest_empty_state": (
            "Rien de nouveau aujourd'hui, d\u00e9j\u00e0 v\u00e9rifi\u00e9."
        ),
        "background.source_fetch_warning": (
            "\u00c9chec de la r\u00e9cup\u00e9ration de la source {source_type} : {error}"
        ),
        "background.digest_event_new": "Nouveau",
        "background.digest_event_price_drop": "Baisse de prix",
        "background.digest_event_price_detail": "{old} \u2192 {new}",
        "background.digest_event_back_in_stock": "De nouveau en stock",
        "background.digest_event_tracked_new": "Nouvel \u00e9l\u00e9ment suivi",
        "background.digest_event_closing": "Cl\u00f4ture : {time}",
        "background.llm_failure_subject": (
            "{product} : \u00e9chec du classement IA pour {channel}"
        ),
        "background.llm_failure_body": (
            "Le canal \u00ab {channel} \u00bb a \u00e9chou\u00e9 lors de cet appel de "
            "classement/r\u00e9sum\u00e9 IA et a \u00e9t\u00e9 ignor\u00e9 ; les autres "
            "canaux ne sont pas affect\u00e9s.\n\nErreur : {error}"
        ),
        "background.tracker_reminder.subject": {
            "one": "{product} · rappel de suivi : {count} élément nécessite votre attention",
            "other": "{product} · rappel de suivi : {count} éléments nécessitent votre attention",
        },
        "background.tracker_reminder.intro": {
            "one": "Un élément suivi a atteint sa fenêtre de rappel.",
            "other": "{count} éléments suivis ont atteint leur fenêtre de rappel.",
        },
        "background.tracker_reminder.context": "Élément : {context}",
        "background.tracker_reminder.deadline": "Échéance : {time}",
        "background.tracker_reminder.view_item": "Voir l’élément : {url}",
        "background.tracker_reminder.view_item_link": "Voir l’élément",
    },
    "de": {
        "background.digest_header": "{product} Tages\u00fcberblick",
        "background.digest_empty_state": "Heute keine neuen Inhalte, bereits gepr\u00fcft.",
        "background.source_fetch_warning": (
            "Abruf der Quelle {source_type} fehlgeschlagen: {error}"
        ),
        "background.digest_event_new": "Neu",
        "background.digest_event_price_drop": "Preissenkung",
        "background.digest_event_price_detail": "{old} \u2192 {new}",
        "background.digest_event_back_in_stock": "Wieder verf\u00fcgbar",
        "background.digest_event_tracked_new": "Neuer verfolgter Eintrag",
        "background.digest_event_closing": "Endet: {time}",
        "background.llm_failure_subject": (
            "{product}: KI-Ranking f\u00fcr {channel} fehlgeschlagen"
        ),
        "background.llm_failure_body": (
            "Kanal \u201e{channel}\u201c: Der KI-Ranking-/Zusammenfassungsaufruf ist in "
            "diesem Zyklus fehlgeschlagen und wurde \u00fcbersprungen; andere Kan\u00e4le "
            "sind nicht betroffen.\n\nFehler: {error}"
        ),
        "background.tracker_reminder.subject": {
            "one": "{product} · Tracker-Erinnerung: {count} beobachteter Eintrag erfordert Aufmerksamkeit",
            "other": "{product} · Tracker-Erinnerung: {count} beobachtete Einträge erfordern Aufmerksamkeit",
        },
        "background.tracker_reminder.intro": {
            "one": "Ein beobachteter Eintrag hat sein Erinnerungsfenster erreicht.",
            "other": "{count} beobachtete Einträge haben ihr Erinnerungsfenster erreicht.",
        },
        "background.tracker_reminder.context": "Eintrag: {context}",
        "background.tracker_reminder.deadline": "Frist: {time}",
        "background.tracker_reminder.view_item": "Eintrag öffnen: {url}",
        "background.tracker_reminder.view_item_link": "Eintrag öffnen",
    },
}
