"""Allow `python -m remote` from HASHI root directory."""
from remote.main import main
import sys
sys.exit(main())
