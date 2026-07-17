"""
Run this file in Spyder to launch the EVIE demand dashboard with optimization.

Steps:
1. Open this file in Spyder.
2. Click the green Run button.
3. Your browser should open automatically at http://127.0.0.1:8766.
4. Leave the Spyder console running while using the dashboard.
5. When finished, click the red Stop button in Spyder.

The dashboard reads the data files from this Dashboard folder. Put these files
next to this script before running:

- clean_trips_with_invoice_plan_and_segment.csv
- user_month_regular.csv
- user_month_student.csv
- user_month_access.csv
"""

from app import main
from pathlib import Path
import sys

THIS_FOLDER = Path(__file__).resolve().parent
if str(THIS_FOLDER) not in sys.path:
    sys.path.insert(0, str(THIS_FOLDER))


if __name__ == "__main__":
    main(open_browser=True)
