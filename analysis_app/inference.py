"""switchback 分析编排、执行偏离/风况诊断与结论分级。"""

import copy
import math

from .blocks import (BLOCK_MISSING, BLOCK_VALID, build_blocks, count_by_status,
                     exposure_audit, flight_contrasts, pooled_mode_accounts)
from .design import validate_against_records
from .randomization import randomization_test


def _mode_wind_test(contrasts_by_mode, design):
    """风况本身做一次同结构的符号翻转检验：风的航次级差值是否显著非零。"""
    from .randomization import (build_sign_distribution, null_statistics,
                                p_value_for_observed)
    values = list(contrasts_by_mode.values())
    if not values:
        return None
    distribution = build_sign_distribution(len(values), design)
    observed = math.fsum(values) / len(values)
    null_values = null_statistics(values, distribution)
    return {'wind_contrast_mps': observed,
            'wind_randomization_p': p_value_for_observed(
                observed, null_values, distribution)}


def _diagnose(blocks, accounts, contrasts, design, reasons, test):
    base, comparison = design.baseline_mode, design.comparison_mode
    flags = []

    # 1. 有效区块数门槛：按模式分别核对，避免某一模式支撑不足。
    block_counts = {mode: accounts[mode]['valid_blocks'] for mode in design.modes}
    enough_blocks = all(count >= design.min_valid_blocks for count in block_counts.values())
    if not enough_blocks:
        flags.append({'code': 'min_valid_blocks_unmet',
                      'detail': {'required_per_mode': design.min_valid_blocks,
                                 'observed': block_counts}})

    # 2. 风况失衡：分钟级均值差（m/s）与航次级风差值的随机化检验并列报告。
    wind_means = {mode: accounts[mode]['wind_mean'] for mode in design.modes}
    wind_gap = None
    if wind_means[base] is not None and wind_means[comparison] is not None:
        wind_gap = wind_means[comparison] - wind_means[base]
    wind_contrasts = {}
    for flight, flight_blocks in blocks.items():
        per_mode = {}
        for block in flight_blocks.values():
            if block['status'] != BLOCK_VALID or not block['retained_wind_values']:
                continue
            per_mode.setdefault(block['planned_mode'], []).extend(
                block['retained_wind_values'])
        if base in per_mode and comparison in per_mode:
            wind_contrasts[flight] = (
                math.fsum(per_mode[comparison]) / len(per_mode[comparison])
                - math.fsum(per_mode[base]) / len(per_mode[base]))
    wind_test = _mode_wind_test(wind_contrasts, design)
    wind_imbalanced = wind_gap is not None and abs(wind_gap) > design.wind_imbalance_limit
    if wind_imbalanced:
        flags.append({'code': 'wind_imbalance',
                      'detail': {'wind_mean_by_mode': wind_means,
                                 'difference_mps': wind_gap,
                                 'limit_mps': design.wind_imbalance_limit,
                                 **(wind_test or {})}})

    # 3. 执行偏离：实际 mode 只用于诊断，不进入 ITT 分配。
    retained_planned = sum(accounts[m]['planned_minutes'] for m in design.modes)
    deviation_minutes = sum(accounts[m]['deviation_minutes'] for m in design.modes)
    deviation_rate = deviation_minutes / retained_planned if retained_planned else None
    deviation_by_mode = {mode: {
        'deviation_minutes': accounts[mode]['deviation_minutes'],
        'planned_minutes': accounts[mode]['planned_minutes']}
        for mode in design.modes}
    deviation_excessive = (
        deviation_rate is not None and deviation_rate > design.deviation_rate_limit)
    if deviation_excessive:
        flags.append({'code': 'execution_deviation',
                      'detail': {'deviation_rate': deviation_rate,
                                 'limit': design.deviation_rate_limit,
                                 'by_mode': deviation_by_mode}})

    # 4. 合格航次少于 2 时符号翻转的随机化空间不足。
    if test['eligible_flights'] < 2:
        flags.append({'code': 'few_eligible_flights',
                      'detail': {'eligible_flights': test['eligible_flights'],
                                 'required': 2}})

    if reasons:
        flags.append({'code': 'flights_excluded_from_contrast',
                      'detail': reasons})

    downgraded = bool(flags)
    return {
        'wind': {'wind_mean_by_mode': wind_means,
                 'difference_mps': wind_gap,
                 'limit_mps': design.wind_imbalance_limit,
                 'imbalanced': wind_imbalanced,
                 **(wind_test or {})},
        'execution_deviation': {'rate': deviation_rate,
                                'limit': design.deviation_rate_limit,
                                'excessive': deviation_excessive,
                                'by_mode': deviation_by_mode},
        'valid_blocks_by_mode': block_counts,
        'min_valid_blocks_required': design.min_valid_blocks,
        'flags': flags,
        'interpretation': 'descriptive_only' if downgraded else 'confirmatory',
        'interpretation_reasons': (
            ['存在触发降级的诊断标记，随机化结果仅作描述性参考'] if downgraded
            else ['有效区块、风况平衡与执行偏离均在预设阈值内']),
    }


def analyze_once(records, design):
    validate_against_records(design, records)
    blocks = build_blocks(records, design)
    contrasts, eligible, reasons = flight_contrasts(blocks, design)
    accounts = pooled_mode_accounts(blocks)
    audit = exposure_audit(blocks)
    status_counts = count_by_status(blocks)
    if not contrasts:
        raise ValueError('没有任何航次同时具备两种模式的有效区块，无法进行随机化对比')
    test = randomization_test(contrasts, design)

    base, comparison = design.baseline_mode, design.comparison_mode
    pooled_gap = None
    if all(accounts[m]['planned_minutes'] for m in design.modes):
        pooled_gap = (
            accounts[comparison]['energy_total'] / accounts[comparison]['planned_minutes']
            - accounts[base]['energy_total'] / accounts[base]['planned_minutes'])
    test['pooled_minute_rate_difference_kwh'] = pooled_gap
    test['pooled_rate_note'] = '仅作口径核对的描述量，随机化检验以航次级差值为准'

    diagnostics = _diagnose(blocks, accounts, contrasts, design, reasons, test)
    return {
        'design': design.as_dict(),
        'rules': {
            'cutoff': 'minute >= cutoff_minute 不进入处理对比；跨截止区块保留截止前分钟',
            'washout': '非首区块切换后前 washout_minutes 分钟剔除对比但计入计划暴露；'
                       '掩码只依赖切换点，对模式取补不变，全部再随机化共用',
            'downtime': 'available=0 的 retained 分钟照常计入计划暴露，energy 按零聚合',
            'missing_block': '设计有、数据无的区块标记 missing，不计对比只披露',
            'intention_to_treat': '对比按设计的 planned_mode；实际 mode 仅用于偏离诊断',
        },
        'block_status_counts': status_counts,
        'exposure_audit': audit,
        'mode_accounts': accounts,
        'flight_contrasts_kwh': dict(contrasts),
        'eligible_flights': eligible,
        'randomization_test': test,
        'diagnostics': diagnostics,
    }


def washout_sensitivity(records, design):
    """洗脱窗口扫描：同样的截止与门槛，只改变 washout_minutes 重跑完整分析。"""
    grid = sorted(set(design.washout_grid))
    results = []
    for washout in grid:
        variant = copy.copy(design)
        variant.washout_minutes = washout
        result = analyze_once(records, variant)
        test = result['randomization_test']
        results.append({
            'washout_minutes': washout,
            'effect_kwh': test['effect_kwh'],
            'p_value': test['p_value'],
            'ci_lower_kwh': test['ci_lower_kwh'],
            'ci_upper_kwh': test['ci_upper_kwh'],
            'eligible_flights': test['eligible_flights'],
            'valid_blocks_by_mode': result['diagnostics']['valid_blocks_by_mode'],
            'retained_minutes': result['exposure_audit']['retained_minutes'],
            'washout_minutes_total': result['exposure_audit']['washout_minutes'],
            'interpretation': result['diagnostics']['interpretation'],
            'flag_codes': [flag['code'] for flag in result['diagnostics']['flags']],
        })
    signs = [item['effect_kwh'] for item in results if item['effect_kwh'] is not None]
    reversal = len({1 if value > 0 else (-1 if value < 0 else 0) for value in signs}) > 1
    significant = [item['washout_minutes'] for item in results
                   if item['ci_lower_kwh'] is not None
                   and (item['ci_lower_kwh'] > 0 or item['ci_upper_kwh'] < 0)]
    return {
        'grid_minutes': grid,
        'results': results,
        'sign_reversal': reversal,
        'significant_windows_minutes': significant,
        'note': '结论在任一洗脱窗口下方向反转，或显著性随窗口得失，均视为结论不稳健',
    }


def run_switchback(records, design):
    primary = analyze_once(records, design)
    sensitivity = washout_sensitivity(records, design)
    primary['washout_sensitivity'] = sensitivity
    return primary
