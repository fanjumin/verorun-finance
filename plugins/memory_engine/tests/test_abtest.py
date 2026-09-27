"""abtest 纯函数单测（无 DB）。"""
from plugins.memory_engine.services.abtest import assign_arm, ARM_CONTROL, ARM_TREATMENT


def test_arm_assignment_is_deterministic():
    assert assign_arm('user-001', 50) == assign_arm('user-001', 50)


def test_arm_split_is_balanced():
    """1000 个用户 50/50 分流，两臂占比应接近 50%（±8% 容差）。"""
    arms = [assign_arm('u%d' % i, 50) for i in range(1000)]
    ctrl = arms.count(ARM_CONTROL)
    assert 420 <= ctrl <= 580


def test_control_pct_zero_means_all_treatment():
    assert all(assign_arm('u%d' % i, 0) == ARM_TREATMENT for i in range(50))


def test_empty_user_falls_to_treatment():
    assert assign_arm('', 50) == ARM_TREATMENT
