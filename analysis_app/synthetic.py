"""确定性合成航次生成器，仅用于验证分析口径，不模拟真实飞控参数。

每个航次为若干等长区块，区块内逐分钟生成 wind 与 energy；支持注入
模式处理效应、切换后残留效应、停机分钟与整块缺失。生成器只依赖标准库，
固定种子即可逐字节重放。
"""

import math
import random

FIELDNAMES = ['observation_id', 'flight', 'mode', 'block', 'minute', 'energy',
              'wind', 'switch_minute', 'planned_mode', 'available']


def _energy(wind_deviation, planned_mode, effect_kwh, minutes_since_switch,
            carryover_minutes, previous_mode, current_mode, rng):
    # wind_deviation 是相对航次风基线的偏离；航次基线在航次级模式差中抵消
    value = 10.0 + 1.1 * wind_deviation
    value += effect_kwh if planned_mode == 'adaptive' else 0.0
    # 残留效应：切换后 carryover_minutes 分钟内，前一模式的效应全额残留，
    # 不做块内衰减，使残留即使在区块均值口径下也能被洗脱窗口识别
    if previous_mode is not None and previous_mode != current_mode \
            and minutes_since_switch < max(1, int(carryover_minutes)):
        legacy = effect_kwh if previous_mode == 'adaptive' else 0.0
        current = effect_kwh if current_mode == 'adaptive' else 0.0
        value += legacy - current
    return max(0.0, value + rng.gauss(0.0, 0.15))


def generate_flights(scenario, flights, blocks_per_flight, block_minutes,
                     seed, effect_kwh=0.0, carryover_minutes=0.0,
                     downtime_rate=0.0, missing_blocks=None,
                     deviation_rate=0.0):
    """返回 (records, allocations)。allocations 为预先确定的区块分配。"""
    rng = random.Random(seed)
    missing_blocks = set(missing_blocks or [])
    if isinstance(block_minutes, int):
        length_schedule = [block_minutes] * flights
    else:
        if len(block_minutes) != flights:
            raise ValueError('length_schedule 长度必须与航次数一致')
        length_schedule = list(block_minutes)
    records = []
    allocations = {}
    counter = 0
    for flight_index in range(flights):
        flight = f'{scenario}-F{flight_index:02d}'
        flight_wind = 6.0 + rng.random() * 4.0
        start_mode = 'adaptive' if flight_index % 2 else 'fixed'
        plan = []
        allocation = []
        minute_cursor = 0
        previous_mode = None
        span = length_schedule[flight_index]
        for block_index in range(blocks_per_flight):
            planned = start_mode if block_index % 2 == 0 else (
                'adaptive' if start_mode == 'fixed' else 'fixed')
            switch_minute = minute_cursor
            allocation.append({'block': block_index, 'planned_mode': planned})
            if (flight, block_index) in missing_blocks:
                minute_cursor += span
                previous_mode = planned
                continue
            plan.append((block_index, planned, switch_minute))
            for offset in range(span):
                minute = switch_minute + offset
                # 风况在航次内围绕航次级基线小幅波动，体现同航次风况相关
                wind = flight_wind + 0.3 * math.sin(minute / 4.0) \
                    + rng.gauss(0.0, 0.1)
                available = 0 if rng.random() < downtime_rate else 1
                actual = planned
                if available and rng.random() < deviation_rate:
                    actual = 'adaptive' if planned == 'fixed' else 'fixed'
                energy = 0.0
                if available:
                    energy = _energy(
                        wind - flight_wind, planned, effect_kwh, offset,
                        carryover_minutes, previous_mode, planned, rng)
                records.append({
                    'observation_id': f'{scenario}-obs{counter:04d}',
                    'flight': flight, 'mode': actual, 'block': block_index,
                    'minute': minute, 'energy': round(energy, 6),
                    'wind': round(wind, 6), 'switch_minute': switch_minute,
                    'planned_mode': planned, 'available': available})
                counter += 1
            minute_cursor += span
            previous_mode = planned
        allocations[flight] = allocation
    return records, allocations


def scenario_suite():
    """四组必验场景 + 一组带明显效应的阳性对照，全部小样本可枚举。"""
    return {
        'null_effect': dict(flights=8, blocks_per_flight=4, block_minutes=4,
                            seed=101, effect_kwh=0.0, carryover_minutes=0.0),
        'carryover': dict(flights=8, blocks_per_flight=4, block_minutes=4,
                          seed=202, effect_kwh=1.5, carryover_minutes=2),
        'missing_blocks': dict(flights=8, blocks_per_flight=4, block_minutes=4,
                               seed=303, effect_kwh=1.0,
                               missing_blocks={('missing_blocks-F02', 1),
                                               ('missing_blocks-F05', 3)}),
        'mixed_lengths': dict(flights=10, blocks_per_flight=4,
                              block_minutes=[3, 4, 5, 3, 4, 5, 3, 4, 5, 4],
                              seed=404, effect_kwh=1.0, downtime_rate=0.1),
        'clear_effect': dict(flights=8, blocks_per_flight=4, block_minutes=4,
                             seed=505, effect_kwh=1.5, carryover_minutes=0.0),
    }
