import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .design import DesignError, SwitchbackDesign, load_design
from .inference import analyze_once, run_switchback
from .io import load_records
from .summary import summarize
from .synthetic import generate_flights, scenario_suite


def save_json(path, result):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.analysis-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def print_json(result):
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))


def run_demo():
    """一次本地命令看完的正式演示：合成场景验证 + 真实数据主分析摘要。"""
    lines = []
    lines.append('switchback 分析演示（合成数据，精确枚举，可重放）')
    lines.append('场景             效应kWh/分钟 p值   95%随机化区间      合格航次  降级标记')
    for name, params in scenario_suite().items():
        records, allocations = generate_flights(name, **params)
        design_payload = {
            'modes': ['fixed', 'adaptive'], 'washout_minutes': 1,
            'washout_grid': [0, 1], 'cutoff_minute': 10_000,
            'min_valid_blocks': 2, 'enumeration_flight_cap': 24,
            'allocations': allocations}
        design = SwitchbackDesign(design_payload)
        result = run_switchback(records, design)
        test = result['randomization_test']
        flags = ','.join(flag['code'] for flag in result['diagnostics']['flags']) or '-'
        ci = (f"[{test['ci_lower_kwh']:+.2f},{test['ci_upper_kwh']:+.2f}]"
              if test['ci_lower_kwh'] is not None else '不可界定')
        lines.append(
            f"{name:<16} {test['effect_kwh']:+7.3f} {test['p_value']:5.3f} "
            f"{ci:<18} {test['eligible_flights']:>4}    {flags}")
    lines.append('')
    lines.append('洗脱敏感性（carryover 场景，检验残留是否被洗脱窗口吸收）')
    records, allocations = generate_flights('carryover', **scenario_suite()['carryover'])
    for washout in (0, 1, 2):
        design = SwitchbackDesign({'modes': ['fixed', 'adaptive'],
                                   'washout_minutes': washout, 'cutoff_minute': 10_000,
                                   'min_valid_blocks': 2,
                                   'enumeration_flight_cap': 24,
                                   'allocations': allocations})
        result = analyze_once(records, design)
        test = result['randomization_test']
        lines.append(f"  washout={washout} 效应={test['effect_kwh']:+.3f} kWh/分钟 "
                     f"p={test['p_value']:.3f} 保留分钟={result['exposure_audit']['retained_minutes']}")
    lines.append('')
    lines.append('完整正式分析：python3 -m analysis_app switchback '
                 '--design design/switchback_design.json')
    print('\n'.join(lines))
    return 0


def main():
    parser = argparse.ArgumentParser(description='本地观测数据分析')
    commands = parser.add_subparsers(dest='command', required=True)
    summary = commands.add_parser('summary', help='按业务维度核对观测口径')
    summary.add_argument('--input', type=Path, default=Path('data/observations.csv'))
    summary.add_argument('--output', type=Path)

    switchback = commands.add_parser('switchback', help='switchback 实验意向处理分析')
    switchback.add_argument('--input', type=Path, default=Path('data/observations.csv'))
    switchback.add_argument('--design', type=Path, required=True,
                            help='预先确定的区块分配与分析参数 JSON')
    switchback.add_argument('--output', type=Path)

    commands.add_parser('demo', help='合成航次验证与单屏演示')
    args = parser.parse_args()
    try:
        if args.command == 'demo':
            return run_demo()
        records = load_records(args.input)
        if args.command == 'summary':
            result = summarize(records)
        else:
            design = load_design(args.design)
            result = run_switchback(records, design)
        if args.output:
            save_json(args.output, result)
        print_json(result)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
