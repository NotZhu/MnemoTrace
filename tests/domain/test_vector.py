"""验证缩放和单位向量计算的数值行为。"""

from sys import float_info

from mnemotrace.domain.vector import scale_by_max_abs, unit_vector


def test_max_abs_scaling_and_unit_normalization() -> None:
    assert scale_by_max_abs((3.0, 4.0)) == [0.75, 1.0]
    assert unit_vector((3.0, 4.0)) == (0.6, 0.8)


def test_normalization_scales_before_calculating_norm() -> None:
    assert unit_vector((float_info.max, float_info.max)) == unit_vector((1.0, 1.0))
