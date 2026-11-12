"""分钟分类与区块聚合。

处置规则（同一套规则同时用于观测分配和每一次再随机化）：

1. 截止规则：minute >= cutoff_minute 的分钟一律不进入处理对比；跨越截止点
   的区块只保留其截止前分钟，完全落在截止点之后的区块标记为 excluded_cutoff。
   选择按分钟截断而不是整块丢弃，是为了不浪费截止点前的计划暴露，同时保证
   截止点对两种模式对称。
2. 洗脱规则：区块起始分钟记为 switch_minute。除航次首个区块外，每个区块
   切换后的前 washout_minutes 分钟（含切换当分钟，满足
   0 <= minute - switch_minute < W）标记为 washout，不进入处理对比，但
   计入计划暴露与洗脱核算。洗脱掩码只依赖设计切换点，对计划模式取补不变，
   因此所有再随机化复用同一掩码，不会在随机化检验中被人为改动。
3. 停机保留：available=0 的分钟只要落在 retained 区间就照常计入计划暴露，
   energy 按零参与聚合，不得因低产被选择性剔除。
4. 缺失区块：设计中存在但数据里没有任何分钟的区块标记为 missing，
   不进入对比，只在有效分配数与诊断中披露。
"""

import math
from collections import OrderedDict, defaultdict

CATEGORY_RETAINED = 'retained'
CATEGORY_WASHOUT = 'washout'
CATEGORY_AFTER_CUTOFF = 'after_cutoff'

BLOCK_VALID = 'valid'
BLOCK_NO_RETAINED = 'no_retained_minutes'
BLOCK_AFTER_CUTOFF = 'excluded_cutoff'
BLOCK_MISSING = 'missing'


def classify_minute(minute, switch_minute, block_index, washout_minutes, cutoff_minute):
    if minute >= cutoff_minute:
        return CATEGORY_AFTER_CUTOFF
    if block_index > 0 and 0 <= minute - switch_minute < washout_minutes:
        return CATEGORY_WASHOUT
    return CATEGORY_RETAINED


def build_blocks(records, design):
    """按航次/区块聚合，返回 OrderedDict[flight] -> OrderedDict[block] -> dict。"""
    grouped = defaultdict(list)
    for row in records:
        grouped[(row['flight'], row['block'])].append(row)

    result = OrderedDict()
    for flight, plan in design.allocations.items():
        flight_blocks = OrderedDict()
        for block_index, planned_mode in plan.items():
            rows = sorted(grouped.get((flight, block_index), []),
                          key=lambda row: row['minute'])
            entry = {
                'flight': flight, 'block': block_index,
                'planned_mode': planned_mode, 'status': BLOCK_VALID,
                'retained_energy': 0.0, 'retained_minutes': 0,
                'retained_available_minutes': 0,
                'retained_deviation_minutes': 0,
                'retained_wind_values': [],
                'washout_minutes': 0, 'washout_energy': 0.0,
                'after_cutoff_minutes': 0, 'after_cutoff_energy': 0.0,
                'pre_cutoff_minutes': 0,
            }
            if not rows:
                entry['status'] = BLOCK_MISSING
                flight_blocks[block_index] = entry
                continue
            for row in rows:
                category = classify_minute(
                    row['minute'], row['switch_minute'], block_index,
                    design.washout_minutes, design.cutoff_minute)
                if category == CATEGORY_AFTER_CUTOFF:
                    entry['after_cutoff_minutes'] += 1
                    entry['after_cutoff_energy'] += row['energy']
                    continue
                entry['pre_cutoff_minutes'] += 1
                if category == CATEGORY_WASHOUT:
                    entry['washout_minutes'] += 1
                    entry['washout_energy'] += row['energy']
                    continue
                entry['retained_minutes'] += 1
                entry['retained_available_minutes'] += row['available']
                entry['retained_energy'] += row['energy']
                entry['retained_wind_values'].append(row['wind'])
                if row['mode'] != planned_mode:
                    entry['retained_deviation_minutes'] += 1
            entry['retained_energy'] = math.fsum(
                [entry['retained_energy']]) if entry['retained_minutes'] else 0.0
            entry['washout_energy'] = math.fsum(
                [entry['washout_energy']]) if entry['washout_minutes'] else 0.0
            entry['after_cutoff_energy'] = math.fsum(
                [entry['after_cutoff_energy']]) if entry['after_cutoff_minutes'] else 0.0
            if entry['retained_minutes'] == 0:
                entry['status'] = (BLOCK_AFTER_CUTOFF if entry['pre_cutoff_minutes'] == 0
                                   else BLOCK_NO_RETAINED)
            flight_blocks[block_index] = entry
        result[flight] = flight_blocks
    return result


def valid_blocks(blocks):
    return [block for flight in blocks.values() for block in flight.values()
            if block['status'] == BLOCK_VALID]


def count_by_status(blocks):
    counts = defaultdict(int)
    for flight in blocks.values():
        for block in flight.values():
            counts[block['status']] += 1
    return dict(counts)


def flight_contrasts(blocks, design):
    """航次级分钟均值差值（comparison - baseline）。

    每个合格航次分别计算两种模式 retained 分钟（含 available=0 的计划暴露
    分钟）的分钟均值，再做差。以分钟均值而非区块均值做估计量，才能让切换
    后前几分钟的残留效应在估计中显形（区块均值会让相邻双向切换的残留互相
    抵消）；随机化单位仍是整个航次，差值在再随机化下整体变号。
    只对两种模式都有 retained 分钟的航次计算。
    """
    contrasts = OrderedDict()
    eligible = []
    reasons = []
    base, comparison = design.baseline_mode, design.comparison_mode
    for flight, flight_blocks in blocks.items():
        per_mode = defaultdict(lambda: {'energy': 0.0, 'minutes': 0})
        for block in flight_blocks.values():
            if block['status'] != BLOCK_VALID:
                continue
            bucket = per_mode[block['planned_mode']]
            bucket['energy'] += block['retained_energy']
            bucket['minutes'] += block['retained_minutes']
        if base not in per_mode or comparison not in per_mode \
                or per_mode[base]['minutes'] == 0 \
                or per_mode[comparison]['minutes'] == 0:
            reasons.append({'flight': flight,
                            'baseline_retained_minutes': per_mode[base]['minutes'],
                            'comparison_retained_minutes':
                                per_mode[comparison]['minutes'],
                            'reason': '单一模式缺少 retained 分钟，航次不进入随机化对比'})
            continue
        base_rate = per_mode[base]['energy'] / per_mode[base]['minutes']
        comparison_rate = (per_mode[comparison]['energy']
                           / per_mode[comparison]['minutes'])
        contrasts[flight] = comparison_rate - base_rate
        eligible.append(flight)
    return contrasts, eligible, reasons


def pooled_mode_accounts(blocks):
    """与电量核算口径一致的模式级累计：retained 分钟（含 available=0）全量计入。"""
    accounts = {}
    for mode in sorted({block['planned_mode'] for flight in blocks.values()
                        for block in flight.values()}):
        accounts[mode] = {'energy_total': 0.0, 'planned_minutes': 0,
                          'available_minutes': 0, 'valid_blocks': 0,
                          'valid_flights': set(), 'wind_minute_values': [],
                          'deviation_minutes': 0}
    for flight, flight_blocks in blocks.items():
        for block in flight_blocks.values():
            if block['status'] != BLOCK_VALID:
                continue
            account = accounts[block['planned_mode']]
            account['energy_total'] += block['retained_energy']
            account['planned_minutes'] += block['retained_minutes']
            account['available_minutes'] += block['retained_available_minutes']
            account['valid_blocks'] += 1
            account['valid_flights'].add(flight)
            account['wind_minute_values'].extend(block['retained_wind_values'])
            account['deviation_minutes'] += block['retained_deviation_minutes']
    for account in accounts.values():
        account['energy_total'] = math.fsum([account['energy_total']])
        account['valid_flights'] = len(account['valid_flights'])
        winds = account.pop('wind_minute_values')
        account['wind_mean'] = math.fsum(winds) / len(winds) if winds else None
    return accounts


def exposure_audit(blocks):
    """全部分钟的去向核算，确保没有分钟被静默丢弃。"""
    audit = {'retained_minutes': 0, 'retained_energy': 0.0,
             'washout_minutes': 0, 'washout_energy': 0.0,
             'after_cutoff_minutes': 0, 'after_cutoff_energy': 0.0,
             'missing_blocks': 0, 'total_input_minutes': 0}
    for flight in blocks.values():
        for block in flight.values():
            if block['status'] == BLOCK_MISSING:
                audit['missing_blocks'] += 1
                continue
            audit['total_input_minutes'] += (
                block['retained_minutes'] + block['washout_minutes']
                + block['after_cutoff_minutes'])
            audit['retained_minutes'] += block['retained_minutes']
            audit['retained_energy'] += block['retained_energy']
            audit['washout_minutes'] += block['washout_minutes']
            audit['washout_energy'] += block['washout_energy']
            audit['after_cutoff_minutes'] += block['after_cutoff_minutes']
            audit['after_cutoff_energy'] += block['after_cutoff_energy']
    for key in ('retained_energy', 'washout_energy', 'after_cutoff_energy'):
        audit[key] = math.fsum([audit[key]])
    return audit
