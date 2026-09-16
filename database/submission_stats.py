"""관리자 제출 집계: 팀 미배정 채널 선택 계정의 제출은 별도로 표시한다."""

from config import settings


def separate_submitter_ids() -> set[str]:
    # 팀원으로 배정된 계정은 채널 선택 권한이 있어도 정상 집계한다.
    return set(settings.SUBMISSION_CHANNEL_CHOOSER_IDS) - set(settings.SUBMISSION_DESTINATIONS)


def session_submission_counts(connection) -> list[dict]:
    excluded = sorted(separate_submitter_ids())
    placeholders = ",".join("?" for _ in excluded) or "NULL"
    return [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT session_name,
                   COUNT(DISTINCT CASE WHEN user_id IN ({placeholders})
                         THEN NULL ELSE user_id END) AS submitters,
                   COUNT(DISTINCT CASE WHEN user_id IN ({placeholders})
                         THEN user_id END) AS separate_submitters,
                   MAX(created_at) AS last_at
              FROM retrospectives
             WHERE is_test_submission = 0
             GROUP BY session_name
             ORDER BY last_at DESC, session_name DESC
            """,
            (*excluded, *excluded),
        )
    ]
