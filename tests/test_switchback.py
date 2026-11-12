import copy
import json
import subprocess
import sys
import tempfile
import unittest
from itertools import product
from pathlib import Path

from analysis_app.blocks import (BLOCK_MISSING, BLOCK_VALID, build_blocks,
                                 classify_minute, exposure_audit,
                                 flight_contrasts, pooled_mode_accounts)
from analysis_app.cli import save_json
from analysis_app.design import (DesignError, SwitchbackDesign,
                                 validate_against_records)
from analysis_app.inference import analyze_once, run_switchback
from analysis_app.io import load_records
from analysis_app.randomization import (build_sign_distribution,
                                        null_statistics,
                                        p_value_for_observed,
                                        randomization_test)
from analysis_app.synthetic import generate_flights, scenario_suite

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / 'scripts' / 'run_analysis.py'


def make_design(allocations, **overrides):
    payload = {'modes': ['fixed', 'adaptive'], 'washout_minutes': 1,
               'washout_grid': [0, 1, 2], 'cutoff_minute': 10_000,
               'min_valid_blocks': 2, 'enumeration_flight_cap': 24,
               'allocations': allocations}
    payload.update(overrides)
    return SwitchbackDesign(payload)


class DesignTest(unittest.TestCase):
    def setUp(self):
        self.records = load_records(ROOT / 'data' / 'observations.csv')
        allocations = {}
        for row in self.records:
            allocations.setdefault(row['flight'], {})[row['block']] = row['planned_mode']
        self.allocations = {flight: [{'block': block, 'planned_mode': mode}
                                     for block, mode in sorted(plan.items())]
                            for flight, plan in sorted(allocations.items())}

    def test_valid_design_accepts_records(self):
        design = make_design(self.allocations, cutoff_minute=12)
        validate_against_records(design, self.records)

    def test_allocation_mismatch_rejected(self):
        wrong = copy.deepcopy(self.allocations)
        first_flight = sorted(wrong)[0]
        entry = wrong[first_flight][0]
        entry['planned_mode'] = ('adaptive' if entry['planned_mode'] == 'fixed'
                                 else 'fixed')
        design = make_design(wrong)
        with self.assertRaisesRegex(DesignError, '不一致'):
            validate_against_records(design, self.records)

    def test_unknown_mode_rejected(self):
        with self.assertRaises(DesignError):
            SwitchbackDesign({'modes': ['fixed', 'adaptive'],
                              'washout_minutes': 1, 'cutoff_minute': 12,
                              'min_valid_blocks': 2,
                              'allocations': {'F': [{'block': 0, 'planned_mode': 'turbo'}]}})

    def test_negative_washout_rejected(self):
        with self.assertRaises(DesignError):
            make_design({'F': [{'block': 0, 'planned_mode': 'fixed'}]},
                        washout_minutes=-1)

    def test_missing_block_in_data_is_allowed_and_marked(self):
        records, allocations = generate_flights(
            'missing_blocks', **scenario_suite()['missing_blocks'])
        design = make_design(allocations)
        validate_against_records(design, records)
        blocks = build_blocks(records, design)
        statuses = [block['status'] for flight in blocks.values()
                    for block in flight.values()]
        self.assertIn(BLOCK_MISSING, statuses)
        self.assertEqual(exposure_audit(blocks)['missing_blocks'], 2)

    def test_extra_block_in_data_rejected(self):
        records, allocations = generate_flights(
            'null_effect', **scenario_suite()['null_effect'])
        design = make_design(allocations)
        pruned = {flight: plan[:-1] for flight, plan in allocations.items()}
        pruned_design = make_design(pruned)
        with self.assertRaisesRegex(DesignError, '设计未覆盖'):
            validate_against_records(pruned_design, records)


class ClassificationTest(unittest.TestCase):
    def test_washout_includes_switch_minute(self):
        # 切换当分钟（delta=0）即进入洗脱
        self.assertEqual(classify_minute(3, 3, 1, 1, 99), 'washout')
        self.assertEqual(classify_minute(4, 3, 1, 1, 99), 'retained')

    def test_washout_window_of_two(self):
        self.assertEqual(classify_minute(3, 3, 1, 2, 99), 'washout')
        self.assertEqual(classify_minute(4, 3, 1, 2, 99), 'washout')
        self.assertEqual(classify_minute(5, 3, 1, 2, 99), 'retained')

    def test_first_block_never_washed_out(self):
        self.assertEqual(classify_minute(0, 0, 0, 2, 99), 'retained')

    def test_cutoff_takes_precedence(self):
        self.assertEqual(classify_minute(12, 12, 1, 2, 12), 'after_cutoff')
        self.assertEqual(classify_minute(11, 9, 1, 2, 12), 'retained')

    def test_zero_washout_keeps_switch_minute(self):
        self.assertEqual(classify_minute(3, 3, 1, 0, 99), 'retained')

    def test_cutoff_boundary_minute_is_excluded(self):
        # 边界规则 minute >= cutoff：截止分钟本身不进入对比
        self.assertEqual(classify_minute(11, 11, 2, 0, 11), 'after_cutoff')
        self.assertEqual(classify_minute(10, 11, 2, 0, 11), 'retained')


class RandomizationTest(unittest.TestCase):
    def setUp(self):
        records, allocations = generate_flights(
            'null_effect', **scenario_suite()['null_effect'])
        self.records = records
        self.design = make_design(allocations)
        blocks = build_blocks(records, self.design)
        self.contrasts, self.eligible, _ = flight_contrasts(blocks, self.design)

    def test_enumeration_space_is_two_to_the_k(self):
        distribution = build_sign_distribution(8, self.design)
        self.assertEqual(distribution.method, 'exact_enumeration')
        self.assertEqual(distribution.replications, 256)
        self.assertEqual(len(set(distribution.matrix)), 256)

    def test_observed_assignment_is_in_enumeration(self):
        values = list(self.contrasts.values())
        null_values = null_statistics(values,
                                      build_sign_distribution(len(values), self.design))
        observed = sum(values) / len(values)
        self.assertIn(observed, null_values)

    def test_null_effect_not_significant_and_ci_contains_zero(self):
        result = randomization_test(self.contrasts, self.design)
        self.assertGreater(result['p_value'], 0.05)
        self.assertLess(result['ci_lower_kwh'], 0)
        self.assertGreater(result['ci_upper_kwh'], 0)

    def test_clear_effect_is_detected(self):
        records, allocations = generate_flights(
            'clear_effect', **scenario_suite()['clear_effect'])
        design = make_design(allocations)
        contrasts, eligible, _ = flight_contrasts(build_blocks(records, design), design)
        result = randomization_test(contrasts, design)
        self.assertLess(result['p_value'], 0.05)
        self.assertGreater(result['effect_kwh'], 1.0)
        self.assertGreater(result['ci_lower_kwh'], 0.0)

    def test_sign_flip_is_flight_level_only(self):
        # 任意再随机化都是逐航次整体取补：检验 2^K 个组合均由整符号向量构成
        distribution = build_sign_distribution(3, self.design)
        self.assertEqual(set(distribution.matrix), set(product((-1, 1), repeat=3)))

    def test_monte_carlo_is_seeded_and_paired(self):
        design = make_design({'F': [{'block': 0, 'planned_mode': 'fixed'}]},
                             enumeration_flight_cap=1,
                             monte_carlo_replications=100, random_seed=7)
        first = build_sign_distribution(4, design)
        second = build_sign_distribution(4, design)
        self.assertEqual(first.matrix, second.matrix)
        self.assertEqual(first.method, 'seeded_monte_carlo')
        # 成对取反：每个符号向量的相反数也在矩阵中
        vectors = set(first.matrix)
        self.assertTrue(all(tuple(-s for s in vector) in vectors
                            for vector in vectors))

    def test_monte_carlo_p_uses_plus_one(self):
        design = make_design({'F': [{'block': 0, 'planned_mode': 'fixed'}]},
                             enumeration_flight_cap=1,
                             monte_carlo_replications=99, random_seed=3)
        distribution = build_sign_distribution(5, design)
        values = [1.0] * 5
        observed = 5.0
        p_value = p_value_for_observed(observed,
                                       null_statistics(values, distribution),
                                       distribution)
        self.assertGreater(p_value, 0.0)
        self.assertEqual(p_value, 1 / 100)

    def test_replay_is_byte_stable(self):
        first = randomization_test(self.contrasts, self.design)
        second = randomization_test(self.contrasts, self.design)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_monte_carlo_replay_is_byte_stable(self):
        design = make_design({'F': [{'block': 0, 'planned_mode': 'fixed'}]},
                             enumeration_flight_cap=1,
                             monte_carlo_replications=200, random_seed=11)
        contrasts = {f'F{i:02d}': float(i + 1) for i in range(6)}
        first = randomization_test(contrasts, design)
        second = randomization_test(contrasts, design)
        self.assertEqual(first, second)
        self.assertEqual(first['randomization_method'], 'seeded_monte_carlo')
        self.assertEqual(first['replications'], 200)


class EstimandRuleTest(unittest.TestCase):
    def test_downtime_minutes_remain_in_planned_exposure(self):
        records, allocations = generate_flights(
            'mixed_lengths', **scenario_suite()['mixed_lengths'])
        design = make_design(allocations)
        blocks = build_blocks(records, design)
        accounts = pooled_mode_accounts(blocks)
        # planned_minutes 包含停机分钟，因此严格大于 available_minutes
        self.assertTrue(any(accounts[m]['planned_minutes']
                            > accounts[m]['available_minutes'] for m in design.modes))
        audit = exposure_audit(blocks)
        self.assertEqual(audit['retained_minutes'],
                         sum(accounts[m]['planned_minutes'] for m in design.modes))
        # 所有输入分钟都有明确去向
        self.assertEqual(audit['total_input_minutes'], len(records))

    def test_energy_conservation_across_categories(self):
        records, allocations = generate_flights(
            'null_effect', **scenario_suite()['null_effect'])
        design = make_design(allocations, washout_minutes=1)
        blocks = build_blocks(records, design)
        audit = exposure_audit(blocks)
        total = (audit['retained_energy'] + audit['washout_energy']
                 + audit['after_cutoff_energy'])
        self.assertAlmostEqual(total, sum(row['energy'] for row in records), places=9)

    def test_cutoff_keeps_pre_cutoff_minutes_in_straddling_block(self):
        records, allocations = generate_flights(
            'null_effect', **scenario_suite()['null_effect'])
        design = make_design(allocations, cutoff_minute=10, washout_minutes=0)
        blocks = build_blocks(records, design)
        audit = exposure_audit(blocks)
        # 8 航次 * 16 分钟，cutoff=10 截断每航次最后 6 分钟
        self.assertEqual(audit['after_cutoff_minutes'], 8 * 6)
        self.assertEqual(audit['retained_minutes'], 8 * 10)

    def test_itt_uses_planned_mode_actual_mode_only_diagnostic(self):
        records, allocations = generate_flights(
            'null_effect', **scenario_suite()['null_effect'], deviation_rate=0.5)
        design = make_design(allocations)
        result = analyze_once(records, design)
        # 对比分配仍按 planned_mode：两模式区块数完全对称
        counts = result['diagnostics']['valid_blocks_by_mode']
        self.assertEqual(counts['fixed'], counts['adaptive'])
        self.assertGreater(
            result['diagnostics']['execution_deviation']['rate'], 0.1)
        self.assertEqual(result['diagnostics']['interpretation'],
                         'descriptive_only')

    def test_wind_imbalance_downgrades(self):
        records, allocations = generate_flights(
            'null_effect', **scenario_suite()['null_effect'])
        # 人为让所有 adaptive 分钟风速抬高，制造失衡
        for row in records:
            if row['planned_mode'] == 'adaptive':
                row['wind'] += 3.0
        design = make_design(allocations, wind_imbalance_limit=0.5)
        result = analyze_once(records, design)
        codes = [flag['code'] for flag in result['diagnostics']['flags']]
        self.assertIn('wind_imbalance', codes)
        self.assertEqual(result['diagnostics']['interpretation'],
                         'descriptive_only')

    def test_min_valid_blocks_threshold(self):
        records, allocations = generate_flights(
            'null_effect', **scenario_suite()['null_effect'])
        design = make_design(allocations, min_valid_blocks=99)
        result = analyze_once(records, design)
        codes = [flag['code'] for flag in result['diagnostics']['flags']]
        self.assertIn('min_valid_blocks_unmet', codes)

    def test_result_echoes_design_parameters(self):
        records, allocations = generate_flights(
            'null_effect', **scenario_suite()['null_effect'])
        design = make_design(allocations, washout_minutes=2, cutoff_minute=13,
                             min_valid_blocks=3, random_seed=4242,
                             monte_carlo_replications=500)
        result = analyze_once(records, design)
        echoed = result['design']
        self.assertEqual(echoed['washout_minutes'], 2)
        self.assertEqual(echoed['cutoff_minute'], 13)
        self.assertEqual(echoed['min_valid_blocks'], 3)
        self.assertEqual(echoed['random_seed'], 4242)
        self.assertEqual(echoed['monte_carlo_replications'], 500)
        self.assertEqual(echoed['allocations'], allocations)

    def test_no_contrasting_flight_raises(self):
        records, allocations = generate_flights(
            'null_effect', **scenario_suite()['null_effect'])
        # 截止点早于第二个区块，所有航次只剩单一模式
        design = make_design(allocations, cutoff_minute=4, washout_minutes=0)
        with self.assertRaisesRegex(ValueError, '无法进行随机化对比'):
            analyze_once(records, design)


class CarryoverSensitivityTest(unittest.TestCase):
    def test_washout_moves_carryover_bias_toward_true_effect(self):
        params = scenario_suite()['carryover']
        records, allocations = generate_flights('carryover', **params)
        effects = {}
        for washout in (0, 1, 2):
            design = make_design(allocations, washout_minutes=washout)
            result = analyze_once(records, design)
            effects[washout] = result['randomization_test']['effect_kwh']
        # 真效应 1.5 kWh/分钟；洗脱越充分，估计越接近真值且单调回升
        self.assertLess(effects[0], effects[1])
        self.assertLess(effects[1], effects[2])
        self.assertAlmostEqual(effects[2], 1.5, delta=0.2)

    def test_sensitivity_grid_reports_reversal_flag(self):
        records, allocations = generate_flights(
            'null_effect', **scenario_suite()['null_effect'])
        design = make_design(allocations, washout_grid=[0, 1, 2])
        result = run_switchback(records, design)
        grid = result['washout_sensitivity']
        self.assertEqual(grid['grid_minutes'], [0, 1, 2])
        self.assertIn(grid['sign_reversal'], (True, False))
        self.assertEqual(len(grid['results']), 3)


class MixedLengthTest(unittest.TestCase):
    def test_flights_of_different_length_all_eligible(self):
        records, allocations = generate_flights(
            'mixed_lengths', **scenario_suite()['mixed_lengths'])
        design = make_design(allocations)
        result = analyze_once(records, design)
        self.assertEqual(result['randomization_test']['eligible_flights'], 10)
        self.assertTrue(all(block['status'] == BLOCK_VALID
                            for flight in build_blocks(records, design).values()
                            for block in flight.values()))


class CliTest(unittest.TestCase):
    def test_switchback_cli_matches_library(self):
        records = load_records(ROOT / 'data' / 'observations.csv')
        design_path = ROOT / 'design' / 'switchback_design.json'
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            run = subprocess.run(
                [sys.executable, str(ENTRY), 'switchback',
                 '--design', str(design_path), '--output', str(output)],
                text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(run.returncode, 0, run.stderr)
            design = SwitchbackDesign(json.loads(design_path.read_text(encoding='utf-8')))
            expected = run_switchback(records, design)
            self.assertEqual(json.loads(run.stdout), expected)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8')), expected)

    def test_demo_command_runs_single_screen(self):
        run = subprocess.run([sys.executable, str(ENTRY), 'demo'],
                             text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('switchback 分析演示', run.stdout)
        for scenario in scenario_suite():
            self.assertIn(scenario, run.stdout)

    def test_same_config_replay_is_identical(self):
        design_path = ROOT / 'design' / 'switchback_design.json'
        run_one = subprocess.run(
            [sys.executable, str(ENTRY), 'switchback',
             '--design', str(design_path)],
            text=True, capture_output=True, cwd=ROOT)
        run_two = subprocess.run(
            [sys.executable, str(ENTRY), 'switchback',
             '--design', str(design_path)],
            text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(run_one.returncode, 0, run_one.stderr)
        self.assertEqual(run_one.stdout, run_two.stdout)

    def test_bad_design_fails_without_output(self):
        with tempfile.TemporaryDirectory() as directory:
            design_path = Path(directory) / 'bad.json'
            design_path.write_text('{不是合法json', encoding='utf-8')
            output = Path(directory) / 'result.json'
            run = subprocess.run(
                [sys.executable, str(ENTRY), 'switchback',
                 '--design', str(design_path), '--output', str(output)],
                text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(run.returncode, 2)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
