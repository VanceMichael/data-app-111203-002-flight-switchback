import math
import statistics
from collections import Counter


def describe(records):
    return {'energy_total': math.fsum(row['energy'] for row in records), 'available_minutes': sum(row['available'] for row in records), 'flights': len({row['flight'] for row in records})}
