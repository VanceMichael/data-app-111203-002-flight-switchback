"""预先确定的 switchback 设计参数。

分析显式接收评审前已经确定的区块分配与分析参数，而不是从观测数据中推断，
避免按结果挑选洗脱长度或截止点。所有默认阈值集中在此处并随结果回显。
"""

from collections import OrderedDict

# 随机化检验：航次数不超过该值时枚举全部 2^K 种航次级符号组合，
# 否则使用有种子的蒙特卡洛抽样（次数见 MonteCarloReplications）。
ENUMERATION_FLIGHT_CAP = 20
# 蒙特卡洛重复次数固定，保证同配置可重放；结果中披露该次数。
MONTE_CARLO_REPLICATIONS = 10000
DEFAULT_RANDOM_SEED = 20261112
# 风况失衡与执行偏离的描述性降级阈值（可在设计文件中覆盖）。
DEFAULT_WIND_IMBALANCE_LIMIT = 0.5
DEFAULT_DEVIATION_RATE_LIMIT = 0.1
DEFAULT_ALPHA = 0.05


class DesignError(ValueError):
    """设计文件与数据契约不一致。"""


class SwitchbackDesign:
    def __init__(self, payload):
        if not isinstance(payload, dict):
            raise DesignError('设计文件必须是 JSON 对象')
        try:
            self.modes = list(payload['modes'])
            self.washout_minutes = int(payload['washout_minutes'])
            self.cutoff_minute = int(payload['cutoff_minute'])
            self.min_valid_blocks = int(payload['min_valid_blocks'])
            allocations = payload['allocations']
        except (KeyError, TypeError, ValueError) as error:
            raise DesignError(f'设计文件缺少或含非法字段：{error}') from None
        if len(self.modes) != 2 or len(set(self.modes)) != 2 \
                or not all(isinstance(mode, str) and mode for mode in self.modes):
            raise DesignError('modes 必须是两个互不相同的非空模式名')
        if self.washout_minutes < 0:
            raise DesignError('washout_minutes 不能为负')
        if self.cutoff_minute <= 0:
            raise DesignError('cutoff_minute 必须为正整数')
        if self.min_valid_blocks < 1:
            raise DesignError('min_valid_blocks 至少为 1')
        if not isinstance(allocations, dict) or not allocations:
            raise DesignError('allocations 必须是非空的 flight -> 区块计划映射')
        self.allocations = OrderedDict()
        for flight, blocks in allocations.items():
            if not isinstance(flight, str) or not flight:
                raise DesignError('航次名必须是非空字符串')
            if not isinstance(blocks, list) or not blocks:
                raise DesignError(f'{flight} 的区块计划必须是非空列表')
            sequence = []
            for entry in blocks:
                if not isinstance(entry, dict) or set(entry) != {'block', 'planned_mode'}:
                    raise DesignError(f'{flight} 的区块条目只能含 block 与 planned_mode')
                index = entry['block']
                mode = entry['planned_mode']
                if not isinstance(index, int) or index < 0:
                    raise DesignError(f'{flight} 存在非法区块编号：{entry}')
                if mode not in self.modes:
                    raise DesignError(f'{flight} 区块{index}的 planned_mode 不在 modes 中')
                sequence.append((index, mode))
            indices = [index for index, _ in sequence]
            if len(set(indices)) != len(indices):
                raise DesignError(f'{flight} 的区块编号重复')
            self.allocations[flight] = OrderedDict(sorted(sequence))
        self.washout_grid = sorted({self.washout_minutes} | {
            int(value) for value in payload.get('washout_grid', [])})
        if any(value < 0 for value in self.washout_grid):
            raise DesignError('washout_grid 不能含负值')
        self.seed = int(payload.get('random_seed', DEFAULT_RANDOM_SEED))
        self.monte_carlo_replications = int(
            payload.get('monte_carlo_replications', MONTE_CARLO_REPLICATIONS))
        if self.monte_carlo_replications < 1:
            raise DesignError('monte_carlo_replications 至少为 1')
        self.enumeration_flight_cap = int(
            payload.get('enumeration_flight_cap', ENUMERATION_FLIGHT_CAP))
        self.wind_imbalance_limit = float(
            payload.get('wind_imbalance_limit', DEFAULT_WIND_IMBALANCE_LIMIT))
        self.deviation_rate_limit = float(
            payload.get('deviation_rate_limit', DEFAULT_DEVIATION_RATE_LIMIT))
        self.alpha = float(payload.get('alpha', DEFAULT_ALPHA))
        if not 0 < self.alpha < 1:
            raise DesignError('alpha 必须落在 (0, 1)')

    @property
    def baseline_mode(self):
        return self.modes[0]

    @property
    def comparison_mode(self):
        return self.modes[1]

    def planned_mode(self, flight, block):
        try:
            return self.allocations[flight][block]
        except KeyError:
            raise DesignError(f'设计未覆盖航次 {flight} 区块 {block}') from None

    def as_dict(self):
        return {
            'modes': list(self.modes),
            'washout_minutes': self.washout_minutes,
            'cutoff_minute': self.cutoff_minute,
            'min_valid_blocks': self.min_valid_blocks,
            'washout_grid': list(self.washout_grid),
            'random_seed': self.seed,
            'monte_carlo_replications': self.monte_carlo_replications,
            'enumeration_flight_cap': self.enumeration_flight_cap,
            'wind_imbalance_limit': self.wind_imbalance_limit,
            'deviation_rate_limit': self.deviation_rate_limit,
            'alpha': self.alpha,
            'allocations': {flight: [{'block': block, 'planned_mode': mode}
                                     for block, mode in blocks.items()]
                            for flight, blocks in self.allocations.items()},
        }


def load_design(path):
    import json
    from pathlib import Path
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise DesignError(f'设计文件不是合法 JSON：{error}') from None
    return SwitchbackDesign(payload)


def validate_against_records(design, records):
    """设计中的预先分配必须与数据自带的 planned_mode 逐分钟一致。

    数据里的 planned_mode 只用于校验；所有分析口径均以设计文件为准。
    """
    seen = {}
    for row in records:
        flight, block = row['flight'], row['block']
        expected = design.planned_mode(flight, block)
        if row['planned_mode'] != expected:
            raise DesignError(
                f'{flight} 区块{block} 数据 planned_mode={row["planned_mode"]} '
                f'与设计分配 {expected} 不一致')
        seen.setdefault(flight, set()).add(block)
    missing_flights = set(seen) - set(design.allocations)
    extra_flights = set(design.allocations) - set(seen)
    if missing_flights or extra_flights:
        raise DesignError(
            f'设计与数据航次集合不一致，数据独有：{sorted(missing_flights)}，'
            f'设计独有：{sorted(extra_flights)}')
    for flight, blocks in seen.items():
        designed = set(design.allocations[flight])
        # 设计覆盖但数据缺失的区块允许存在，由分析标记 missing 并披露；
        # 数据出现设计之外的区块则是契约错误。
        if not blocks <= designed:
            raise DesignError(
                f'{flight} 出现设计未分配的区块：数据 {sorted(blocks - designed)}')
