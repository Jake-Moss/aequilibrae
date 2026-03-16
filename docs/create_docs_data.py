import sys
from pathlib import Path

from aequilibrae.utils.create_example import create_example


project_dir = Path(__file__).parent.parent
if str(project_dir) not in sys.path:
    sys.path.append(str(project_dir))

project = create_example("/tmp/accessing_sfalls_data")
project.close()
project = create_example("/tmp/accessing_nauru_data", "nauru")
project.close()
project = create_example("/tmp/accessing_coquimbo_data", "coquimbo")
project.close()
