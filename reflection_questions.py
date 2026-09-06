from copy import deepcopy


QUESTION_BANK = [
    {
        "stage": "good_points",
        "label": "잘한 점",
        "questions": [
            "이번 주에 작게라도 ‘이건 잘했다’고 느끼는 일은 무엇인가요?",
            "그 과정에서 내가 한 선택이나 행동 중 도움이 된 것은 무엇인가요?",
            "다음에도 비슷한 상황이 온다면, 무엇을 다시 해보고 싶나요?",
        ],
    },
    {
        "stage": "improvements",
        "label": "개선할 점",
        "questions": [
            "이번 주에 기대와 다르게 흘러가 아쉬웠던 일은 무엇인가요?",
            "어려워지기 시작한 순간은 언제였고, 당시 어떤 상황이 있었나요?",
            "다시 그 순간으로 돌아간다면, 바꿔볼 부분이나 필요한 도움은 무엇인가요?",
        ],
    },
    {
        "stage": "learnings",
        "label": "배운 점",
        "questions": [
            "이번 주에 나 자신이나 일하는 방식에 대해 새롭게 알게 된 것은 무엇인가요?",
            "어떤 경험이나 장면이 그렇게 생각하게 했나요?",
            "그 발견을 다음에는 어떤 상황에서 활용할 수 있을까요?",
        ],
    },
    {
        "stage": "action_item",
        "label": "다음 행동",
        "questions": [
            "다음 주에 직접 해보고 싶은 작은 변화 하나는 무엇인가요?",
            "언제, 어떤 상황에서 구체적으로 무엇을 해볼 건가요?",
            "실행이 어려워진다면, 어떻게 더 작게 해볼 수 있을까요?",
        ],
    },
]


def select_reflection_questions() -> list[dict]:
    return deepcopy(QUESTION_BANK)
