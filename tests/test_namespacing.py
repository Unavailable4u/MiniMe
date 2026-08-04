import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.bus import read, write

write('app_slug', 'project_a')
write('registry:test', {'hello': 'world'})
print('Under project_a:', read('registry:test'))

write('app_slug', 'project_b')
print('Under project_b:', read('registry:test'))