FIELDS = {'observation_id': 'str', 'flight': 'str', 'mode': 'str', 'block': 'int', 'minute': 'int', 'energy': 'float', 'wind': 'float', 'switch_minute': 'int', 'planned_mode': 'str', 'available': 'int'}
REQUIRED = ['observation_id', 'flight', 'mode', 'energy']
ENTITY = 'flight'
GROUP = 'mode'
MEASURE = 'energy'
PRODUCT = 'flight-switchback'
UNIT = '千瓦时'
