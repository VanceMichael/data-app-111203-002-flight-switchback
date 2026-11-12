"""保留航次内区块结构的随机化检验。

设计是航次内严格交替的区块分配，合法再随机化的空间是：每个航次保持区块
结构与切换点不变，整体取计划模式序列或其取补序列（两种起点模式），共 2^K
种航次级组合。分钟从不被打散；同一航次内所有区块的符号联动，保证洗脱掩码
和航次内风况相关结构在所有再随机化中保持不变。

航次级差值 d_i 在一次取补 s_i ∈ {-1, +1} 下整体变号，检验统计量
T(s) = mean_i s_i d_i 的零分布只需对固定的 d 向量做符号翻转即可复现：
- K <= 设计阈值（设计文件可配，默认 20）时枚举全部 2^K 个符号组合，
  检验精确，不做 plus-one 校正；
- 否则用随机种子固定的蒙特卡洛抽样，重复次数在结果中披露，p 值采用
  plus-one 校正以避免出现零 p 值。

平移零假设 H0: 效应 = theta 下，调整后航次差值为 d_i - theta，原始分配的
统计量为 mean(d) - theta，而零分布与未平移时同分布（符号翻转对常数取补
不变），因此置信区间为观测效应加减零分布的双侧 alpha 分位数，无需迭代。
"""

import math
import random
from itertools import product


class SignDistribution:
    def __init__(self, matrix, method, replications, seed, flight_count):
        self.matrix = matrix
        self.method = method
        self.replications = replications
        self.seed = seed
        self.flight_count = flight_count
        self.plus_one = method == 'seeded_monte_carlo'


def build_sign_distribution(k, design):
    if k <= design.enumeration_flight_cap:
        matrix = list(product((-1, 1), repeat=k))
        return SignDistribution(matrix, 'exact_enumeration', len(matrix),
                                None, k)
    rng = random.Random(design.seed)
    replications = design.monte_carlo_replications
    matrix = []
    pairs = replications // 2
    for _ in range(pairs):
        # 成对取反，保证蒙特卡洛零分布严格对称，均值为零
        signs = tuple(1 if rng.random() < 0.5 else -1 for _ in range(k))
        matrix.append(signs)
        matrix.append(tuple(-sign for sign in signs))
    if replications % 2:
        matrix.append(tuple(1 if rng.random() < 0.5 else -1 for _ in range(k)))
    return SignDistribution(matrix, 'seeded_monte_carlo', len(matrix),
                            design.seed, k)


def null_statistics(values, distribution):
    """全部再随机化下的检验统计量 mean_i s_i d_i。"""
    return [math.fsum(sign * value for sign, value in zip(signs, values))
            / len(values) for signs in distribution.matrix]


def p_value_for_observed(observed, null_values, distribution):
    """observed 为原始分配下的统计量；null_values 为预计算的零分布统计量。"""
    hits = sum(1 for value in null_values
               if abs(value) >= abs(observed) - 1e-12)
    if distribution.plus_one:
        return (hits + 1) / (distribution.replications + 1)
    return hits / distribution.replications


def critical_quantile(null_values, distribution, alpha):
    """零分布的双侧 alpha 临界值 c：P(|T| >= c) <= alpha 的最小 c。"""
    ordered = sorted(abs(value) for value in null_values)
    if distribution.plus_one:
        # 与 plus-one p 值保持同口径：(hits+1)/(R+1) <= alpha
        rank = math.ceil(alpha * (distribution.replications + 1)) - 1
        allowed_hits = max(0, rank - 1)
        index = distribution.replications - 1 - allowed_hits
    else:
        # 精确枚举：取第 (1-alpha) 分位，使尾部概率 <= alpha
        index = math.ceil((1 - alpha) * distribution.replications) - 1
    index = max(0, min(index, distribution.replications - 1))
    return ordered[index]


def minimum_attainable_p(distribution):
    """该再随机化空间下理论上最小的双侧尾概率。"""
    if distribution.plus_one:
        return 1.0 / (distribution.replications + 1)
    return 1.0 / distribution.replications


def confidence_interval(observed, null_values, distribution, alpha):
    # 区间可界定的条件是零分布存在尾概率 <= alpha 的临界值，与观测是否显著无关
    if minimum_attainable_p(distribution) > alpha + 1e-12:
        return None, None
    critical = critical_quantile(null_values, distribution, alpha)
    return observed - critical, observed + critical


def randomization_test(contrasts, design):
    ordered = list(contrasts.values())
    k = len(ordered)
    distribution = build_sign_distribution(k, design)
    null_values = null_statistics(ordered, distribution)
    observed = math.fsum(ordered) / k
    p_value = p_value_for_observed(observed, null_values, distribution)
    lower, upper = confidence_interval(
        observed, null_values, distribution, design.alpha)
    return {
        'estimand': 'mean_flight_minute_rate_contrast_kwh',
        'estimand_note': '航次内两模式 retained 分钟均值之差（千瓦时/分钟，含停机分钟），再对合格航次等权平均',
        'effect_kwh': observed,
        'p_value': p_value,
        'confidence_level': 1 - design.alpha,
        'ci_lower_kwh': lower,
        'ci_upper_kwh': upper,
        'ci_note': None if lower is not None else '随机化空间下最小可达 p 值仍超过 alpha，区间不可界定',
        'eligible_flights': k,
        'randomization_method': distribution.method,
        'replications': distribution.replications,
        'random_seed': distribution.seed,
        'possible_assignments': 2 ** k,
        'p_value_correction': 'plus_one' if distribution.plus_one else 'none_exact',
    }
