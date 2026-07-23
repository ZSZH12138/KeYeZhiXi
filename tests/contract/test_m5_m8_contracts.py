"""M5/M8 契约序列化往返测试。"""


class TestContractRoundtrip:
    def _roundtrip(self, obj):
        """obj -> dict -> obj，验证字段一致。"""
        cls = type(obj)
        data = obj.model_dump(mode="json")
        restored = cls.model_validate(data)
        assert restored == obj
        return restored

    def test_state_update_result_roundtrip(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        result = m5_service.update_state(
            scoring_result_bundle, knowledge_bundle, None, None, state_policy_path,
        )
        self._roundtrip(result)

    def test_learner_state_snapshot_roundtrip(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        result = m5_service.update_state(
            scoring_result_bundle, knowledge_bundle, None, None, state_policy_path,
        )
        self._roundtrip(result.learner_state_snapshot)

    def test_class_state_snapshot_roundtrip(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        result = m5_service.update_state(
            scoring_result_bundle, knowledge_bundle, None, None, state_policy_path,
        )
        self._roundtrip(result.class_state_snapshot)

    def test_diagnosis_result_roundtrip(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        result = m5_service.update_state(
            scoring_result_bundle, knowledge_bundle, None, None, state_policy_path,
        )
        self._roundtrip(result.diagnosis_result)

    def test_assessment_paper_roundtrip(self, m8_service, task_plan, knowledge_bundle):
        paper = m8_service.generate_paper(task_plan, knowledge_bundle, None, None)
        self._roundtrip(paper)

    def test_scoring_result_bundle_roundtrip(self, scoring_result_bundle):
        self._roundtrip(scoring_result_bundle)

    def test_scoring_preparation_roundtrip(self, scoring_preparation):
        self._roundtrip(scoring_preparation)
