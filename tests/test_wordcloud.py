"""이번 회차 회고에서 자주 나온 말.

형태소 분석기 없이 조사·어미만 잘라 세므로, 무엇을 세고 무엇을 버리는지를
고정해 둔다. 규칙을 손보다가 한 사람의 사정이 대시보드 맨 앞에 걸리거나
서술어가 화제처럼 보이는 일이 없어야 한다.
"""

import unittest

from dashboard import wordcloud


class CountWordsTest(unittest.TestCase):
    def test_weighs_by_how_many_people_wrote_it(self):
        """한 사람이 열 번 되풀이해도 그 이야기를 한 사람이 는 건 아니다."""
        words = wordcloud.count_words(
            [
                "운동 운동 운동 운동 면접",
                "면접 준비",
                "면접 후기",
            ]
        )
        found = {row["word"]: row for row in words}
        self.assertEqual(found["면접"]["writers"], 3)
        self.assertEqual(found["면접"]["total"], 3)
        # 3명이 쓴 낱말이 4번 나온 1인 낱말보다 앞선다.
        self.assertEqual(words[0]["word"], "면접")
        self.assertNotIn("운동", found)

    def test_drops_words_only_one_person_wrote(self):
        """개인 사정이 이름표 없이 대시보드 맨 앞에 걸리면 안 된다."""
        words = wordcloud.count_words(["코로나 코로나 코로나", "이력서 면접"])
        self.assertEqual(words, [])

    def test_strips_josa_so_the_same_word_counts_once(self):
        words = wordcloud.count_words(["이력서를 고쳤다", "이력서에서 뺐다", "이력서는 길다"])
        self.assertEqual([row["word"] for row in words], ["이력서"])
        self.assertEqual(words[0]["writers"], 3)

    def test_merges_one_letter_nouns_that_carry_a_josa(self):
        """합치지 않으면 `일은`과 `일이`가 서로 다른 낱말로 따로 걸린다."""
        words = wordcloud.count_words(["일은 많다", "일이 밀렸다", "일을 했다"])
        self.assertEqual([row["word"] for row in words], ["일"])
        self.assertEqual(words[0]["writers"], 3)

    def test_keeps_a_word_whose_stem_is_not_a_known_one_letter_noun(self):
        """허용 목록에 없는 한 글자를 남기면 엉뚱한 말이 만들어진다."""
        # `과`는 조사지만 `사`는 낱말이 아니다.
        self.assertEqual(wordcloud.normalize("사과"), "사과")
        # `린`은 조사가 아니라 어간의 일부다.
        self.assertEqual(wordcloud.normalize("밀린"), "밀린")

    def test_drops_conjugated_predicates(self):
        """서술어는 화제가 아니다."""
        words = wordcloud.count_words(["좋았다 하는 되는 있어서", "좋았다 하는 되는 있어서"])
        self.assertEqual(words, [])

    def test_drops_stopwords_and_single_letters(self):
        words = wordcloud.count_words(["그리고 너무 이번 나 를", "그리고 너무 이번 나 를"])
        self.assertEqual(words, [])

    def test_folds_english_case_together(self):
        words = wordcloud.count_words(["Python 공부", "python 공부"])
        found = {row["word"]: row["writers"] for row in words}
        self.assertEqual(found["python"], 2)

    def test_ignores_empty_fields(self):
        self.assertEqual(wordcloud.count_words([None, "", "   "]), [])

    def test_caps_the_number_of_words(self):
        letters = "가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허"
        texts = [
            " ".join(f"{first}{second}" for first in letters for second in letters)
        ] * 2
        self.assertEqual(len(wordcloud.count_words(texts)), wordcloud.MAX_WORDS)


class RenderTest(unittest.TestCase):
    def test_scales_font_size_between_the_bounds(self):
        words = [
            {"word": "면접", "writers": 8, "total": 9},
            {"word": "운동", "writers": 2, "total": 2},
        ]
        html = wordcloud.render(words)
        self.assertIn(f"font-size:{wordcloud.MAX_FONT}px", html)
        self.assertIn(f"font-size:{wordcloud.MIN_FONT}px", html)

    def test_uses_one_size_when_every_word_ties(self):
        html = wordcloud.render([{"word": "면접", "writers": 2, "total": 2}])
        self.assertIn(f"font-size:{wordcloud.MAX_FONT}px", html)

    def test_escapes_words_from_retrospective_text(self):
        html = wordcloud.render(
            [{"word": "<script>x</script>", "writers": 2, "total": 2}]
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_explains_the_empty_state(self):
        self.assertIn("두 명 이상", wordcloud.render([]))


if __name__ == "__main__":
    unittest.main()
