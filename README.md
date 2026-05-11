#  Canvas Course Export Utility

This Python utility facilitates the bulk extraction of Canvas LMS course content via the Canvas REST API. It is designed to create a **fully navigable local HTML mirror** of your courses—including pages, modules, and hosted files—ensuring your study materials remain accessible even when you are offline.

##  Key Features
* **Local Mirroring:** Recreates the Canvas course structure in a browseable offline format.
* **Content Extraction:** Downloads pages, module hierarchies, and linked assets.
* **Availability Filter:** Exclusively exports files that are currently **published** and available to students.

## Limitations & Notes

* **Gradebook Exclusion:** The current script does **not** download assessment information, scores, or feedback from the gradebook.
* **Interactive Features:** Certain external LTI tools, embedded content (e.g., H5P), or native Canvas interactive features are not available in offline copies.
* This is a **rapid-response reference implementation**, created during a live Canvas outage. It is shared for learning and adaptation purposes and has **not been hardened, refactored, or fully tested** for general production use out of its author institution.

## CAUTION
**Use this script at your own risk and discretion.** Users are responsible for performing additional quality assurance as required by their specific institution or department.

## Features
- Exports multiple Canvas courses in one run
- Handles Pages, Assignments, Discussions, Quizzes, Files, and External URLs
- Rate‑limit aware (respects Canvas API headers)
- Parallel fetching and downloading for performance
- Rewrites internal Canvas links for local viewing
- No hard‑coded credentials

## Requirements
- Python 3.9+
- PIP (dependency manager) 

### Virtual environment (recommended)

Use a Python virtual environment to keep dependencies isolated from your system Python:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

The script reads sensitive values from environment variables:

```bash
export CANVAS_ACCESS_TOKEN="your_canvas_api_token"
export CANVAS_BASE_URL="https://yourinstitution.instructure.com"
```

Edit `COURSE_IDS` in `main_crawl_pipe.py` to specify which courses to export:

```python
COURSE_IDS = [11111, 22222]
```

## Usage

Run the script directly:

```bash
python main_crawl_pipe.py
```

Each course will be exported to a local directory using the course name.

## Security Notes

*   **Never commit real API tokens** to source control
*   Use environment variables for all credentials
*   Course IDs should be treated as internal identifiers
*   Ensure the API token used has the minimum required scopes for content read‑only access.
*   This is a reference implementation, not production‑supported software.

## Disclaimer

This script uses the Canvas API and should only be run by users with appropriate institutional permission and API access.

## Licence

This is a **rapid-response reference implementation**, created during a live Canvas outage. It is shared for learning and adaptation purposes and has **not been hardened, refactored, or fully tested** for general production use. Users are free to download, copy and adapt as required.